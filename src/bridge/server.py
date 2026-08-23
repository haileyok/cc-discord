"""Single-loop Slack bridge lifecycle and localhost health endpoint."""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import secrets as stdlib_secrets
import signal
import time
import uuid
from typing import Any, Callable, Mapping

from aiohttp import web

from bridge import state
from bridge import tasks as tasks_module
from bridge.bot import Bot
from bridge.commands import CommandDispatcher, build_dispatcher
from bridge.daemon_supervisor import DaemonSupervisor
from bridge.mcp_api import McpApiError, McpCapability, McpContext, McpFacade
from bridge.mcp_auth import load_or_create_mcp_token
from bridge.secrets import Secrets
from bridge.redaction import safe_error
from bridge.tasks import TaskRegistry

log = logging.getLogger(__name__)

BOT_KEY: web.AppKey[Any] = web.AppKey("bot", object)
TASK_REGISTRY_KEY: web.AppKey[Any] = web.AppKey("task_registry", object)
STARTED_AT_KEY: web.AppKey[float] = web.AppKey("started_at", float)
MCP_FACADE_KEY: web.AppKey[Any] = web.AppKey("mcp_facade", object)
MCP_TOKEN_KEY: web.AppKey[str] = web.AppKey("mcp_token", str)
MCP_CAPABILITIES_KEY: web.AppKey[Any] = web.AppKey("mcp_capabilities", object)
MCP_MAX_REQUEST_BYTES = 64 * 1024


def _value(source: Any, *names: str, default: Any = None) -> Any:
    """Read a secret/config value from attrs or mappings without exposing it."""
    for name in names:
        if isinstance(source, Mapping):
            value = source.get(name)
        else:
            value = getattr(source, name, None)
        if value is not None and value != "":
            return value
    return default


def _secret(secrets: Secrets, *names: str, default: Any = None) -> Any:
    # Environment-style names are supported for small test/config objects too;
    # actual environment loading remains the responsibility of secrets.py.
    return _value(secrets, *names, default=default)


def _health_fields(bot: Any) -> dict[str, Any]:
    health = getattr(bot, "health", None)
    if callable(health):
        result = health()
    else:
        result = getattr(bot, "health_fields", None)
        result = result() if callable(result) else result
    if not isinstance(result, Mapping):
        result = {
            "bot_connected": bool(getattr(bot, "is_ready", False)),
            "slack_connected": bool(getattr(bot, "is_ready", False)),
            "socket_mode_connected": bool(getattr(bot, "socket_mode_connected", False)),
            "team_id": getattr(bot, "team_id", None),
            "home_channel_id": getattr(bot, "home_channel_id", None),
            "bot_user_id": getattr(bot, "bot_user_id", None),
        }
    # Never serialize token-like values even if an injected bot exposes them.
    return {
        key: value for key, value in dict(result).items()
        if "token" not in key.lower() and "secret" not in key.lower()
    }


async def _handle_health(request: web.Request) -> web.Response:
    """Return Slack connectivity and identity without credentials."""
    bot = request.app[BOT_KEY]
    started_at = request.app[STARTED_AT_KEY]
    response = _health_fields(bot)
    response.setdefault("bot_connected", bool(getattr(bot, "is_ready", False)))
    response.setdefault("team_id", getattr(bot, "team_id", None))
    response.setdefault("home_channel_id", getattr(bot, "home_channel_id", None))
    response.setdefault("bot_user_id", getattr(bot, "bot_user_id", None))
    response["uptime_secs"] = max(0, int(time.monotonic() - started_at))
    return web.Response(text=json.dumps(response), content_type="application/json")


async def _handle_mcp_call(request: web.Request) -> web.Response:
    """Authenticated private RPC used only by the local stdio MCP process."""
    if request.content_length is not None and request.content_length > MCP_MAX_REQUEST_BYTES:
        return web.json_response({"ok": False, "error": {"code": "request_too_large", "message": "MCP request body is too large", "retryable": False}}, status=413)
    authorization = request.headers.get("Authorization", "")
    expected = f"Bearer {request.app[MCP_TOKEN_KEY]}"
    if not stdlib_secrets.compare_digest(authorization, expected):
        return web.json_response({"ok": False, "error": {"code": "unauthorized", "message": "MCP authentication failed", "retryable": False}}, status=401)
    try:
        payload = await request.json(loads=json.loads)
    except (json.JSONDecodeError, UnicodeError, ValueError):
        return web.json_response({"ok": False, "error": {"code": "invalid_json", "message": "MCP request must be valid JSON", "retryable": False}}, status=400)
    if not isinstance(payload, Mapping) or not isinstance(payload.get("arguments", {}), Mapping):
        return web.json_response({"ok": False, "error": {"code": "invalid_arguments", "message": "MCP request shape is invalid", "retryable": False}}, status=400)
    facade: McpFacade = request.app[MCP_FACADE_KEY]
    request_id = str(payload.get("request_id") or request.headers.get("Idempotency-Key") or uuid.uuid4())[:200]
    context = McpContext(
        owner_user_id=facade.owner_user_id,
        team_id=facade.team_id,
        request_id=request_id,
        capabilities=request.app[MCP_CAPABILITIES_KEY],
    )
    try:
        response = await facade.call(str(payload.get("tool") or ""), payload.get("arguments") or {}, context)
    except McpApiError as exc:
        status = {"forbidden": 403, "capability_denied": 403, "task_not_found": 404, "rate_limited": 429}.get(exc.code, 400)
        return web.json_response(exc.as_dict(), status=status)
    return web.json_response(response)


def make_message_dispatcher(task_registry: TaskRegistry) -> Callable[[Any], Any]:
    """Return the Slack message callback used by :class:`bridge.bot.Bot`."""
    async def dispatch(message: Any) -> None:
        try:
            await task_registry.maybe_route_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Socket Mode callbacks should not take down the gateway. Avoid raw
            # SDK tracebacks because SlackApiError includes response bodies.
            log.error("Slack message routing failed: %s", safe_error(exc, "routing failed"))
    return dispatch


def make_socket_dispatcher(dispatcher: CommandDispatcher, task_registry: TaskRegistry) -> Callable[[Any], Any]:
    """Single explicit Socket Mode callback after Bot acknowledgement."""
    async def dispatch(payload: Any) -> Any:
        try:
            kind = str(payload.get("kind") or payload.get("type") or "").lower() if isinstance(payload, Mapping) else ""
            if kind in {"message", "app_mention", "bot_message"}:
                # Bot.handle_socket_envelope has already authenticated and normalized
                # the event.  Pass that complete mapping onward so the registry can
                # consume actor_id/team/channel/root fields without reaching into
                # provider-specific payload nesting.
                return await task_registry.maybe_route_message(payload)
            if kind == "agent_session_stopped":
                return await task_registry.handle_agent_session_stopped(payload)
            if kind in {"reaction_added", "reaction_removed"}:
                return None
            if kind == "app_home_opened":
                return await task_registry.handle_app_home_opened(payload)
            if kind == "app_context_changed":
                return await task_registry.handle_app_context_changed(payload)
            return await dispatcher.dispatch(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Slack Socket Mode routing failed: %s", safe_error(exc, "routing failed"))
            return None
    return dispatch


def make_interaction_dispatcher(dispatcher: CommandDispatcher) -> Callable[[Any], Any]:
    """Compatibility callback for direct interactive tests/callers."""
    async def dispatch(payload: Any) -> Any:
        try:
            return await dispatcher.handle_socket_envelope(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Slack interaction routing failed: %s", safe_error(exc, "interaction routing failed"))
            return None
    return dispatch


async def build_app(bot: Bot, *, task_registry: TaskRegistry | None = None,
                    started_at: float | None = None,
                    mcp_facade: McpFacade | None = None,
                    mcp_token: str | None = None,
                    mcp_capabilities: frozenset[McpCapability] | None = None) -> web.Application:
    """Build the aiohttp health and authenticated private MCP RPC application."""
    app = web.Application(client_max_size=MCP_MAX_REQUEST_BYTES)
    app[BOT_KEY] = bot
    if task_registry is not None:
        app[TASK_REGISTRY_KEY] = task_registry
    app[STARTED_AT_KEY] = started_at if started_at is not None else time.monotonic()
    app.router.add_get("/v1/health", _handle_health)
    if mcp_facade is not None and mcp_token:
        app[MCP_FACADE_KEY] = mcp_facade
        app[MCP_TOKEN_KEY] = mcp_token
        app[MCP_CAPABILITIES_KEY] = mcp_capabilities or frozenset(McpCapability)
        app.router.add_post("/v1/mcp/call", _handle_mcp_call)
    return app


def _construct_bot(secrets: Secrets, *, on_dispatch: Callable[[Any], Any]) -> Bot:
    """Construct Bot with the single explicit Socket Mode callback."""
    bot_token = _secret(secrets, "bot_token", "slack_bot_token", "SLACK_BOT_TOKEN")
    app_token = _secret(secrets, "app_token", "slack_app_token", "SLACK_APP_TOKEN")
    team_id = _secret(secrets, "team_id", "slack_team_id", "SLACK_TEAM_ID")
    home_channel_id = _secret(secrets, "home_channel_id", "slack_home_channel_id", "SLACK_HOME_CHANNEL_ID")
    # ``channel_id`` is only a compatibility fallback for pre-Slack Secrets;
    # migrated secrets should use the explicit home_channel_id spelling.
    home_channel_id = home_channel_id or _secret(secrets, "channel_id", "SLACK_CHANNEL_ID")
    owner_user_id = _secret(secrets, "owner_user_id", "slack_owner_user_id", "owner_id", "SLACK_OWNER_USER_ID")
    if not bot_token:
        raise ValueError("Slack bot token is not configured")
    return Bot(
        bot_token,
        team_id=team_id,
        owner_user_id=owner_user_id,
        home_channel_id=home_channel_id,
        app_token=app_token,
        on_dispatch=on_dispatch,
    )


async def serve(
    secrets: Secrets,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    supervisor: DaemonSupervisor | None = None,
    bot_factory: Callable[..., Bot] | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run health HTTP + Slack Socket Mode until a termination signal.

    All components are created and shut down on the caller's event loop.  DB
    reconciliation intentionally happens before Slack login so recovered daemon
    rows can stage notices without attempting Web API calls before readiness.
    """
    daemon_supervisor = supervisor or DaemonSupervisor()
    conn = await state.open_db()
    task_registry = TaskRegistry(conn, None, daemon_supervisor)
    bot: Any = None
    runner: web.AppRunner | None = None
    sweep_task: asyncio.Task[Any] | None = None
    dispatcher: CommandDispatcher | None = None
    try:
        await task_registry.load_from_db(reconcile_with_daemons=True)
        # Build the command dispatcher before Bot; the callback closes over its
        # holder because Bot must be constructed with one explicit callback.
        from bridge.projects import load_projects_from_env
        dispatcher_holder: dict[str, CommandDispatcher] = {}

        async def socket_callback(payload: Any) -> Any:
            current = dispatcher_holder.get("dispatcher")
            if current is None:
                return None
            registry_dispatch = make_socket_dispatcher(current, task_registry)
            return await registry_dispatch(payload)

        factory = bot_factory or _construct_bot
        bot = factory(secrets, on_dispatch=socket_callback)
        task_registry.bind_bot(bot)
        dispatcher = build_dispatcher(bot, task_registry, load_projects_from_env())
        dispatcher_holder["dispatcher"] = dispatcher
        mcp_token = load_or_create_mcp_token()
        mcp_facade = McpFacade(
            bot=bot,
            registry=task_registry,
            owner_user_id=str(_secret(secrets, "owner_user_id", "SLACK_OWNER_USER_ID")),
            team_id=str(_secret(secrets, "team_id", "SLACK_TEAM_ID")),
        )

        app = await build_app(
            bot, task_registry=task_registry, mcp_facade=mcp_facade, mcp_token=mcp_token,
        )
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()

        # Bot.start validates Slack identity and connects Socket Mode on this
        # same loop; consumers and startup notices are deliberately later.
        await bot.start()
        while not bool(getattr(bot, "is_ready", False)):
            await asyncio.sleep(0.05)
        # bot.bot_user_id / bot.bot_id are only populated by bot.start()'s
        # auth.test call, which runs after the bind_bot() above. Without this
        # second call, task_registry._bridge_user_id stays None for the
        # process's entire lifetime, and every mention-stripping helper
        # (_strip_verified_mention) silently no-ops on every message forever
        # -- not just at startup. bind_bot() is idempotent/safe to call again.
        task_registry.bind_bot(bot)
        reconcile_promotions = getattr(task_registry, "reconcile_promotion_journals", None)
        if callable(reconcile_promotions):
            result = reconcile_promotions()
            if inspect.isawaitable(result):
                await result
        flush_notices = getattr(task_registry, "flush_startup_notices", None)
        if callable(flush_notices):
            result = flush_notices()
            if inspect.isawaitable(result):
                await result
        await task_registry.start_event_consumers()

        tasks_module.sweep_old_attachments()

        async def attachment_sweep_loop() -> None:
            while True:
                try:
                    await asyncio.sleep(3600)
                    tasks_module.sweep_old_attachments()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("attachment sweep failed")

        sweep_task = asyncio.create_task(attachment_sweep_loop(), name="attachment-sweep")
        stop = stop_event or asyncio.Event()
        loop = asyncio.get_running_loop()
        installed: list[signal.Signals] = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
                installed.append(sig)
            except (NotImplementedError, RuntimeError, ValueError):
                log.debug("signal handler unavailable for %s", sig)
        try:
            await stop.wait()
        finally:
            for sig in installed:
                with contextlib.suppress(Exception):
                    loop.remove_signal_handler(sig)
    finally:
        if sweep_task is not None:
            sweep_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweep_task
        with contextlib.suppress(Exception):
            await task_registry.shutdown()
        if bot is not None:
            with contextlib.suppress(Exception):
                await bot.close()
        if runner is not None:
            with contextlib.suppress(Exception):
                await runner.cleanup()
        await state.close_db(conn)


__all__ = [
    "BOT_KEY", "STARTED_AT_KEY", "TASK_REGISTRY_KEY", "build_app",
    "make_interaction_dispatcher", "make_message_dispatcher", "make_socket_dispatcher", "serve",
]

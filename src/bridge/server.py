"""Single-loop Slack bridge lifecycle and localhost health endpoint."""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import signal
import time
from typing import Any, Callable, Mapping

from aiohttp import web

from bridge import state
from bridge import tasks as tasks_module
from bridge.bot import Bot
from bridge.commands import CommandDispatcher, build_dispatcher
from bridge.daemon_supervisor import DaemonSupervisor
from bridge.secrets import Secrets
from bridge.redaction import safe_error
from bridge.tasks import TaskRegistry

log = logging.getLogger(__name__)

BOT_KEY: web.AppKey[Any] = web.AppKey("bot", object)
TASK_REGISTRY_KEY: web.AppKey[Any] = web.AppKey("task_registry", object)
STARTED_AT_KEY: web.AppKey[float] = web.AppKey("started_at", float)


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
            if kind in {"reaction_added", "reaction_removed"}:
                return None
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
                    started_at: float | None = None) -> web.Application:
    """Build the aiohttp health application; no external calls are made."""
    app = web.Application()
    app[BOT_KEY] = bot
    if task_registry is not None:
        app[TASK_REGISTRY_KEY] = task_registry
    app[STARTED_AT_KEY] = started_at if started_at is not None else time.monotonic()
    app.router.add_get("/v1/health", _handle_health)
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

        app = await build_app(bot, task_registry=task_registry)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()

        # Bot.start validates Slack identity and connects Socket Mode on this
        # same loop; consumers and startup notices are deliberately later.
        await bot.start()
        while not bool(getattr(bot, "is_ready", False)):
            await asyncio.sleep(0.05)
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

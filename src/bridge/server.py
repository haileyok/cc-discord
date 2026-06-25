"""aiohttp web server for /v1/notify, /v1/health, and /v1/ask endpoints."""

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

import discord
from aiohttp import web

from bridge.approvals import ApprovalRouter
from bridge.bot import Bot, BotNotReady
from bridge.listener import Listener, _PendingAsk
from bridge.secrets import Secrets
from bridge import tasks as tasks_module
from bridge.tasks import TaskRegistry
from bridge.threads import ThreadRegistry
from bridge.zellij import ZellijManager
from bridge import state

logger = logging.getLogger(__name__)


def _phantom_enabled() -> bool:
    """Whether the daemon should keep a headless phantom zellij client attached.

    Default on — without an attached, sized terminal client zellij drops
    `action write-chars`, so headless-driven TUIs (Claude Code) bind but never
    render or receive keystrokes. Opt out with BRIDGE_PHANTOM=0 (e.g. when you
    already attach a real terminal to the session yourself).
    """
    return os.environ.get("BRIDGE_PHANTOM", "1") != "0"


def _phantom_command() -> list[str]:
    """Argv for the phantom client. Run as a package module so it works under
    both `uv run` and `uv tool install` (the wheel ships src/bridge, not
    scripts/)."""
    return [sys.executable, "-m", "bridge.phantom"]


async def _phantom_supervisor() -> None:
    """Spawn and keep the phantom zellij client alive for the daemon's lifetime.

    Restarts the client (capped exponential backoff) if it exits — e.g. after a
    session teardown/recreate — so the session always has a sized client. On
    cancellation (daemon shutdown) the child is terminated. Never raises out of
    the loop except CancelledError; transient spawn failures are logged and
    retried so a flaky launch can't take the daemon down.
    """
    cmd = _phantom_command()
    backoff = 1.0
    while True:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            logger.info("phantom client started (pid %s)", proc.pid)
            backoff = 1.0  # reset after a clean start
            rc = await proc.wait()
            logger.warning(
                "phantom client exited (rc=%s); restarting in %.0fs", rc, backoff
            )
        except asyncio.CancelledError:
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
            raise
        except Exception:
            logger.exception("phantom supervisor error; retrying in %.0fs", backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


def _clamp_timeout(secs: float) -> float:
    """Clamp timeout value to valid range [5, 3600] seconds.

    Args:
        secs: timeout in seconds

    Returns:
        clamped timeout in seconds

    Raises:
        ValueError: if secs cannot be converted to float
    """
    timeout = float(secs)
    return max(5.0, min(3600.0, timeout))


def _format_question(question: str, cwd: str) -> str:
    """Format a question with header and working directory.

    Args:
        question: the user's question text
        cwd: the working directory (can be empty)

    Returns:
        formatted question string
    """
    if cwd:
        return f"❓ asks\n{question}\n\n(cwd: {cwd})"
    return f"❓ asks\n{question}"


class AskLockMap:
    """Per-thread asyncio.Lock factory for FIFO serialization of /v1/ask calls."""

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def get(self, thread_id: int) -> asyncio.Lock:
        """Get or create a lock for a thread_id.

        Args:
            thread_id: Discord thread ID

        Returns:
            asyncio.Lock for the thread
        """
        async with self._guard:
            lock = self._locks.get(thread_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[thread_id] = lock
            return lock


# Typed AppKey definitions to avoid NotAppKeyWarning
BOT_KEY: web.AppKey[Bot] = web.AppKey("bot", Bot)
THREADS_KEY: web.AppKey[ThreadRegistry] = web.AppKey("threads", ThreadRegistry)
LISTENER_KEY: web.AppKey[Listener] = web.AppKey("listener", Listener)
ASK_LOCKS_KEY: web.AppKey[AskLockMap] = web.AppKey("ask_locks", AskLockMap)
TASK_REGISTRY_KEY: web.AppKey[TaskRegistry] = web.AppKey("task_registry", TaskRegistry)
ZELLIJ_KEY: web.AppKey[ZellijManager] = web.AppKey("zellij", ZellijManager)
APPROVAL_ROUTER_KEY: web.AppKey[ApprovalRouter] = web.AppKey("approval_router", ApprovalRouter)

STARTED_AT_KEY: web.AppKey[float] = web.AppKey("started_at", float)


async def _handle_notify(request: web.Request) -> web.Response:
    """Handle POST /v1/notify.

    Body (JSON): { "session_id": str, "cwd": str, "message": str, "title"?: str, "level"?: str }
    """
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        return web.Response(
            status=400,
            text=json.dumps({"error": "invalid json"}),
            content_type="application/json",
        )

    # Validate required fields
    required = ["session_id", "cwd", "message"]
    if not all(key in body for key in required):
        missing = [key for key in required if key not in body]
        return web.Response(
            status=400,
            text=json.dumps({"error": f"missing required fields: {', '.join(missing)}"}),
            content_type="application/json",
        )

    bot: Bot = request.app[BOT_KEY]
    registry: ThreadRegistry = request.app[THREADS_KEY]
    message = body["message"]

    # Check if bot is ready before routing to thread
    if not bot.is_ready:
        return web.Response(
            status=503,
            text=json.dumps({"error": "bot_not_connected"}),
            content_type="application/json",
        )

    try:
        thread_id = await registry.get_or_create_thread(body["session_id"], body["cwd"])
        message_ids = await bot.post(message, thread_id=thread_id)
        return web.Response(
            status=200,
            text=json.dumps(
                {"thread_id": thread_id, "message_id": message_ids[0] if message_ids else None}
            ),
            content_type="application/json",
        )
    except BotNotReady:
        return web.Response(
            status=503,
            text=json.dumps({"error": "bot_not_connected"}),
            content_type="application/json",
        )
    except Exception:
        logger.exception("notify failed")
        return web.Response(
            status=500,
            text=json.dumps({"error": "internal"}),
            content_type="application/json",
        )


async def _handle_ask(request: web.Request) -> web.Response:
    """Handle POST /v1/ask.

    Body (JSON): {
        "session_id": str,
        "cwd": str,
        "question": str,
        "timeout_secs"?: number (default 900, clamped to [5, 3600])
    }

    Response on success (200):
        { "reply": str, "replied_at": str (ISO8601) }

    Response on timeout (408):
        { "error": "timeout" }

    Response on bot not connected (503):
        { "error": "bot_not_connected" }
    """
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        return web.Response(
            status=400,
            text=json.dumps({"error": "invalid json"}),
            content_type="application/json",
        )

    # Validate required fields
    required = ["session_id", "cwd", "question"]
    if not all(key in body for key in required):
        missing = [key for key in required if key not in body]
        return web.Response(
            status=400,
            text=json.dumps({"error": f"missing required fields: {', '.join(missing)}"}),
            content_type="application/json",
        )

    # Validate timeout_secs early if provided
    if "timeout_secs" in body:
        try:
            _clamp_timeout(body["timeout_secs"])
        except ValueError:
            return web.Response(
                status=400,
                text=json.dumps({"error": "invalid timeout_secs"}),
                content_type="application/json",
            )

    try:
        bot: Bot = request.app[BOT_KEY]
        registry: ThreadRegistry = request.app[THREADS_KEY]
        listener: Listener = request.app[LISTENER_KEY]
        locks: AskLockMap = request.app[ASK_LOCKS_KEY]

        # Check if bot is ready
        if not bot.is_ready:
            return web.Response(
                status=503,
                text=json.dumps({"error": "bot_not_connected"}),
                content_type="application/json",
            )

        try:
            thread_id = await registry.get_or_create_thread(body["session_id"], body["cwd"])
        except BotNotReady:
            return web.Response(
                status=503,
                text=json.dumps({"error": "bot_not_connected"}),
                content_type="application/json",
            )

        # Acquire per-thread lock for FIFO serialization
        lock = await locks.get(thread_id)
        async with lock:
            # Post the question AFTER acquiring the lock
            ask_text = _format_question(body["question"], body["cwd"])
            try:
                await bot.post(ask_text, thread_id=thread_id)
            except BotNotReady:
                return web.Response(
                    status=503,
                    text=json.dumps({"error": "bot_not_connected"}),
                    content_type="application/json",
                )

            # Register pending ask and wait for reply
            asked_at = datetime.now(timezone.utc)
            ask = _PendingAsk(asked_at)
            await listener.register(thread_id, ask)
            try:
                # Parse and clamp timeout
                timeout = _clamp_timeout(body.get("timeout_secs", 900))
                result = await asyncio.wait_for(ask.future, timeout=timeout)
            except asyncio.TimeoutError:
                return web.Response(
                    status=408,
                    text=json.dumps({"error": "timeout"}),
                    content_type="application/json",
                )
            finally:
                await listener.unregister(thread_id, ask)

        return web.Response(
            status=200,
            text=json.dumps(
                {"reply": result.reply, "replied_at": result.replied_at},
            ),
            content_type="application/json",
        )
    except BotNotReady:
        return web.Response(
            status=503,
            text=json.dumps({"error": "bot_not_connected"}),
            content_type="application/json",
        )
    except asyncio.TimeoutError:
        return web.Response(
            status=408,
            text=json.dumps({"error": "timeout"}),
            content_type="application/json",
        )
    except Exception:
        logger.exception("ask failed")
        return web.Response(
            status=500,
            text=json.dumps({"error": "internal"}),
            content_type="application/json",
        )


async def _handle_health(request: web.Request) -> web.Response:
    """Handle GET /v1/health."""
    bot: Bot = request.app[BOT_KEY]
    started_at: float = request.app[STARTED_AT_KEY]
    uptime_secs = int(time.monotonic() - started_at)

    response = {
        "bot_connected": bot.is_ready,
        "channel_id": bot.channel_id,
        "uptime_secs": uptime_secs,
    }
    return web.Response(
        status=200,
        text=json.dumps(response),
        content_type="application/json",
    )


async def _handle_hook_event(request: web.Request) -> web.Response:
    """Handle POST /v1/hook/event — dispatch by hook_event_name to TaskRegistry."""
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        return web.Response(
            status=400,
            text=json.dumps({"error": "invalid json"}),
            content_type="application/json",
        )

    # Validate that body is a JSON object (dict)
    if not isinstance(body, dict):
        return web.Response(
            status=400,
            text=json.dumps({"error": "body must be a JSON object"}),
            content_type="application/json",
        )

    # Validate required field
    if "hook_event_name" not in body or not isinstance(body.get("hook_event_name"), str):
        return web.Response(
            status=400,
            text=json.dumps({"error": "missing required field: hook_event_name"}),
            content_type="application/json",
        )

    try:
        registry: TaskRegistry = request.app[TASK_REGISTRY_KEY]
        await registry.handle_event(body["hook_event_name"], body)
        return web.Response(
            status=200,
            text=json.dumps({"ok": True}),
            content_type="application/json",
        )
    except Exception:
        logger.exception("hook event handler failed")
        return web.Response(
            status=500,
            text=json.dumps({"error": "internal"}),
            content_type="application/json",
        )


async def _handle_pretooluse(request: web.Request) -> web.Response:
    """Handle POST /v1/hook/pretooluse — register Future, post approval prompt, await decision."""
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        return web.Response(
            status=400,
            text=json.dumps({"error": "invalid json"}),
            content_type="application/json",
        )

    required = ("request_id", "task_id", "tool_name", "tool_input")
    if not all(k in body for k in required):
        missing = [k for k in required if k not in body]
        return web.Response(
            status=400,
            text=json.dumps({"error": f"missing: {missing}"}),
            content_type="application/json",
        )

    try:
        registry: TaskRegistry = request.app[TASK_REGISTRY_KEY]
        router: ApprovalRouter = request.app[APPROVAL_ROUTER_KEY]

        task = registry.get_by_task_id(body["task_id"])
        if task is None:
            # Fail-closed at the daemon level too: deny if we don't know this task
            return web.json_response({
                "decision": "deny",
                "reason": f"unknown task_id {body['task_id']}",
            }, status=200)

        decision, reason = await router.request_permission(
            request_id=body["request_id"],
            task_id=body["task_id"],
            thread_id=task.thread_id,
            tool_name=body["tool_name"],
            tool_input=body.get("tool_input") or {},
        )
        return web.json_response({"decision": decision, "reason": reason}, status=200)
    except Exception:
        logger.exception("pretooluse handler failed")
        return web.Response(
            status=500,
            text=json.dumps({"error": "internal"}),
            content_type="application/json",
        )


def make_message_dispatcher(
    approval_router: ApprovalRouter,
    task_registry: TaskRegistry,
    listener: Listener,
) -> callable:
    """Create a message dispatcher closure.

    The dispatcher enforces a critical order: resolve_by_text (approval replies)
    takes precedence over maybe_route_message (task thread routing), which takes
    precedence over listener.deliver (general listener).

    This invariant must not regress: if a user types a free-text deny reply to an
    approval prompt, it is NOT routed to the task pane.

    Args:
        approval_router: ApprovalRouter instance for resolving approvals
        task_registry: TaskRegistry instance for routing task-thread messages
        listener: Listener instance for delivering non-routed messages

    Returns:
        async callable that dispatches messages in the correct order
    """
    async def _dispatch_message(msg):
        # First, try to resolve any pending approval via text reply
        if await approval_router.resolve_by_text(msg.channel.id, msg.content or "", msg.author.bot):
            return
        # Then, try to resolve any pending TUI answer via text reply
        if await approval_router.resolve_tui_by_text(msg.channel.id, msg.content or "", msg.author.bot):
            return
        # Then, check task threads for tool output routing
        if await task_registry.maybe_route_message(msg):
            return
        # Finally, fall back to the general listener
        await listener.deliver(msg)

    return _dispatch_message


async def build_app(bot: Bot, *, started_at: float | None = None) -> web.Application:
    """Build and configure the aiohttp Application."""
    app = web.Application()
    app[BOT_KEY] = bot
    app[STARTED_AT_KEY] = started_at if started_at is not None else time.monotonic()
    app.router.add_post("/v1/notify", _handle_notify)
    app.router.add_post("/v1/ask", _handle_ask)
    app.router.add_get("/v1/health", _handle_health)
    app.router.add_post("/v1/hook/event", _handle_hook_event)
    app.router.add_post("/v1/hook/pretooluse", _handle_pretooluse)
    return app


async def serve(secrets: Secrets, *, host: str = "127.0.0.1", port: int = 8787) -> None:
    """Run the bridge server with bot integration and signal handling.

    Binds to host:port, starts the Discord bot, and runs until SIGTERM/SIGINT.
    Opens the database for session persistence and instantiates ThreadRegistry.
    """
    listener = Listener()
    zellij = ZellijManager()
    await zellij.ensure_session_alive()

    conn = await state.open_db()
    approval_router = ApprovalRouter(None, conn)
    task_registry = TaskRegistry(conn, None, zellij, approval_router)
    await task_registry.load_from_db(reconcile_with_zellij=True)

    # Dispatcher closures call into approval_router/task_registry; both are
    # available now. The Bot is constructed after these closures because Bot's
    # constructor wires the callbacks up to discord.py event handlers.
    _dispatch_message = make_message_dispatcher(approval_router, task_registry, listener)

    bot_holder: dict[str, Bot] = {}

    async def _on_reaction_dispatch(payload: discord.RawReactionActionEvent) -> None:
        """Dispatch raw reaction events to approval and TUI resolvers."""
        bot_self = bot_holder.get("bot")
        client_user = bot_self.client.user if bot_self and bot_self.client else None
        user_is_self_bot = bool(client_user and payload.user_id == client_user.id)
        if await approval_router.resolve_by_reaction(payload.message_id, str(payload.emoji), user_is_self_bot):
            return
        await approval_router.resolve_tui_by_reaction(payload.message_id, str(payload.emoji), user_is_self_bot)

    bot = Bot(secrets.bot_token, secrets.channel_id, on_message=_dispatch_message, on_reaction=_on_reaction_dispatch)
    bot_holder["bot"] = bot

    approval_router.bind_bot(bot)
    task_registry.bind_bot(bot)
    registry = ThreadRegistry(bot, conn)
    app = await build_app(bot)
    app[THREADS_KEY] = registry
    app[LISTENER_KEY] = listener
    app[ASK_LOCKS_KEY] = AskLockMap()
    app[TASK_REGISTRY_KEY] = task_registry
    app[ZELLIJ_KEY] = zellij
    app[APPROVAL_ROUTER_KEY] = approval_router
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    await bot.start()
    logger.info("listening on http://%s:%d", host, port)

    # Build and sync the slash command tree
    from bridge.commands import build_tree
    from bridge.projects import load_projects_from_env

    projects = load_projects_from_env()
    tree = build_tree(bot, task_registry, projects)
    # Wait for bot to be ready before syncing commands
    while not bot.is_ready:
        await asyncio.sleep(0.1)
    # Bot is ready — drain any reconciliation notices staged by load_from_db.
    await task_registry.flush_startup_notices()
    guild_id = bot.channel.guild.id  # type: ignore[union-attr]
    guild = discord.Object(id=guild_id)
    tree.copy_global_to(guild=guild)  # registers globally to this guild for instant sync
    # Slash-command sync hits the Discord HTTP API and can transiently 503
    # during incidents; bounded retry so a single 503 doesn't crash startup.
    sync_attempts = 0
    while True:
        try:
            synced = await tree.sync(guild=guild)
            break
        except discord.DiscordServerError as e:
            sync_attempts += 1
            if sync_attempts >= 4:
                logger.warning(
                    "slash command sync failed after %d attempts (%s); "
                    "continuing without resync",
                    sync_attempts, e,
                )
                synced = []
                break
            backoff = 0.5 * (2 ** (sync_attempts - 1))
            logger.warning(
                "slash command sync got %s; retrying in %.1fs (attempt %d/4)",
                e, backoff, sync_attempts,
            )
            await asyncio.sleep(backoff)
    logger.info("synced %d slash commands to guild %d", len(synced), guild_id)

    # Sweep stale attachments at startup, then schedule hourly background
    # sweep. TTL is `BRIDGE_ATTACHMENT_TTL_SECS` env var (default 7 days).
    tasks_module.sweep_old_attachments()

    async def _attachment_sweep_loop() -> None:
        while True:
            try:
                await asyncio.sleep(3600)
                tasks_module.sweep_old_attachments()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("attachment sweep failed")

    sweep_task = asyncio.create_task(
        _attachment_sweep_loop(), name="attachment-sweep"
    )

    # Phantom client: keep a sized, headless terminal client attached to the
    # zellij session so TUIs spawned inside it (Claude Code) render and receive
    # keystrokes without a human attaching a terminal. Default on; opt out with
    # BRIDGE_PHANTOM=0. The session already exists (ensure_session_alive above).
    phantom_task: asyncio.Task | None = None
    if _phantom_enabled():
        phantom_task = asyncio.create_task(_phantom_supervisor(), name="phantom-supervisor")
    else:
        logger.info("phantom client disabled (BRIDGE_PHANTOM=0)")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    finally:
        sweep_task.cancel()
        if phantom_task is not None:
            phantom_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweep_task
        if phantom_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await phantom_task
        await bot.close()
        await runner.cleanup()
        await state.close_db(conn)

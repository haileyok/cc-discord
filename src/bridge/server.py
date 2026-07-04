"""aiohttp web server (health only) + Discord message routing for the bridge.

The HTTP surface shrank to ``GET /v1/health`` when the bridge moved from the
Claude-Code hook model to the Polytoken daemon model: inbound prompts now flow
through ``POST /prompt`` on each per-task daemon (driven by ``TaskRegistry``),
and outbound activity arrives over each daemon's ``/events`` SSE stream, so the
old hook/notify/ask endpoints are gone.
"""

import asyncio
import contextlib
import json
import logging
import os
import signal
import time

import discord
from aiohttp import web

from bridge import state
from bridge import tasks as tasks_module
from bridge.bot import Bot
from bridge.daemon_supervisor import DaemonSupervisor
from bridge.secrets import Secrets
from bridge.tasks import TaskRegistry

logger = logging.getLogger(__name__)


# Typed AppKey definitions to avoid NotAppKeyWarning.
BOT_KEY: web.AppKey[Bot] = web.AppKey("bot", Bot)
TASK_REGISTRY_KEY: web.AppKey[TaskRegistry] = web.AppKey("task_registry", TaskRegistry)
STARTED_AT_KEY: web.AppKey[float] = web.AppKey("started_at", float)
SUPERVISOR_KEY: web.AppKey[DaemonSupervisor] = web.AppKey("supervisor", DaemonSupervisor)
STOP_VERIFY_DELAY_KEY: web.AppKey[float] = web.AppKey("stop_verify_delay", float)
NOTIFY_TASKS_KEY: web.AppKey[set] = web.AppKey("notify_tasks", set)

# Grace period between a `stop` hook event and the /state verification, so a
# session that immediately continues (queued prompt, goal continuation) has
# started its next turn by the time we look. Override with
# BRIDGE_STOP_VERIFY_DELAY_SECS.
DEFAULT_STOP_VERIFY_DELAY_SECS = 2.5


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
        status=200, text=json.dumps(response), content_type="application/json"
    )


def make_message_dispatcher(task_registry: TaskRegistry) -> callable:
    """Create the on_message dispatcher: route messages in task-bound threads
    to their daemon; ignore everything else."""

    async def _dispatch_message(msg) -> None:
        with contextlib.suppress(Exception):
            await task_registry.maybe_route_message(msg)

    return _dispatch_message


def _summarize_notify(event: str, session_id: str, project: str, body: dict) -> str:
    """Build a one-line Discord summary from a Polytoken hook payload."""
    sid = (session_id or "unknown")[:12]
    leaf = ""
    if project:
        leaf = f" ({os.path.basename(project.rstrip('/'))})"
    if event == "stop":
        return f"🔔 Session `{sid}`{leaf} finished a turn — waiting for input."
    if event == "notification":
        summary = str(body.get("summary") or "needs your attention")
        return f"🔔 Session `{sid}`{leaf}: {summary[:300]}"
    detail = str(body.get("summary") or body.get("event") or event or "activity")
    return f"🔔 Session `{sid}`{leaf}: {detail[:300]}"


async def _stop_session_awaits_input(supervisor, session_id: str) -> tuple[bool, str]:
    """Decide whether a `stop` hook event means the session is genuinely waiting.

    `stop` fires whenever a turn would end — including turns that immediately
    continue (queued prompts, goal continuation, harness-driven sessions that
    end turns while awaiting forwarded tool results). Mirror the main-thread
    gate (TurnComplete never pings; only real input needs do) by checking the
    session's live `/state`: ping when it has ``pending_interrogatives`` (blocked
    on an answer) or is idle; suppress when the turn is (still or again) in
    flight, or the session is gone from the registry entirely. Fails open —
    only positive evidence of activity suppresses the ping. ``turn_in_flight``
    missing from older daemons reads as falsy, i.e. pre-gate behavior.
    """
    if supervisor is None or not session_id:
        return True, "unverifiable"
    try:
        info = await supervisor.find_session(session_id)
    except Exception:
        logger.exception("notify: could not list sessions to verify %s", session_id)
        return True, "registry_error"
    if info is None:
        return False, "session_gone"
    client = supervisor.client_for(info.port)
    try:
        state = await client.state()
    except Exception:
        logger.warning("notify: could not read /state for %s on port %d",
                       session_id, info.port)
        return True, "state_error"
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()
    if state.get("pending_interrogatives"):
        return True, "pending_interrogative"
    if state.get("turn_in_flight"):
        return False, "turn_in_flight"
    return True, "idle"


async def _handle_notify(request: web.Request) -> web.Response:
    """Handle POST /v1/notify — a global Polytoken hook forwarding an event.

    The hook (hooks/notify-discord.sh) posts here on `stop` (session waiting for
    input) and `notification` events for ANY Polytoken session. The bridge posts
    the summary to the bot channel with an @mention. Events for sessions the
    bridge already drives are suppressed — those sessions render their turn
    completion (typing indicator) and notifications (AttentionPing) inline in
    their task thread, so a channel ping would double-notify. The hook's value is
    the sessions the bridge can't see (TUI, `exec`, externally-started daemons).

    `stop` events are not posted directly: the hook is blocking, so we ack it
    immediately and verify in the background (after a short grace delay) that
    the session is actually waiting for input before pinging — see
    :func:`_stop_session_awaits_input`. `notification` events post immediately
    (parity with the main thread's AttentionPing).
    """
    bot: Bot = request.app[BOT_KEY]
    registry: TaskRegistry = request.app[TASK_REGISTRY_KEY]
    event = request.headers.get("X-Polytoken-Event", "")
    session_id = request.headers.get("X-Polytoken-Session", "")
    project = request.headers.get("X-Polytoken-Project", "")
    try:
        body = await request.json()
    except Exception:
        body = {}

    # Suppress for sessions the bridge already renders inline (any event type);
    # the hook exists to cover sessions the bridge does not drive.
    if session_id and registry.get_by_session_id(session_id) is not None:
        return web.Response(status=200, text='{"status":"suppressed"}',
                            content_type="application/json")

    # Suppress for non-interactive sessions (subagents, exec, background daemons).
    # Their activity is rendered inline through the parent session's event stream,
    # and a "waiting for input" ping is meaningless for a session that can't
    # accept input.
    ni = request.headers.get("X-Polytoken-Non-Interactive", "").strip().lower()
    if ni and ni not in ("0", "false", "no", "off"):
        return web.Response(status=200, text='{"status":"suppressed_non_interactive"}',
                            content_type="application/json")

    if event == "stop":
        # Ack the (blocking) hook now; verify + post in the background.
        supervisor = request.app.get(SUPERVISOR_KEY)
        delay = request.app.get(STOP_VERIFY_DELAY_KEY, DEFAULT_STOP_VERIFY_DELAY_SECS)
        notify_tasks = request.app[NOTIFY_TASKS_KEY]

        async def _verify_then_post() -> None:
            await asyncio.sleep(delay)
            should_ping, reason = await _stop_session_awaits_input(supervisor, session_id)
            if not should_ping:
                logger.info("notify: suppressed stop ping for %s (%s)",
                            (session_id or "?")[:12], reason)
                return
            summary = _summarize_notify(event, session_id, project, body)
            try:
                await bot.post(f"{registry.notify_mention_prefix()}{summary}")
            except Exception:
                logger.exception("failed to post notify summary to Discord")

        t = asyncio.create_task(
            _verify_then_post(), name=f"notify-stop-{(session_id or '?')[:12]}"
        )
        notify_tasks.add(t)
        t.add_done_callback(notify_tasks.discard)
        return web.Response(status=200, text='{"status":"scheduled"}',
                            content_type="application/json")

    summary = _summarize_notify(event, session_id, project, body)
    mention = registry.notify_mention_prefix()
    try:
        await bot.post(f"{mention}{summary}")
    except Exception:
        logger.exception("failed to post notify summary to Discord")
        return web.Response(status=502, text='{"status":"post_failed"}',
                            content_type="application/json")
    return web.Response(status=200, text='{"status":"posted"}',
                        content_type="application/json")


async def build_app(
    bot: Bot,
    *,
    started_at: float | None = None,
    supervisor: DaemonSupervisor | None = None,
    stop_verify_delay: float | None = None,
) -> web.Application:
    """Build and configure the aiohttp Application (health + notify)."""
    app = web.Application()
    app[BOT_KEY] = bot
    app[STARTED_AT_KEY] = started_at if started_at is not None else time.monotonic()
    if supervisor is not None:
        app[SUPERVISOR_KEY] = supervisor
    if stop_verify_delay is None:
        stop_verify_delay = float(
            os.environ.get("BRIDGE_STOP_VERIFY_DELAY_SECS", DEFAULT_STOP_VERIFY_DELAY_SECS)
        )
    app[STOP_VERIFY_DELAY_KEY] = stop_verify_delay
    app[NOTIFY_TASKS_KEY] = set()
    app.router.add_get("/v1/health", _handle_health)
    app.router.add_post("/v1/notify", _handle_notify)
    return app


async def serve(secrets: Secrets, *, host: str = "127.0.0.1", port: int = 8787) -> None:
    """Run the bridge: HTTP health server + Discord bot, until SIGTERM/SIGINT."""
    # Startup version guard: warn loudly if the polytoken binary is outside the
    # pinned series. The daemon HTTP/event contracts are verified against a
    # specific version; a mismatch can break the bridge silently. `doctor` is
    # the hard gate; here we warn (and surface the version) but still start.
    from bridge.version_guard import check_polytoken_version, detect_polytoken_version_detail

    v, is_prerelease = detect_polytoken_version_detail(os.environ.get("POLYTOKEN_BIN", "polytoken"))
    ok, msg = check_polytoken_version(v, is_prerelease=is_prerelease)
    if ok:
        logger.info("polytoken version check: %s", msg)
    else:
        logger.warning("⚠️  polytoken VERSION MISMATCH: %s — the bridge is pinned to a "
                       "specific daemon contract; expect breakage. Run "
                       "`claude-discord-bridge doctor` for details.", msg)

    supervisor = DaemonSupervisor()

    conn = await state.open_db()
    task_registry = TaskRegistry(conn, None, supervisor)
    await task_registry.load_from_db(reconcile_with_daemons=True)

    _dispatch_message = make_message_dispatcher(task_registry)

    bot = Bot(secrets.bot_token, secrets.channel_id, on_message=_dispatch_message)
    task_registry.bind_bot(bot)

    app = await build_app(bot, supervisor=supervisor)
    app[TASK_REGISTRY_KEY] = task_registry
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    await bot.start()
    logger.info("listening on http://%s:%d", host, port)

    from bridge.commands import build_tree
    from bridge.projects import load_projects_from_env

    projects = load_projects_from_env()
    tree = build_tree(bot, task_registry, projects)
    while not bot.is_ready:
        await asyncio.sleep(0.1)
    await task_registry.flush_startup_notices()
    # Now that the bot is bound + ready, start the SSE consumers for any tasks
    # recovered during reconcile (deferred so they never post before the bot).
    await task_registry.start_event_consumers()
    guild_id = bot.channel.guild.id  # type: ignore[union-attr]
    guild = discord.Object(id=guild_id)
    tree.copy_global_to(guild=guild)
    sync_attempts = 0
    while True:
        try:
            synced = await tree.sync(guild=guild)
            break
        except discord.DiscordServerError as e:
            sync_attempts += 1
            if sync_attempts >= 4:
                logger.warning(
                    "slash command sync failed after %d attempts (%s); continuing without resync",
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

    sweep_task = asyncio.create_task(_attachment_sweep_loop(), name="attachment-sweep")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    finally:
        sweep_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweep_task
        await task_registry.shutdown()
        await bot.close()
        await runner.cleanup()
        await state.close_db(conn)

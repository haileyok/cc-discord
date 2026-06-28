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


async def build_app(bot: Bot, *, started_at: float | None = None) -> web.Application:
    """Build and configure the aiohttp Application (health only)."""
    app = web.Application()
    app[BOT_KEY] = bot
    app[STARTED_AT_KEY] = started_at if started_at is not None else time.monotonic()
    app.router.add_get("/v1/health", _handle_health)
    return app


async def serve(secrets: Secrets, *, host: str = "127.0.0.1", port: int = 8787) -> None:
    """Run the bridge: HTTP health server + Discord bot, until SIGTERM/SIGINT."""
    supervisor = DaemonSupervisor()

    conn = await state.open_db()
    task_registry = TaskRegistry(conn, None, supervisor)
    await task_registry.load_from_db(reconcile_with_daemons=True)

    _dispatch_message = make_message_dispatcher(task_registry)

    bot = Bot(secrets.bot_token, secrets.channel_id, on_message=_dispatch_message)
    task_registry.bind_bot(bot)

    app = await build_app(bot)
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

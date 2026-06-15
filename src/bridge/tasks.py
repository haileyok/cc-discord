"""Discord-driven Polytoken daemon sessions.

Each task is one Polytoken daemon process (spawned via ``DaemonSupervisor``)
bound to a Discord thread. Inbound Discord messages become ``POST /prompt``
calls; the daemon's ``/events`` SSE stream is consumed per task, translated
to render actions by :mod:`bridge.events`, and driven onto Discord through
the surviving renderers (tool-summary aggregator, subagent embeds, ``Bot``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import discord

from bridge import tool_summary, voice
from bridge.daemon_supervisor import DaemonSupervisor, DaemonSupervisorError
from bridge.events import (
    AskQuestion,
    AssistantText,
    AssistantThinking,
    AttentionPing,
    Clarification,
    Confirmation,
    ImageResolved,
    ModelError,
    Reconcile,
    StateRefresh,
    StatusNote,
    SubagentActivity,
    SubagentCompleted,
    SubagentStarted,
    TitleChange,
    ToolDiff,
    ToolFailure,
    ToolLine,
    Translator,
    TurnCancelled,
    TurnComplete,
    TurnStarted,
)
from bridge.listener import MessageLike
from bridge.polytoken_client import PolytokenClient, PolytokenClientError, TurnInFlight
from bridge.state import (
    PinRow,
    TaskRow,
    delete_pin,
    get_pin,
    list_active_tasks,
    list_pins,
    touch_pin,
    upsert_pin,
    upsert_task,
)

if TYPE_CHECKING:
    from bridge.bot import Bot

logger = logging.getLogger(__name__)

# Per-task attachment directory for files relayed from Discord.
ATTACHMENTS_DIR = Path.home() / ".local" / "state" / "claude-discord-bridge" / "attachments"
# Delete relayed attachments older than this (startup + hourly sweep).
ATTACHMENT_TTL_SECS = int(os.environ.get("BRIDGE_ATTACHMENT_TTL_SECS", str(7 * 86400)))

# Cap on how many recent subagent actions we render in a block.
_SUBAGENT_BLOCK_MAX_ACTIONS = 5
# Don't edit a block's Discord message more often than this (rate-limit guard).
_SUBAGENT_EDIT_THROTTLE_SECS = 1.5

# Marker convention agents use to attach files back to the Discord thread.
_ATTACH_MARKER = re.compile(r"\[\[attach:\s*([^\]]+?)\s*\]\]")
_MAX_ATTACHMENTS_PER_POST = 10


@dataclass
class SubagentBlock:
    """Per-subagent live-updating Discord embed, keyed by the daemon's
    ``subagent_handle``. Edited in place as the subagent runs."""

    handle: str
    attribution: str
    started_at: float
    message_id: int | None = None
    finished_at: float | None = None
    last_edit_at: float = 0.0
    actions: list[str] = field(default_factory=list)


@dataclass
class PendingInterrogative:
    """A daemon interrogative awaiting the user's next in-thread reply."""

    interrogative_id: str
    kind: str  # "ask_user_question" | "clarification" | "confirmation"
    options: list[dict[str, str]] = field(default_factory=list)
    payload: dict = field(default_factory=dict)


def _parse_attach_markers(text: str) -> tuple[str, list[Path]]:
    """Strip ``[[attach: <path>]]`` markers and return cleaned text + paths."""
    paths: list[Path] = []
    for match in _ATTACH_MARKER.finditer(text):
        candidate = Path(match.group(1).strip())
        if candidate.is_absolute() and candidate.is_file():
            paths.append(candidate)
        else:
            logger.info("attach marker skipped (not absolute / missing): %r", str(candidate))
    cleaned = _ATTACH_MARKER.sub("", text).strip()
    return cleaned, paths[:_MAX_ATTACHMENTS_PER_POST]


def _cleanup_task_attachments(task_id: str) -> None:
    """Remove a task's relayed-attachment directory."""
    out_dir = ATTACHMENTS_DIR / task_id
    if not out_dir.exists():
        return
    try:
        for child in out_dir.iterdir():
            with contextlib.suppress(OSError):
                child.unlink()
        out_dir.rmdir()
    except OSError:
        logger.exception("failed to clean up attachments dir %s", out_dir)


def sweep_old_attachments(*, ttl_secs: int = ATTACHMENT_TTL_SECS) -> None:
    """Delete relayed attachment files older than ``ttl_secs`` and prune empty dirs."""
    if not ATTACHMENTS_DIR.exists():
        return
    cutoff = time.time() - ttl_secs
    for task_dir in ATTACHMENTS_DIR.iterdir():
        if not task_dir.is_dir():
            continue
        for f in task_dir.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                logger.exception("failed to sweep attachment %s", f)
        with contextlib.suppress(OSError):
            if not any(task_dir.iterdir()):
                task_dir.rmdir()


class TaskSpawnError(Exception):
    """Raised when a task can't be spawned."""


class TaskNotFound(Exception):
    """Raised when a task_id isn't tracked."""


class TaskRestartError(Exception):
    """Raised when a task can't be restarted."""


class _ToolSummaryAggregator:
    """Collects tool summaries within a 1s window and flushes as one Discord message.

    On 429 rate limit, enters slow mode (5s window) for the task's lifetime.
    """

    FLUSH_WINDOW = 1.0
    SLOW_FLUSH_WINDOW = 5.0

    def __init__(self, bot: "Bot", thread_id: int) -> None:
        self._bot = bot
        self._thread_id = thread_id
        self._lines: list[str] = []
        self._flush_task: asyncio.Task | None = None
        self._slow_mode = False

    def _flush_window(self) -> float:
        return self.SLOW_FLUSH_WINDOW if self._slow_mode else self.FLUSH_WINDOW

    def append(self, line: str) -> None:
        self._lines.append(line)
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_after_window())

    async def _flush_after_window(self) -> None:
        try:
            await asyncio.sleep(self._flush_window())
        except asyncio.CancelledError:
            return
        if not self._lines:
            return
        local_lines = list(self._lines)
        self._lines.clear()
        body = "\n".join(local_lines)
        try:
            await self._bot.post(body, thread_id=self._thread_id)
        except asyncio.CancelledError:
            self._lines[:0] = local_lines
            raise
        except Exception as e:
            if getattr(e, "status", None) == 429:
                logger.warning("tool summary hit 429; switching to slow mode")
                self._slow_mode = True
                self._lines.insert(0, body)
            else:
                logger.exception("failed to post tool summary chunk")

    async def flush_now(self) -> None:
        """Force flush (called on turn end to avoid orphaned summaries)."""
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
        if self._lines:
            body = "\n".join(self._lines)
            self._lines.clear()
            try:
                await self._bot.post(body, thread_id=self._thread_id)
            except Exception:
                logger.exception("failed to flush final tool summary chunk")


@dataclass
class Task:
    """An in-memory task representation."""

    task_id: str
    thread_id: int
    cwd: str
    status: str
    polytoken_session_id: str | None
    port: int | None
    created_at: int
    last_activity: int
    # Live-updating subagent embeds keyed by the daemon's subagent_handle.
    subagent_blocks: dict[str, SubagentBlock] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: TaskRow) -> "Task":
        return cls(
            task_id=row.task_id,
            thread_id=row.thread_id,
            cwd=row.cwd,
            status=row.status,
            polytoken_session_id=row.polytoken_session_id,
            port=row.port,
            created_at=row.created_at,
            last_activity=row.last_activity,
        )


class TaskRegistry:
    """In-memory registry of Polytoken-daemon tasks with DB persistence."""

    def __init__(
        self,
        conn: aiosqlite.Connection,
        bot: "Bot | None",
        supervisor: DaemonSupervisor,
    ) -> None:
        """Initialize with database connection, bot, and daemon supervisor.

        ``bot`` may be None at construction; ``server.serve`` builds the
        registry before the Bot exists and calls ``bind_bot`` later.
        """
        self._conn = conn
        self._bot = bot
        self._supervisor = supervisor
        self._by_task_id: dict[str, Task] = {}
        self._by_thread_id: dict[int, Task] = {}
        self._by_session_id: dict[str, Task] = {}
        self._typing_tasks: dict[str, asyncio.Task] = {}
        self._aggregators: dict[str, _ToolSummaryAggregator] = {}
        self._clients: dict[str, PolytokenClient] = {}
        self._consumers: dict[str, asyncio.Task] = {}
        self._translators: dict[str, Translator] = {}
        self._pending_interrogatives: dict[str, PendingInterrogative] = {}
        self._torn_down: set[str] = set()
        self._pending_startup_notices: list[dict] = []
        self._pin_spawn_locks: dict[int, asyncio.Lock] = {}

    def bind_bot(self, bot: "Bot") -> None:
        self._bot = bot

    @staticmethod
    def _notify_mention_prefix() -> str:
        """Return ``<@USER_ID> `` to ping the configured user on attention, or ''."""
        user_id = os.environ.get("BRIDGE_NOTIFY_USER_ID", "").strip()
        if not user_id.isdigit():
            return ""
        return f"<@{user_id}> "

    # -- startup / reconcile ---------------------------------------------

    async def load_from_db(self, *, reconcile_with_daemons: bool = False) -> None:
        """Restore the in-memory task map from SQLite.

        With ``reconcile_with_daemons=True`` (production startup), each active
        row is diffed against ``polytoken sessions``: a still-live daemon is
        re-attached (its event consumer restarted, port refreshed); a dead one
        is flipped to 'crashed' and a startup notice is staged.
        """
        rows = await list_active_tasks(self._conn)
        if not rows:
            return

        if not reconcile_with_daemons:
            for row in rows:
                await self._index(Task.from_row(row))
            return

        listing_ok = True
        try:
            live = {s.session_id: s for s in await self._supervisor.list_sessions()}
        except DaemonSupervisorError:
            logger.exception("failed to list polytoken sessions during recovery; keeping rows as-is")
            listing_ok = False
            live = {}

        for row in rows:
            task = Task.from_row(row)
            sid = task.polytoken_session_id
            info = live.get(sid) if sid else None
            if info is not None:
                task.port = info.port
                task.status = "running"
                await self._index(task)
                await self._persist(task)
                # Don't start the consumer here — the bot isn't bound/ready
                # yet at reconcile time. `serve` calls start_event_consumers()
                # after `bot.is_ready`.
                logger.info("recovered task %s on session %s:%d", task.task_id[:8], sid, info.port)
                continue
            if not listing_ok:
                # We couldn't confirm the daemon is gone (the registry listing
                # failed). Don't tear the task down on a transient CLI error —
                # keep it as-is and let the event consumer detect a truly-dead
                # daemon at runtime (and mark it crashed then).
                await self._index(task)
                # Consumer deferred to start_event_consumers() (bot not ready yet).
                logger.info("kept task %s as-is (session listing unavailable)", task.task_id[:8])
                continue
            # The listing succeeded and this session isn't in it — genuinely
            # gone. Mark crashed (defer Discord notices; bot not ready yet).
            task.status = "crashed"
            task.last_activity = int(time.time())
            await self._index(task)
            await self._persist(task)
            self._pending_startup_notices.append({
                "task_id": task.task_id,
                "thread_id": task.thread_id,
                "messages": ["💥 Bridge restarted; this task's daemon is gone"],
                "archive": True,
            })
            _cleanup_task_attachments(task.task_id)

    async def flush_startup_notices(self) -> None:
        """Post and archive notices staged during reconcile. Idempotent."""
        if not self._pending_startup_notices:
            return
        notices, self._pending_startup_notices = self._pending_startup_notices, []
        for notice in notices:
            for msg in notice["messages"]:
                try:
                    await self._bot.post(msg, thread_id=notice["thread_id"])
                except Exception:
                    logger.exception("failed to post startup notice for task %s", notice["task_id"])
            if notice["archive"]:
                try:
                    await self._archive_thread(notice["thread_id"])
                except Exception:
                    logger.exception("failed to archive thread for task %s", notice["task_id"])

    async def start_event_consumers(self) -> None:
        """Start a `/events` consumer for every live in-memory task.

        Called by `serve` **after** the Discord bot is bound and ready —
        consumers post to Discord, so they must not run during
        `load_from_db` reconcile (when ``self._bot`` is still ``None``).
        Idempotent: `_start_consumer` skips tasks that already have a running
        consumer, so this is safe to call once at startup.
        """
        for task in list(self._by_task_id.values()):
            if task.status in ("running", "spawning"):
                self._start_consumer(task)

    # -- lookups ----------------------------------------------------------

    def get_by_task_id(self, task_id: str) -> Task | None:
        return self._by_task_id.get(task_id)

    def get_by_thread_id(self, thread_id: int) -> Task | None:
        return self._by_thread_id.get(thread_id)

    def get_by_session_id(self, session_id: str) -> Task | None:
        return self._by_session_id.get(session_id)

    async def _index(self, task: Task) -> None:
        self._by_task_id[task.task_id] = task
        self._by_thread_id[task.thread_id] = task
        if task.polytoken_session_id:
            self._by_session_id[task.polytoken_session_id] = task

    async def _persist(self, task: Task) -> None:
        await upsert_task(
            self._conn,
            task.task_id,
            task.thread_id,
            task.cwd,
            task.status,
            polytoken_session_id=task.polytoken_session_id,
            port=task.port,
        )

    # -- per-task client + event consumer --------------------------------

    def _client_for(self, task: Task) -> PolytokenClient:
        client = self._clients.get(task.task_id)
        if client is None or client.port != task.port:
            if client is not None:
                asyncio.create_task(client.aclose())
            client = PolytokenClient(task.port)
            self._clients[task.task_id] = client
        return client

    def _start_consumer(self, task: Task) -> None:
        prev = self._consumers.get(task.task_id)
        if prev is not None and not prev.done():
            return
        self._translators.setdefault(task.task_id, Translator())
        self._consumers[task.task_id] = asyncio.create_task(
            self._consume_events(task.task_id), name=f"events-{task.task_id[:8]}"
        )

    async def _consume_events(self, task_id: str) -> None:
        """Long-lived: follow the daemon's /events, translate, render.

        Reconnects with backoff on stream drops (resuming from the last seq via
        the client's ``Last-Event-ID`` header). If the daemon has actually
        disappeared from the session registry, stop and mark the task crashed
        instead of retrying forever.
        """
        translator = self._translators.setdefault(task_id, Translator())
        backoff = 1.0
        while True:
            task = self.get_by_task_id(task_id)
            if task is None or task.status not in ("running", "spawning"):
                return
            client = self._client_for(task)
            try:
                async for env in client.stream_events(last_seq=translator.last_seq):
                    for action in translator.handle(env):
                        await self._render(task, action)
                backoff = 1.0
            except asyncio.CancelledError:
                return
            except PolytokenClientError as exc:
                logger.warning("event stream for %s dropped: %s", task_id[:8], exc)
                if await self._daemon_is_gone(task):
                    await self._handle_daemon_death(task)
                    return
            except Exception:
                logger.exception("event consumer error for %s", task_id[:8])
            await asyncio.sleep(min(backoff, 10.0))
            backoff = min(backoff * 2, 10.0)

    async def _daemon_is_gone(self, task: Task) -> bool:
        """True only if the session is confirmed absent from `polytoken sessions`.

        Returns False when we can't tell (no session id, or the registry listing
        itself failed) so the consumer keeps retrying rather than tearing down a
        possibly-live task on an inconclusive signal.
        """
        if not task.polytoken_session_id:
            return False
        try:
            return await self._supervisor.find_session(task.polytoken_session_id) is None
        except DaemonSupervisorError:
            return False

    async def _handle_daemon_death(self, task: Task) -> None:
        """The daemon for this task is gone — notify, mark crashed, tear down."""
        logger.warning("daemon for task %s is gone; marking crashed", task.task_id[:8])
        try:
            await self._bot.post(
                "💥 The session's daemon has exited — marking this task crashed. "
                "Use `/start` to begin a new one.",
                thread_id=task.thread_id,
            )
        except Exception:
            logger.exception("failed to post daemon-death notice for task %s", task.task_id[:8])
        # cancel_consumer=False: we ARE the consumer and return right after.
        await self._teardown_task(task, status="crashed", archive=True, cancel_consumer=False)

    # -- action rendering -------------------------------------------------

    async def _render(self, task: Task, action) -> None:  # noqa: C901 - flat dispatch
        try:
            if isinstance(action, AssistantText):
                if action.subagent_handle:
                    await self._subagent_activity(task, action.subagent_handle, f"• 💬 {action.text[:140]}")
                else:
                    await self._post_assistant_text(task, action.text)
            elif isinstance(action, AssistantThinking):
                if not action.subagent_handle:
                    await self._bot.post(f"💭 {action.text}", thread_id=task.thread_id)
            elif isinstance(action, ToolLine):
                self._agg_for(task).append(action.line)
            elif isinstance(action, ToolDiff):
                await self._bot.post(action.block, thread_id=task.thread_id)
            elif isinstance(action, ToolFailure):
                await self._bot.post(action.line, thread_id=task.thread_id)
            elif isinstance(action, SubagentStarted):
                await self._subagent_started(task, action)
            elif isinstance(action, SubagentActivity):
                await self._subagent_activity(task, action.handle, action.line)
            elif isinstance(action, SubagentCompleted):
                await self._subagent_completed(task, action)
            elif isinstance(action, (AskQuestion, Clarification, Confirmation)):
                await self._post_interrogative(task, action)
            elif isinstance(action, TurnStarted):
                await self._start_typing(task)
            elif isinstance(action, TurnComplete):
                await self._end_turn(task)
            elif isinstance(action, TurnCancelled):
                await self._end_turn(task)
                await self._bot.post(f"🛑 Turn cancelled ({action.reason})", thread_id=task.thread_id)
            elif isinstance(action, ModelError):
                await self._end_turn(task)
                await self._bot.post(f"⚠ Model error: {action.error[:500]}", thread_id=task.thread_id)
            elif isinstance(action, TitleChange):
                await self._rename_thread(task, action.title)
            elif isinstance(action, StatusNote):
                await self._bot.post(action.text, thread_id=task.thread_id)
            elif isinstance(action, AttentionPing):
                await self._bot.post(
                    f"{self._notify_mention_prefix()}🔔 {action.summary}", thread_id=task.thread_id
                )
            elif isinstance(action, ImageResolved):
                logger.info("image reference resolved for %s: %s", task.task_id[:8], action.path)
            elif isinstance(action, StateRefresh):
                pass  # nothing to mirror eagerly today
            elif isinstance(action, Reconcile):
                await self._handle_reconcile(task, action.reason)
        except Exception:
            logger.exception("failed to render %s for task %s", type(action).__name__, task.task_id[:8])

    async def _handle_reconcile(self, task: Task, reason: str) -> None:
        """Surface an event-stream gap to the user and re-sync session state.

        The daemon retains durable history, but this bridge does not yet replay
        missed `/events` frames item-by-item — so rather than silently drop
        possibly-important output (assistant text, tool results, a question),
        we make the gap visible and re-sync the cheap state (title/todos via
        `/state`). Connection drops are already recovered by `Last-Event-ID`
        resume on reconnect; this path is for in-stream `stream_discontinuity`
        / seq jumps.
        """
        logger.warning("event gap for task %s: %s", task.task_id[:8], reason)
        try:
            await self._bot.post(
                "⚠️ Detected a gap in the daemon's event stream — some intermediate "
                "output may be missing. Re-syncing session state.",
                thread_id=task.thread_id,
            )
        except Exception:
            logger.exception("failed to post reconcile notice for task %s", task.task_id[:8])
        state = await self.get_state(task.task_id)
        if state:
            title = (state.get("session_title") or "").strip()
            if title:
                await self._rename_thread(task, title)

    async def _post_assistant_text(self, task: Task, text: str) -> None:
        cleaned, attach_paths = _parse_attach_markers(text)
        if attach_paths:
            await self._bot.post_with_attachments(
                [str(p) for p in attach_paths], thread_id=task.thread_id, text=cleaned or None
            )
        elif cleaned:
            await self._bot.post(cleaned, thread_id=task.thread_id)

    # -- subagent embeds --------------------------------------------------

    async def _subagent_started(self, task: Task, action: SubagentStarted) -> None:
        block = SubagentBlock(
            handle=action.handle,
            attribution=action.subagent_type or action.handle,
            started_at=time.time(),
        )
        task.subagent_blocks[action.handle] = block
        embed = self._render_subagent_embed(block, [], 0, finished=False)
        try:
            block.message_id = await self._bot.post_embed(embed, thread_id=task.thread_id)
            block.last_edit_at = time.time()
        except Exception:
            logger.exception("failed to post subagent embed for %s", action.handle)

    async def _subagent_activity(self, task: Task, handle: str, line: str) -> None:
        block = task.subagent_blocks.get(handle)
        if block is None:
            block = SubagentBlock(handle=handle, attribution=handle, started_at=time.time())
            task.subagent_blocks[handle] = block
            with contextlib.suppress(Exception):
                block.message_id = await self._bot.post_embed(
                    self._render_subagent_embed(block, [], 0, finished=False), thread_id=task.thread_id
                )
        block.actions.append(line)
        await self._maybe_edit_subagent(task, block, finished=False)

    async def _subagent_completed(self, task: Task, action: SubagentCompleted) -> None:
        block = task.subagent_blocks.get(action.handle)
        if block is None:
            return
        block.finished_at = time.time()
        if action.result_summary:
            block.actions.append(f"• ✅ {action.result_summary[:140]}")
        await self._maybe_edit_subagent(task, block, finished=True, force=True)

    async def _maybe_edit_subagent(self, task: Task, block: SubagentBlock, *, finished: bool, force: bool = False) -> None:
        now = time.time()
        if not force and now - block.last_edit_at < _SUBAGENT_EDIT_THROTTLE_SECS:
            return
        if block.message_id is None:
            return
        embed = self._render_subagent_embed(
            block, block.actions[-_SUBAGENT_BLOCK_MAX_ACTIONS:], len(block.actions), finished=finished
        )
        try:
            await self._bot.edit_message(task.thread_id, block.message_id, embed=embed)
            block.last_edit_at = now
        except Exception:
            logger.exception("failed to edit subagent embed for %s", block.handle)

    def _render_subagent_embed(self, block: SubagentBlock, last_actions: list[str], total_actions: int, finished: bool) -> discord.Embed:
        status = "finished" if finished else "running"
        end = (block.finished_at if finished else None) or time.time()
        elapsed = end - block.started_at
        dur = f"{elapsed:.0f}s" if elapsed < 60 else f"{elapsed / 60:.1f}m"
        color = 0x57F287 if finished else 0xFEE75C
        description = "\n".join(last_actions)
        if len(description) > 3900:
            description = description[:3897] + "…"
        embed = discord.Embed(
            title=f"🤖 {block.attribution}",
            description=description or "_(no actions yet)_",
            color=color,
        )
        embed.set_footer(text=f"{status} · {total_actions} actions · {dur}")
        return embed

    # -- interrogatives ---------------------------------------------------

    async def _post_interrogative(self, task: Task, action) -> None:
        prefix = self._notify_mention_prefix()
        if isinstance(action, Confirmation):
            self._pending_interrogatives[task.task_id] = PendingInterrogative(
                interrogative_id=action.interrogative_id, kind="confirmation"
            )
            await self._bot.post(f"{prefix}❓ {action.question}\nReply **yes** or **no**.", thread_id=task.thread_id)
            return
        if isinstance(action, Clarification):
            self._pending_interrogatives[task.task_id] = PendingInterrogative(
                interrogative_id=action.interrogative_id, kind="clarification", options=action.options
            )
            lines = [f"{prefix}❓ {action.question}"]
            for i, opt in enumerate(action.options, 1):
                lines.append(f"  {i}. {opt.get('label') or opt.get('key')}")
            lines.append("_Reply with a number, the option text, or your own answer._")
            await self._bot.post("\n".join(lines), thread_id=task.thread_id)
            return
        # AskQuestion
        questions = action.payload.get("questions") or []
        self._pending_interrogatives[task.task_id] = PendingInterrogative(
            interrogative_id=action.interrogative_id, kind="ask_user_question", payload=action.payload
        )
        lines = [f"{prefix}❓ Claude is asking:"]
        for q in questions:
            lines.append(f"**{q.get('question', '')}**")
            ctx = q.get("context")
            if ctx:
                lines.append(ctx[:1500])
            for i, opt in enumerate(q.get("options") or [], 1):
                lines.append(f"  {i}. {opt.get('label', '')} — {opt.get('description', '')}".rstrip(" —"))
        lines.append("_Reply with a number or free text._")
        await self._bot.post("\n".join(lines)[:1900], thread_id=task.thread_id)

    async def _answer_interrogative(self, task: Task, pending: PendingInterrogative, text: str) -> None:
        client = self._client_for(task)
        text = text.strip()
        if pending.kind == "confirmation":
            confirmed = text.lower() in ("y", "yes", "ok", "confirm", "true", "👍")
            response = {"kind": "confirmation_answer", "confirmed": confirmed}
        elif pending.kind == "clarification":
            response = self._clarification_response(pending, text)
        else:
            response = self._ask_question_response(pending, text)
        try:
            await client.respond_interrogative(pending.interrogative_id, response)
        except PolytokenClientError:
            logger.exception("failed to respond to interrogative for %s", task.task_id[:8])
            await self._bot.post("⚠ Failed to deliver your answer to the daemon.", thread_id=task.thread_id)

    @staticmethod
    def _clarification_response(pending: PendingInterrogative, text: str) -> dict:
        # Numeric pick.
        if text.isdigit():
            idx = int(text) - 1
            if 0 <= idx < len(pending.options):
                return {"kind": "clarification_choice", "choice": pending.options[idx].get("key", "")}
        # Match by label or key (case-insensitive).
        low = text.lower()
        for opt in pending.options:
            if low in (opt.get("label", "").lower(), opt.get("key", "").lower()):
                return {"kind": "clarification_choice", "choice": opt.get("key", "")}
        return {"kind": "clarification_text", "text": text}

    @staticmethod
    def _ask_question_response(pending: PendingInterrogative, text: str) -> dict:
        questions = pending.payload.get("questions") or []
        answers = []
        for q in questions:
            qid = q.get("id", "")
            options = q.get("options") or []
            reply: dict = {"question_id": qid}
            if text.isdigit() and options:
                idx = int(text) - 1
                if 0 <= idx < len(options):
                    reply["selected_option_ids"] = [options[idx].get("id", "")]
                else:
                    reply["free_text"] = text
            else:
                matched = next(
                    (o for o in options if text.lower() == o.get("label", "").lower()), None
                )
                if matched is not None:
                    reply["selected_option_ids"] = [matched.get("id", "")]
                else:
                    reply["free_text"] = text
            answers.append(reply)
        return {"kind": "ask_user_question_answers", "answers": answers}

    # -- typing + turn lifecycle -----------------------------------------

    def _agg_for(self, task: Task) -> _ToolSummaryAggregator:
        agg = self._aggregators.get(task.task_id)
        if agg is None:
            agg = _ToolSummaryAggregator(self._bot, task.thread_id)
            self._aggregators[task.task_id] = agg
        return agg

    async def _start_typing(self, task: Task) -> None:
        prev = self._typing_tasks.pop(task.task_id, None)
        if prev is not None and not prev.done():
            prev.cancel()
        self._typing_tasks[task.task_id] = asyncio.create_task(
            self._run_typing(task), name=f"typing-{task.task_id[:8]}"
        )

    async def _run_typing(self, task: Task) -> None:
        try:
            channel = await self._bot.fetch_messageable(task.thread_id)
            async with channel.typing():
                await asyncio.Future()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("typing indicator failed for task %s", task.task_id)

    async def _stop_typing(self, task_id: str) -> None:
        t = self._typing_tasks.pop(task_id, None)
        if t is not None and not t.done():
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

    async def _end_turn(self, task: Task) -> None:
        await self._stop_typing(task.task_id)
        agg = self._aggregators.get(task.task_id)
        if agg is not None:
            await agg.flush_now()

    async def _archive_thread(self, thread_id: int) -> None:
        try:
            await self._bot.archive_thread(thread_id)
        except Exception:
            logger.exception("Failed to archive thread %d", thread_id)

    async def _rename_thread(self, task: Task, title: str) -> None:
        if not title:
            return
        try:
            await self._bot.rename_thread(task.thread_id, title[:90])
        except Exception:
            logger.exception("failed to rename thread for task %s", task.task_id[:8])

    # -- spawn / routing --------------------------------------------------

    async def spawn_task(
        self,
        cwd: str,
        *,
        prompt: str | None = None,
        channel_id: int | None = None,
    ) -> Task:
        """Spawn a new Polytoken daemon session bound to a Discord thread.

        The ``prompt`` parameter is accepted but not sent here — ``commands.py``
        calls ``write_initial_prompt`` separately after the thread exists.
        """
        if not Path(cwd).is_dir():
            raise TaskSpawnError(f"cwd does not exist: {cwd}")

        task_id = str(uuid.uuid4())
        if channel_id is not None:
            thread_id = channel_id
        else:
            thread_id = await self._bot.create_thread(name=f"cc · {Path(cwd).name} · {task_id[:8]}")

        now = int(time.time())
        await upsert_task(self._conn, task_id, thread_id, cwd, "spawning", now=now)

        try:
            result = await self._supervisor.spawn(cwd)
        except DaemonSupervisorError:
            logger.exception("spawn_task failed for task_id %s", task_id)
            await upsert_task(self._conn, task_id, thread_id, cwd, "crashed")
            _cleanup_task_attachments(task_id)
            try:
                await self._bot.post(
                    "💥 Task failed to spawn — couldn't start a Polytoken daemon. "
                    "Check the bridge log for details.",
                    thread_id=thread_id,
                )
            except Exception:
                logger.exception("failed to post spawn-failure notice for task %s", task_id)
            if channel_id is None:
                await self._archive_thread(thread_id)
            raise TaskSpawnError("polytoken daemon failed to spawn")

        task = Task(
            task_id=task_id,
            thread_id=thread_id,
            cwd=cwd,
            status="running",
            polytoken_session_id=result.session_id,
            port=result.port,
            created_at=now,
            last_activity=now,
        )
        await self._index(task)
        await self._persist(task)
        self._start_consumer(task)
        logger.info("spawned task %s → session %s:%d", task_id[:8], result.session_id, result.port)
        return task

    async def write_initial_prompt(self, task_id: str, prompt: str) -> None:
        """Send the initial prompt to a freshly-spawned task's daemon."""
        task = self.get_by_task_id(task_id)
        if task is None or task.port is None:
            logger.warning("write_initial_prompt: task %s not ready", task_id)
            return
        await self._prompt(task, prompt)

    async def _prompt(self, task: Task, content: str) -> bool:
        """Submit a prompt to the daemon; surface 409/transport failures."""
        client = self._client_for(task)
        try:
            await client.prompt(content)
            task.last_activity = int(time.time())
            await self._persist(task)
            return True
        except TurnInFlight:
            await self._bot.post(
                "⏳ A turn is already running — wait for it to finish (or `/stop`).",
                thread_id=task.thread_id,
            )
            return False
        except PolytokenClientError as exc:
            logger.warning("prompt failed for %s: %s", task.task_id[:8], exc)
            await self._bot.post(
                f"⚠ Couldn't deliver your message to the daemon (`{exc}`). It may have crashed; try `/kill`.",
                thread_id=task.thread_id,
            )
            return False

    async def maybe_route_message(self, msg: MessageLike) -> bool:
        """Route a Discord message in a task-bound thread to its daemon.

        Returns True if handled (consumed), False to fall through.
        """
        thread_id = msg.channel.id
        task = self.get_by_thread_id(thread_id)

        if task is None or task.status not in ("running", "spawning"):
            spawned = await self._maybe_spawn_for_pinned(thread_id)
            if spawned is not None:
                task = spawned

        if task is None:
            return False
        if task.status not in ("running", "spawning") or task.port is None:
            return True  # bound but not live — silent ignore

        text = (msg.content or "").rstrip()
        attachment_paths: list[Path] = []
        if msg.attachments:
            attachment_paths = await self._save_attachments(task.task_id, msg)

        voice_paths = [p for p in attachment_paths if voice.is_audio_path(p)]
        other_paths = [p for p in attachment_paths if not voice.is_audio_path(p)]

        voice_segments: list[str] = []
        for p in voice_paths:
            transcript_text = await voice.transcribe(p)
            if transcript_text:
                voice_segments.append(f"[voice memo] {transcript_text}")
            else:
                voice_segments.append(
                    f"[voice memo received — transcription unavailable; raw file: {p}]"
                )

        if not text and not voice_segments and not other_paths:
            return True

        # A pending interrogative consumes the next plain-text reply.
        pending = self._pending_interrogatives.pop(task.task_id, None)
        if pending is not None and text and not other_paths and not voice_segments:
            await self._answer_interrogative(task, pending, text)
            return True
        if pending is not None:
            # Non-text reply: re-stash so a later text reply still answers.
            self._pending_interrogatives[task.task_id] = pending

        parts: list[str] = []
        if text:
            parts.append(text)
        parts.extend(voice_segments)
        # File attachments become @-references the daemon resolves.
        for p in other_paths:
            parts.append(f"@{p}")
        combined = " ".join(parts)

        logger.info("relay → daemon (task=%s, %d chars)", task.task_id[:8], len(combined))
        await self._prompt(task, combined)
        return True

    async def _maybe_spawn_for_pinned(self, channel_id: int) -> Task | None:
        """If ``channel_id`` is pinned and has no live task, spawn one."""
        pin = await get_pin(self._conn, channel_id)
        if pin is None:
            return None
        lock = self._pin_spawn_locks.setdefault(channel_id, asyncio.Lock())
        async with lock:
            existing = self.get_by_thread_id(channel_id)
            if existing is not None and existing.status in ("running", "spawning"):
                return existing
            logger.info("pinned auto-spawn: channel=%s cwd=%s", channel_id, pin.cwd)
            try:
                task = await self.spawn_task(cwd=pin.cwd, channel_id=channel_id)
            except TaskSpawnError as e:
                logger.error("pinned auto-spawn failed for channel %s: %s", channel_id, e)
                return None
            await touch_pin(self._conn, channel_id)
            return task

    async def _save_attachments(self, task_id: str, msg: MessageLike) -> list[Path]:
        """Download Discord attachments under ATTACHMENTS_DIR/<task_id>/."""
        out_dir = ATTACHMENTS_DIR / task_id
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception("failed to create attachments dir %s", out_dir)
            return []
        msg_id = getattr(msg, "id", None) or int(time.time() * 1000)
        saved: list[Path] = []
        for i, att in enumerate(msg.attachments or []):
            raw_name = getattr(att, "filename", None) or f"att-{i}"
            safe_name = Path(raw_name).name or f"att-{i}"
            local = out_dir / f"{msg_id}-{safe_name}"
            try:
                data = await att.read()
                # Write off-loop: a large attachment must not block the shared
                # asyncio loop (which serves both HTTP and the Discord gateway).
                await asyncio.to_thread(local.write_bytes, data)
                saved.append(local)
            except Exception:
                logger.exception("failed to save attachment %s", raw_name)
        return saved

    # -- pins -------------------------------------------------------------

    async def pin_channel(self, channel_id: int, cwd: str) -> None:
        if not Path(cwd).is_dir():
            raise ValueError(f"cwd does not exist: {cwd}")
        await upsert_pin(self._conn, channel_id, cwd)

    async def unpin_channel(self, channel_id: int) -> bool:
        return await delete_pin(self._conn, channel_id)

    async def get_pin_for(self, channel_id: int) -> PinRow | None:
        return await get_pin(self._conn, channel_id)

    async def list_all_pins(self) -> list[PinRow]:
        return await list_pins(self._conn)

    # -- skills / effort / rename ----------------------------------------

    async def invoke_skill(self, task_id: str, skill_name: str, args: str | None = None) -> None:
        """Invoke a skill by sending an ``@<skill>`` reference prompt to the daemon."""
        task = self.get_by_task_id(task_id)
        if task is None:
            raise TaskNotFound(f"task {task_id[:8]} not found")
        if task.port is None:
            raise TaskSpawnError(f"task {task_id[:8]} isn't ready yet")
        content = f"@{skill_name}"
        if args:
            content += f" {args}"
        await self._prompt(task, content)

    async def set_effort(self, task_id: str, level: str) -> None:
        """Change the session's reasoning effort by re-selecting the active model."""
        task = self.get_by_task_id(task_id)
        if task is None:
            raise TaskNotFound(f"task {task_id[:8]} not found")
        if task.port is None:
            raise TaskSpawnError(f"task {task_id[:8]} isn't ready yet")
        client = self._client_for(task)
        try:
            state = await client.state()
            model = state.get("active_model")
            if not model:
                raise TaskSpawnError("active model unknown; cannot set effort")
            await client.set_model(model, reasoning_effort=level)
        except PolytokenClientError as exc:
            raise TaskSpawnError(f"daemon rejected effort change: {exc}") from exc
        task.last_activity = int(time.time())
        await self._persist(task)

    async def set_model(self, task_id: str, model: str, *, reasoning_effort: str | None = None) -> None:
        """Switch the session's active model (optionally setting reasoning effort).

        A bare model switch resets reasoning effort to the model's default;
        pass ``reasoning_effort`` to set it in the same call.
        """
        task = self.get_by_task_id(task_id)
        if task is None:
            raise TaskNotFound(f"task {task_id[:8]} not found")
        if task.port is None:
            raise TaskSpawnError(f"task {task_id[:8]} isn't ready yet")
        client = self._client_for(task)
        try:
            await client.set_model(model, reasoning_effort=reasoning_effort)
        except PolytokenClientError as exc:
            raise TaskSpawnError(f"daemon rejected model change: {exc}") from exc
        task.last_activity = int(time.time())
        await self._persist(task)

    async def set_facet(self, task_id: str, facet: str) -> None:
        """Switch the session's active facet."""
        task = self.get_by_task_id(task_id)
        if task is None:
            raise TaskNotFound(f"task {task_id[:8]} not found")
        if task.port is None:
            raise TaskSpawnError(f"task {task_id[:8]} isn't ready yet")
        client = self._client_for(task)
        try:
            await client.set_facet(facet)
        except PolytokenClientError as exc:
            raise TaskSpawnError(f"daemon rejected facet change: {exc}") from exc
        task.last_activity = int(time.time())
        await self._persist(task)

    async def list_models(self) -> list[str]:
        """Return the selectable model names (config-level, for autocomplete)."""
        try:
            return await self._supervisor.list_models()
        except DaemonSupervisorError:
            logger.warning("failed to list polytoken models")
            return []

    async def generate_thread_name(self, task_id: str, *, timeout: float = 30.0) -> str | None:
        """Return the daemon's current session title (it auto-titles sessions)."""
        task = self.get_by_task_id(task_id)
        if task is None or task.port is None:
            return None
        client = self._client_for(task)
        try:
            state = await client.state()
        except PolytokenClientError:
            return None
        title = (state.get("session_title") or "").strip()
        return title or None

    async def get_state(self, task_id: str) -> dict | None:
        """Return the daemon ``/state`` snapshot for a task, or None if unavailable."""
        task = self.get_by_task_id(task_id)
        if task is None or task.port is None:
            return None
        client = self._client_for(task)
        try:
            return await client.state()
        except PolytokenClientError:
            logger.warning("failed to read state for task %s", task_id[:8])
            return None

    # -- listing / lifecycle ---------------------------------------------

    async def list_tasks(self) -> list[Task]:
        return sorted(
            (t for t in self._by_task_id.values() if t.status in ("spawning", "running")),
            key=lambda t: t.last_activity,
            reverse=True,
        )

    async def stop_task(self, task_id: str, *, timeout: float = 5.0) -> bool:
        """Stop a task: cancel any in-flight turn, then terminate the daemon."""
        task = self.get_by_task_id(task_id)
        if task is None:
            raise TaskNotFound(task_id)
        if task.status not in {"running", "spawning"}:
            return True
        client = self._client_for(task)
        with contextlib.suppress(PolytokenClientError):
            await client.cancel_turn()
        with contextlib.suppress(PolytokenClientError):
            await client.terminate()
        await self._teardown_task(task, status="stopped", archive=True)
        return True

    async def kill_task(self, task_id: str) -> None:
        """Immediately terminate a task's daemon and mark it crashed."""
        task = self.get_by_task_id(task_id)
        if task is None:
            raise TaskNotFound(task_id)
        if task.status not in {"running", "spawning"}:
            return
        client = self._client_for(task)
        with contextlib.suppress(PolytokenClientError):
            await client.terminate()
        await self._teardown_task(task, status="crashed", archive=True)

    async def restart_task(self, task_id: str) -> Task:
        """Not supported with the Polytoken daemon backend."""
        if self.get_by_task_id(task_id) is None:
            raise TaskNotFound(task_id)
        raise TaskRestartError(
            "Restart isn't supported with the Polytoken daemon backend; "
            "use /kill to end this task and /start to begin a new one."
        )

    async def _teardown_task(self, task: Task, *, status: str, archive: bool, cancel_consumer: bool = True) -> None:
        """Common terminal path for stop/kill: stop consumer, persist, archive, clean up.

        Idempotent: the first terminal transition wins. A later teardown (e.g.
        `_handle_daemon_death` racing a user `/stop`) is a no-op, so the status
        and side effects (archive, cleanup) can't be duplicated or overwritten.
        """
        if task.task_id in self._torn_down:
            return
        self._torn_down.add(task.task_id)
        task.status = status
        task.last_activity = int(time.time())
        await self._persist(task)

        consumer = self._consumers.pop(task.task_id, None)
        # ``cancel_consumer=False`` when called from within the consumer itself
        # (daemon-death path) — it can't cancel-and-await itself.
        if cancel_consumer and consumer is not None and not consumer.done():
            consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await consumer
        self._translators.pop(task.task_id, None)
        await self._stop_typing(task.task_id)
        agg = self._aggregators.pop(task.task_id, None)
        if agg is not None:
            await agg.flush_now()
        self._pending_interrogatives.pop(task.task_id, None)
        client = self._clients.pop(task.task_id, None)
        if client is not None:
            await client.aclose()

        self._by_thread_id.pop(task.thread_id, None)
        if task.polytoken_session_id is not None:
            self._by_session_id.pop(task.polytoken_session_id, None)

        if archive:
            await self._archive_thread(task.thread_id)
        _cleanup_task_attachments(task.task_id)

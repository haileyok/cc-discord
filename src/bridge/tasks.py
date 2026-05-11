"""Task and TaskRegistry for managing discord-driven sessions."""

from __future__ import annotations

import asyncio
import contextlib
import json
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

import bridge as _bridge_pkg
from bridge import tool_summary, transcript, usage, voice
from bridge.bot import BotNotReady
from bridge.listener import MessageLike
from bridge.state import TaskRow, list_active_tasks, upsert_task
from bridge.zellij import ZellijError, ZellijManager

if TYPE_CHECKING:
    from bridge.approvals import ApprovalRouter
    from bridge.bot import Bot

logger = logging.getLogger(__name__)

# Task-scoped settings directory
TASK_SETTINGS_DIR = Path.home() / ".local" / "state" / "cc-bridge" / "task-settings"
# Per-task attachment directory for files relayed from Discord.
ATTACHMENTS_DIR = Path.home() / ".local" / "state" / "cc-bridge" / "attachments"

# Hook scripts directory — resolved at import time for test monkeypatch support
HOOKS_DIR = Path(_bridge_pkg.__file__).parent.parent.parent / "hooks"

# Cap on how many recent subagent actions we render in a block.
_SUBAGENT_BLOCK_MAX_ACTIONS = 5
# Don't edit a block's Discord message more often than this; coalesces
# bursty subagent activity into chunkier updates and keeps us under
# Discord's per-channel edit rate limit (~5 edits / 5s).
_SUBAGENT_EDIT_THROTTLE_SECS = 1.5

# Marker convention agents use to attach files back to the Discord thread.
# Example: `[[attach: /tmp/screenshot.png]]` in a streamed text block.
_ATTACH_MARKER = re.compile(r"\[\[attach:\s*([^\]]+?)\s*\]\]")
# Discord per-message attachment cap.
_MAX_ATTACHMENTS_PER_POST = 10
# claude session ids are uuid4 — reject any other shape on ingest so a
# malformed value can't reach argv (claude --resume <id>) or the KDL
# layout (interpolated as an env(1) arg).
_SESSION_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass
class SubagentBlock:
    """Per-subagent live-updating Discord message: tracks state for one
    subagent (identified by `agent_id`) so we can edit a single message in
    place as the subagent runs, instead of streaming each tool call as its
    own message.
    """

    agent_id: str
    attribution: str  # e.g. "ed3d-research-agents:internet-researcher"
    started_at: float
    message_id: int | None = None
    finished_at: float | None = None
    last_entry_uuid: str | None = None  # change-detection key
    last_edit_at: float = 0.0  # throttle edits


def _parse_attach_markers(text: str) -> tuple[str, list[Path]]:
    """Strip `[[attach: <path>]]` markers from text and return the cleaned
    text plus the list of resolved file paths (must be absolute & exist)."""
    paths: list[Path] = []
    for match in _ATTACH_MARKER.finditer(text):
        candidate = Path(match.group(1).strip())
        if candidate.is_absolute() and candidate.is_file():
            paths.append(candidate)
        else:
            logger.info("attach marker skipped (not absolute / missing): %r", str(candidate))
    cleaned = _ATTACH_MARKER.sub("", text).strip()
    return cleaned, paths[:_MAX_ATTACHMENTS_PER_POST]


def _write_task_settings(
    task_id: str, *, settings_dir: Path = TASK_SETTINGS_DIR, hooks_dir: Path = HOOKS_DIR
) -> Path:
    """Generate the task-scoped settings JSON. Returns the absolute path written.

    Registers `event.py` for the observability events. PreToolUse is
    registered ONLY for `AskUserQuestion` and `ExitPlanMode` so the bridge
    can intercept those interactive prompts (the hook body carries the
    full structured `tool_input`, which we can post to Discord with proper
    options/reactions before the tool blocks for TUI input). All other
    PreToolUse traffic is left unhooked so the user's permission mode
    (typically `defaultMode: "auto"`) drives approvals via Claude Code's
    own classifier.
    """
    settings_dir.mkdir(parents=True, exist_ok=True)
    out_path = settings_dir / f"{task_id}.json"
    event_script = str(hooks_dir / "event.py")

    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "AskUserQuestion",
                    "hooks": [{"type": "command", "command": event_script}],
                },
                {
                    "matcher": "ExitPlanMode",
                    "hooks": [{"type": "command", "command": event_script}],
                },
            ],
            "SessionStart": [
                {"matcher": "*", "hooks": [{"type": "command", "command": event_script}]},
            ],
            "UserPromptSubmit": [
                {"matcher": "*", "hooks": [{"type": "command", "command": event_script}]},
            ],
            "PostToolUse": [
                {"matcher": "*", "hooks": [{"type": "command", "command": event_script}]},
            ],
            "PostToolUseFailure": [
                {"matcher": "*", "hooks": [{"type": "command", "command": event_script}]},
            ],
            "Stop": [
                {"matcher": "*", "hooks": [{"type": "command", "command": event_script}]},
            ],
            "Notification": [
                {"matcher": "*", "hooks": [{"type": "command", "command": event_script}]},
            ],
            "SessionEnd": [
                {"matcher": "*", "hooks": [{"type": "command", "command": event_script}]},
            ],
        }
    }

    out_path.write_text(json.dumps(settings, indent=2))
    return out_path


def _write_task_layout(
    task_id: str,
    *,
    env: dict[str, str],
    claude_argv: list[str],
    tab_name: str,
    settings_dir: Path = TASK_SETTINGS_DIR,
) -> Path:
    """Generate a zellij KDL layout file that opens the new tab with a
    single pane running `env K=V ... claude <argv>`. Used so the tab spawns
    with claude as the only pane — no default shell pane to close
    afterward.

    The layout wraps the pane in `tab name="..."` so the tab gets the
    bridge-expected name (`cc-<task_id_prefix>`), since `new-tab --layout`
    sources the tab name from the layout when the layout's outer node is
    `tab`. `close_on_exit` is a CHILD node on the pane — zellij's KDL
    treats `close_on_exit=true` as an attribute on a different syntax
    branch and the boolean child is what actually gates the tab close.

    Returns the absolute path written.
    """
    settings_dir.mkdir(parents=True, exist_ok=True)
    out_path = settings_dir / f"{task_id}.kdl"

    def _kdl_quote(s: str) -> str:
        # KDL strings are double-quoted. We backslash-escape \ and ".
        # Control bytes (NUL, BS, TAB, LF, CR, ESC, etc.) would either
        # break out of the string or — once zellij's KDL parser unescapes
        # them — get passed through env(1) into claude's argv with their
        # embedded form intact. None of our legitimate values (env vars,
        # tab name, claude argv) ever need a control byte, so strip them
        # rather than escape them. Defense-in-depth: callers also
        # validate session_id at ingest, but this keeps us safe even if
        # a new field flows in unsanitized later.
        cleaned = "".join(ch for ch in s if ch >= " " or ch == " ")
        return '"' + cleaned.replace("\\", "\\\\").replace('"', '\\"') + '"'

    args_tokens: list[str] = []
    for k, v in env.items():
        args_tokens.append(_kdl_quote(f"{k}={v}"))
    args_tokens.append(_kdl_quote("claude"))
    for arg in claude_argv:
        args_tokens.append(_kdl_quote(arg))
    args_line = " ".join(args_tokens)

    layout = (
        "layout {\n"
        f'    tab name={_kdl_quote(tab_name)} focus=true {{\n'
        '        pane command="env" {\n'
        f"            args {args_line}\n"
        "            close_on_exit true\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    out_path.write_text(layout)
    return out_path


def _cleanup_task_settings(task_id: str, *, settings_dir: Path = TASK_SETTINGS_DIR) -> None:
    """Remove the task-scoped settings + layout files. Idempotent — silent on missing files."""
    for suffix in (".json", ".kdl"):
        p = settings_dir / f"{task_id}{suffix}"
        try:
            p.unlink()
        except FileNotFoundError:
            continue
        except Exception:
            logger.exception("failed to remove task file %s", p)


def _cleanup_task_attachments(
    task_id: str, *, attachments_dir: Path = ATTACHMENTS_DIR
) -> None:
    """Remove the per-task attachment directory. Idempotent — silent on missing dir."""
    import shutil

    d = attachments_dir / task_id
    if not d.exists():
        return
    try:
        shutil.rmtree(d)
    except Exception:
        logger.exception("failed to remove attachments dir %s", d)


def _cleanup_task_artifacts(task_id: str) -> None:
    """Remove all on-disk state for a finished task: settings file + attachments.

    Used at every lifecycle terminal (stop / kill / crash / archive) so the two
    artifacts can't drift apart. Both helpers are idempotent.
    """
    _cleanup_task_settings(task_id)
    _cleanup_task_attachments(task_id)


def sweep_old_attachments(
    *,
    attachments_dir: Path = ATTACHMENTS_DIR,
    ttl_secs: int | None = None,
) -> None:
    """Walk the attachments root and remove any file older than `ttl_secs`.

    Empty per-task dirs are removed too. Defaults to BRIDGE_ATTACHMENT_TTL_SECS
    env var (fallback: 7 days). Best-effort — failures are logged and skipped
    so a single bad file doesn't abort the sweep.
    """
    if ttl_secs is None:
        try:
            ttl_secs = int(os.environ.get("BRIDGE_ATTACHMENT_TTL_SECS") or 7 * 24 * 3600)
        except ValueError:
            ttl_secs = 7 * 24 * 3600
    if not attachments_dir.exists():
        return
    cutoff = time.time() - ttl_secs
    removed = 0
    for task_dir in attachments_dir.iterdir():
        if not task_dir.is_dir():
            continue
        for f in task_dir.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                logger.exception("failed to remove stale attachment %s", f)
        try:
            task_dir.rmdir()  # only succeeds when empty
        except OSError:
            pass
    if removed:
        logger.info("attachment sweep removed %d stale file(s)", removed)


class TaskSpawnError(Exception):
    """Raised when a task cannot be spawned."""

    pass


class TaskNotFound(Exception):
    """Raised when a task cannot be found by its ID."""

    pass


class TaskRestartError(Exception):
    """Raised when a task cannot be restarted (e.g., no session to resume)."""

    pass


class _ToolSummaryAggregator:
    """Collects PostToolUse summaries within a 1s window and flushes as one Discord message.

    On 429 rate limit, enters slow mode (5s window) for the task's lifetime.
    """

    FLUSH_WINDOW = 1.0  # seconds
    SLOW_FLUSH_WINDOW = 5.0  # seconds (when rate-limited)

    def __init__(self, bot: Bot, thread_id: str) -> None:
        self._bot = bot
        self._thread_id = thread_id
        self._lines: list[str] = []
        self._flush_task: asyncio.Task | None = None
        self._slow_mode = False  # True after we hit a 429

    def _flush_window(self) -> float:
        """Return appropriate flush window based on rate-limit status."""
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
        # Snapshot lines before clearing so we can restore if the post is cancelled
        local_lines = list(self._lines)
        self._lines.clear()
        body = "\n".join(local_lines)
        try:
            await self._bot.post(body, thread_id=self._thread_id)
        except asyncio.CancelledError:
            # Restore lines so flush_now can re-emit them
            self._lines[:0] = local_lines
            raise
        except Exception as e:
            # Check for 429 (rate limit)
            if getattr(e, "status", None) == 429:
                logger.warning("tool summary hit 429; switching to slow mode")
                self._slow_mode = True
                # Re-queue the body for retry
                self._lines.insert(0, body)
            else:
                logger.exception("failed to post tool summary chunk")

    async def flush_now(self) -> None:
        """Force flush (called on Stop / SessionEnd to avoid orphaned summaries)."""
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
    thread_id: str
    zellij_pane_id: str | None
    cwd: str
    status: str
    current_claude_session_id: str | None
    current_transcript_path: str | None
    created_at: int
    last_activity: int
    # Assistant-entry uuids whose content has already been streamed to Discord
    # within the current turn. Cleared on UserPromptSubmit / SessionStart so a
    # new turn starts fresh; not persisted across bridge restarts.
    posted_assistant_uuids: set[str] = field(default_factory=set)
    # Live-updating subagent blocks keyed by Claude's `agentId`. Cleared on
    # UserPromptSubmit / SessionStart.
    subagent_blocks: dict[str, "SubagentBlock"] = field(default_factory=dict)
    # Mirror of claude's session task list, keyed by claude's task id.
    # Each entry: {"subject", "status", "owner", "description"}. Updated
    # incrementally on TaskCreate/TaskUpdate, snapshot-replaced on TaskList.
    # Cleared on SessionStart so a fresh session starts empty.
    task_list_state: dict[str, dict] = field(default_factory=dict)
    # Debounce handle so a burst of TaskCreate/TaskUpdate events coalesces
    # into a single Discord post 1s after the last event.
    task_list_post_timer: asyncio.Task | None = None
    # Discord message id of the most recent task-list post. If it's still
    # the last message in the thread when the next post fires, we edit
    # it in place instead of posting a duplicate.
    last_task_list_message_id: int | None = None
    # Current status indicator emoji prefixed onto the thread name. Empty
    # string means "not set yet" (so the first state transition takes effect
    # even if the thread name happens to already start with a green dot).
    status_indicator: str = ""

    @classmethod
    def from_row(cls, row: TaskRow) -> "Task":
        """Convert a frozen TaskRow to a mutable Task."""
        return cls(
            task_id=row.task_id,
            thread_id=row.thread_id,
            zellij_pane_id=row.zellij_pane_id,
            cwd=row.cwd,
            status=row.status,
            current_claude_session_id=row.current_claude_session_id,
            current_transcript_path=row.current_transcript_path,
            created_at=row.created_at,
            last_activity=row.last_activity,
        )


class TaskRegistry:
    """In-memory registry of tasks with database persistence."""

    _HANDLERS: dict[str, str] = {
        "SessionStart": "_on_session_start",
        "UserPromptSubmit": "_on_user_prompt_submit",
        "PreToolUse": "_on_pre_tool_use",
        "PostToolUse": "_on_post_tool_use",
        "PostToolUseFailure": "_on_post_tool_use_failure",
        "Stop": "_on_stop",
        "Notification": "_on_notification",
        "SessionEnd": "_on_session_end",
        "SubagentStop": "_on_subagent_stop",
        "PreCompact": "_on_pre_compact",
    }

    def __init__(
        self,
        conn: aiosqlite.Connection,
        bot: "Bot | None",
        zellij: ZellijManager,
        approval_router: "ApprovalRouter | None" = None,
    ) -> None:
        """Initialize with database connection, bot, zellij manager, and optional approval router.

        `bot` may be None at construction time — `server.serve` builds the
        registry before the Bot exists (load_from_db must run before the HTTP
        server accepts requests, but the Bot logs in later) and calls
        `bind_bot` once the Bot is constructed.
        """
        self._conn = conn
        self._bot = bot
        self._zellij = zellij
        self._approval_router = approval_router
        self._by_task_id: dict[str, Task] = {}
        self._by_thread_id: dict[str, Task] = {}
        self._by_session_id: dict[str, Task] = {}
        self._stop_futures: dict[str, asyncio.Future] = {}
        self._typing_tasks: dict[str, asyncio.Task] = {}
        self._aggregators: dict[str, _ToolSummaryAggregator] = {}
        self._tui_handler_tasks: dict[str, asyncio.Task] = {}
        # Per-task locks serializing _stream_assistant_progress so a Stop
        # event can't race a still-in-flight PostToolUse streamer (which
        # was causing both duplicate messages AND, after the fix for
        # those, lost messages on transient post failure).
        self._streamer_locks: dict[str, asyncio.Lock] = {}
        # Discord-side reconciliation notices staged by load_from_db. Flushed by
        # flush_startup_notices() once the bot is ready.
        self._pending_startup_notices: list[dict] = []

    def bind_bot(self, bot: "Bot") -> None:
        """Attach the Bot instance after construction. Called once by
        `server.serve` after the Bot is created."""
        self._bot = bot

    @staticmethod
    def _notify_mention_prefix() -> str:
        """Return `<@USER_ID> ` to prepend to interactive TUI prompts so the
        configured user gets a Discord notification when claude is blocking
        on input. Empty string if `BRIDGE_NOTIFY_USER_ID` isn't set —
        keeps the prompt body unchanged for users who don't want pings.
        """
        user_id = os.environ.get("BRIDGE_NOTIFY_USER_ID", "").strip()
        if not user_id.isdigit():
            return ""
        return f"<@{user_id}> "

    async def load_from_db(self, *, reconcile_with_zellij: bool = False) -> None:
        """Restore in-memory task map from SQLite.

        With ``reconcile_with_zellij=False`` (the default), every active row is
        loaded into the in-memory map verbatim — used by tests and any caller
        that already knows the panes are alive.

        With ``reconcile_with_zellij=True`` (production daemon startup), each row
        is also reconciled against ``list_panes()``:
        - row's pane_id present and not exited → keep as-is
        - row's pane_id missing or exited=True → flip to 'crashed', post 💥, archive
        Tasks already in 'stopped'/'crashed' are excluded by ``list_active_tasks``.
        """
        rows = await list_active_tasks(self._conn)
        if not rows:
            return

        if not reconcile_with_zellij:
            for row in rows:
                await self._index(Task.from_row(row))
            return

        try:
            live_panes = await self._zellij.list_panes()
            live_pane_ids = {p["id"] for p in live_panes if not p.get("exited", False)}
        except ZellijError:
            logger.exception("failed to query zellij during recovery; assuming all panes alive")
            # Defensive: set live_pane_ids to all pane IDs we know about, so they're kept as-is
            live_pane_ids = {row.zellij_pane_id for row in rows if row.zellij_pane_id}

        recovered_live_tasks: list[Task] = []

        for row in rows:
            task = Task.from_row(row)
            # If task has no pane_id, it's mid-spawn; keep it loaded as-is
            if not task.zellij_pane_id:
                await self._index(task)
                logger.info("recovered spawning task %s (no pane yet)", task.task_id[:8])
                continue

            # Task has a pane_id; check if it's still alive
            if task.zellij_pane_id in live_pane_ids:
                await self._index(task)
                recovered_live_tasks.append(task)
                logger.info("recovered task %s on pane %s", task.task_id[:8], task.zellij_pane_id)
                continue

            # Pane is gone — mark crashed. Defer discord-side notices to
            # flush_startup_notices(); the bot isn't connected yet at startup.
            task.status = "crashed"
            task.last_activity = int(time.time())
            await self._index(task)
            await self._persist(task)
            self._pending_startup_notices.append({
                "task_id": task.task_id,
                "thread_id": task.thread_id,
                "messages": [
                    "💥 Bridge restarted; this task's pane is gone",
                    "🛡 ❌ Any pending approval was denied (bridge restarted)",
                ],
                "archive": True,
            })
            _cleanup_task_artifacts(task.task_id)

        for task in recovered_live_tasks:
            self._pending_startup_notices.append({
                "task_id": task.task_id,
                "thread_id": task.thread_id,
                "messages": [
                    "ℹ Bridge restarted; any pending approval was denied at the hook level",
                ],
                "archive": False,
            })

    async def flush_startup_notices(self) -> None:
        """Post and archive notices staged during reconcile. Caller must invoke
        only after the discord bot is ready (otherwise self._bot.post raises).
        Idempotent: drains the queue once and then becomes a no-op.
        """
        if not self._pending_startup_notices:
            return
        notices, self._pending_startup_notices = self._pending_startup_notices, []
        for notice in notices:
            for msg in notice["messages"]:
                try:
                    await self._bot.post(msg, thread_id=notice["thread_id"])
                except Exception:
                    logger.exception(
                        "failed to post startup notice for task %s", notice["task_id"]
                    )
            if notice["archive"]:
                try:
                    await self._archive_thread(notice["thread_id"])
                except Exception:
                    logger.exception(
                        "failed to archive thread for task %s", notice["task_id"]
                    )

    def get_by_task_id(self, task_id: str) -> Task | None:
        """Get task by task_id."""
        return self._by_task_id.get(task_id)

    def get_by_thread_id(self, thread_id: str) -> Task | None:
        """Get task by thread_id."""
        return self._by_thread_id.get(thread_id)

    def get_by_session_id(self, session_id: str) -> Task | None:
        """Get task by current_claude_session_id."""
        return self._by_session_id.get(session_id)

    async def _index(self, task: Task, prev_session_id: str | None = None) -> None:
        """Update all three maps for a task. Removes prior session_id entry if it changed."""
        # Remove old session_id entry if this task previously had a different session_id
        if prev_session_id and prev_session_id in self._by_session_id:
            if self._by_session_id[prev_session_id].task_id == task.task_id:
                del self._by_session_id[prev_session_id]

        # Remove old session_id entry if a different task owns the new session_id
        if task.current_claude_session_id:
            old_task = self._by_session_id.get(task.current_claude_session_id)
            if old_task and old_task.task_id != task.task_id:
                # Different task now owns this session_id; remove the old one
                if old_task.task_id in self._by_task_id:
                    del self._by_task_id[old_task.task_id]
                if old_task.thread_id in self._by_thread_id:
                    del self._by_thread_id[old_task.thread_id]

        # Index the task
        self._by_task_id[task.task_id] = task
        self._by_thread_id[task.thread_id] = task
        if task.current_claude_session_id:
            self._by_session_id[task.current_claude_session_id] = task

    async def _persist(self, task: Task) -> None:
        """Persist task to database."""
        await upsert_task(
            self._conn,
            task.task_id,
            task.thread_id,
            task.cwd,
            task.status,
            zellij_pane_id=task.zellij_pane_id,
            current_claude_session_id=task.current_claude_session_id,
            current_transcript_path=task.current_transcript_path,
        )

    def _build_spawn_env(self, task_id: str) -> dict[str, str]:
        """Bridge-specific env vars to inject into a spawned claude.

        Only the bridge-owned keys: zellij injects these at exec time via
        `env(1)`, on top of whatever env the zellij server was started with
        (which already carries PATH, HOME, etc).
        """
        return {
            "CC_BRIDGE_TASK_ID": task_id,
            "CC_DISCORD_TASK_ID": task_id,  # backward compat, remove in future release
            "BRIDGE_URL": os.environ.get("BRIDGE_URL", "http://127.0.0.1:8787"),
        }

    async def _is_pane_alive(self, pane_id: str) -> bool:
        """Check if a pane is still running (returns False if exited)."""
        try:
            panes = await self._zellij.list_panes()
            for pane in panes:
                if pane.get("id") == pane_id and not pane.get("exited", True):
                    return True
            return False
        except Exception:
            logger.exception("_is_pane_alive failed for pane %s", pane_id)
            return False

    async def _mark_stopped(self, task: Task) -> None:
        """Mark a task as stopped and archive its thread."""
        task.status = "stopped"
        task.last_activity = int(time.time())
        await self._persist(task)
        await self._archive_thread(task.thread_id)
        # Remove from live indexes; stopped tasks should not be returned by get_by_thread_id/get_by_session_id
        self._by_thread_id.pop(task.thread_id, None)
        if task.current_claude_session_id is not None:
            self._by_session_id.pop(task.current_claude_session_id, None)
        # Clean up typing task, aggregator, and any pending TUI handler task
        await self._stop_typing(task.task_id)
        agg = self._aggregators.pop(task.task_id, None)
        if agg is not None:
            await agg.flush_now()
        handler_task = self._tui_handler_tasks.pop(task.task_id, None)
        if handler_task is not None and not handler_task.done():
            handler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await handler_task
        # Drop the per-task streamer lock — it's keyed on task_id and a
        # stopped task will never stream again. Without this the dict
        # leaks one Lock object per task forever.
        self._streamer_locks.pop(task.task_id, None)
        # Clean up the task-scoped settings file
        _cleanup_task_artifacts(task.task_id)

    async def _archive_thread(self, thread_id: str) -> None:
        """Archive a Discord thread."""
        try:
            await self._bot.archive_thread(thread_id)
        except Exception:
            logger.exception("Failed to archive thread %s", thread_id)

    async def _start_typing(self, task: Task) -> None:
        """Start a typing indicator for a task. Cancels any prior typing task."""
        prev = self._typing_tasks.pop(task.task_id, None)
        if prev is not None and not prev.done():
            prev.cancel()
        self._typing_tasks[task.task_id] = asyncio.create_task(
            self._run_typing(task), name=f"typing-{task.task_id[:8]}"
        )

    async def _run_typing(self, task: Task) -> None:
        """Run typing indicator in background. Lives until cancelled."""
        try:
            channel = await self._bot.fetch_messageable(task.thread_id)
            async with channel.typing():
                # Sleep until cancelled (Stop/Notification cancels us). Discord.py auto-renews
                # the indicator every 5s under the hood; we just need the context to stay open.
                await asyncio.Future()  # never resolves; we live until cancelled
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("typing indicator failed for task %s", task.task_id)

    async def _stop_typing(self, task_id: str) -> None:
        """Cancel and await a typing task."""
        t = self._typing_tasks.pop(task_id, None)
        if t is not None and not t.done():
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t

    def _agg_for(self, task: Task) -> _ToolSummaryAggregator:
        agg = self._aggregators.get(task.task_id)
        if agg is None:
            agg = _ToolSummaryAggregator(self._bot, task.thread_id)
            self._aggregators[task.task_id] = agg
        return agg

    async def spawn_task(self, cwd: str, *, prompt: str | None = None) -> Task:
        """Spawn a new claude session in a Discord-bound task.

        1. Validates cwd is a directory.
        2. Generates a task_id UUID.
        3. Creates a Discord thread.
        4. Persists a row with status='spawning'.
        5. Builds env with CC_BRIDGE_TASK_ID (and CC_DISCORD_TASK_ID for backward compat) and BRIDGE_URL.
        6. Spawns claude in zellij via ZellijManager.
        7. Updates row with zellij_pane_id.
        8. Indexes the task in memory.
        9. Returns the Task.

        The `prompt` parameter is accepted but not used here — the slash-command
        layer in `commands.py` waits for SessionStart binding and then calls
        `write_initial_prompt` separately, since the pane isn't writable until
        the per-task settings file is loaded by claude.

        Raises TaskSpawnError if cwd is not a directory or zellij spawn fails.
        """
        # Validate cwd
        if not Path(cwd).is_dir():
            raise TaskSpawnError(f"cwd does not exist: {cwd}")

        # Generate task_id
        task_id = str(uuid.uuid4())

        # Create Discord thread
        thread_name = f"cc · {Path(cwd).name} · {task_id[:8]}"
        thread_id = await self._bot.create_thread(name=thread_name)

        # Persist row with status='spawning'
        now = int(time.time())
        await upsert_task(
            self._conn,
            task_id,
            thread_id,
            cwd,
            "spawning",
            zellij_pane_id=None,
            current_claude_session_id=None,
            current_transcript_path=None,
            now=now,
        )

        # Write task-scoped settings file with bridge hooks
        settings_path = _write_task_settings(task_id)

        # Build env for spawned claude
        env = self._build_spawn_env(task_id)

        # Generate a zellij layout that opens the new tab as a single
        # claude pane (no default shell). claude_argv is what runs after
        # `env K=V ...`. `tab_name` matches what we'll use to address the
        # tab via `go-to-tab-name` later.
        tab_name = f"cc-{task_id[:8]}"
        layout_path = _write_task_layout(
            task_id,
            env=env,
            claude_argv=["--settings", str(settings_path)],
            tab_name=tab_name,
        )

        # Spawn via zellij; on failure, mark the task as crashed and re-raise
        try:
            pane_id = await self._zellij.spawn_task(
                cwd=cwd,
                pane_name=tab_name,
                layout_path=str(layout_path),
            )
        except ZellijError:
            logger.exception(f"spawn_task failed for task_id {task_id}")
            # Mark the task as crashed in the database
            await upsert_task(
                self._conn,
                task_id,
                thread_id,
                cwd,
                "crashed",
                zellij_pane_id=None,
                current_claude_session_id=None,
                current_transcript_path=None,
            )
            _cleanup_task_artifacts(task_id)
            # Surface the failure in the just-created Discord thread, then
            # archive it so the user isn't left with an empty silent thread.
            try:
                await self._bot.post(
                    "💥 Task failed to spawn — zellij couldn't open the pane. "
                    "Check the bridge daemon log for details.",
                    thread_id=thread_id,
                )
            except Exception:
                logger.exception("failed to post spawn-failure notice for task %s", task_id)
            try:
                await self._archive_thread(thread_id)
            except Exception:
                logger.exception("failed to archive thread for failed spawn %s", task_id)
            raise

        # Update row with zellij_pane_id (bump last_activity)
        now2 = int(time.time())
        await upsert_task(
            self._conn,
            task_id,
            thread_id,
            cwd,
            "spawning",
            zellij_pane_id=pane_id,
            current_claude_session_id=None,
            current_transcript_path=None,
            now=now2,
        )

        # Construct and index the Task
        task = Task(
            task_id=task_id,
            thread_id=thread_id,
            zellij_pane_id=pane_id,
            cwd=cwd,
            status="spawning",
            current_claude_session_id=None,
            current_transcript_path=None,
            created_at=now,
            last_activity=now,
        )
        await self._index(task)

        return task

    async def maybe_route_message(self, msg: MessageLike) -> bool:
        """If msg is in a task-bound thread, write to its zellij pane and return True.
        Otherwise return False so the caller falls through to the existing /v1/ask listener.

        Attachments are downloaded to ~/.local/state/cc-bridge/attachments/<task>/
        and their absolute paths are appended to the relayed text so Claude can read them
        with the Read tool (handles images, PDFs, JSON, plain text, etc.).
        """
        thread_id = msg.channel.id
        task = self.get_by_thread_id(thread_id)
        if task is None:
            return False
        if task.zellij_pane_id is None:
            # Task is mid-spawn; treat as no-op so the message goes to the /v1/ask listener
            # (which will also drop it; net effect: silent ignore — fine).
            return False
        if task.status not in ("running", "spawning"):
            # task is stopped/crashed — silent ignore per AC3.6 spirit
            return True

        text = (msg.content or "").rstrip()
        attachment_paths: list[Path] = []
        if msg.attachments:
            attachment_paths = await self._save_attachments(task.task_id, msg)

        # Split saved attachments into audio (transcribe via Wispr) vs other
        # (relay path so claude can Read it).
        voice_paths = [p for p in attachment_paths if voice.is_audio_path(p)]
        other_paths = [p for p in attachment_paths if not voice.is_audio_path(p)]

        voice_segments: list[str] = []
        for p in voice_paths:
            transcript_text = await voice.transcribe(p)
            if transcript_text:
                voice_segments.append(f"[voice memo] {transcript_text}")
            else:
                voice_segments.append(
                    "[voice memo received — transcription unavailable; "
                    f"raw file: {p}]"
                )

        if not text and not voice_segments and not other_paths:
            return True  # consumed silently — empty message

        parts: list[str] = []
        if text:
            parts.append(text)
        parts.extend(voice_segments)
        if other_paths:
            # Inline each path with the rest of the prose (space-separated)
            # rather than putting them on their own lines. zellij's pipeline
            # to claude's TUI loses content past the first ~50 bytes of
            # multi-line writes, so keeping paths inline guarantees they
            # arrive even if any later \n drops content.
            for p in other_paths:
                parts.append(f"[attached: {p}]")
        # Single-space join keeps the relay one logical line (the user's own
        # text may still contain its own newlines, but we don't add any).
        combined = " ".join(parts)

        logger.info(
            "relay → pane (task=%s, %d chars, %d segments): %r",
            task.task_id[:8],
            len(combined),
            combined.count("\n") + 1,
            combined[:300] + ("…" if len(combined) > 300 else ""),
        )
        if not await self._write_with_retry(task, combined + "\n"):
            return True  # error already surfaced to the thread
        task.last_activity = int(time.time())
        await self._persist(task)
        return True

    async def _write_with_retry(self, task: Task, payload: str) -> bool:
        """Type `payload` into the task's pane, retrying once on ZellijError.

        Empirically zellij hiccups on `go-to-tab-name` when no client is
        attached to the session — Hailey reported that re-sending the same
        message works on the second try. Retry once after a 1s pause
        before surfacing the failure as a thread notice.

        Returns True if delivery succeeded (caller should bump
        last_activity), False if both attempts failed (caller should
        skip activity bump because we already posted an error).
        """
        for attempt in (1, 2):
            try:
                await self._zellij.write_to_pane(task.zellij_pane_id, payload)
                if attempt == 2:
                    logger.info(
                        "write_to_pane succeeded on retry for task %s",
                        task.task_id,
                    )
                return True
            except ZellijError as e:
                if attempt == 1:
                    logger.warning(
                        "write_to_pane attempt 1 failed for task %s (%s); retrying",
                        task.task_id, e,
                    )
                    await asyncio.sleep(1.0)
                    continue
                logger.exception(
                    "write_to_pane retry also failed for task %s", task.task_id
                )
                try:
                    await self._bot.post(
                        f"⚠ Couldn't deliver your message — zellij command failed "
                        f"(`{e}`). The task may be wedged; try `/restart` or `/kill`.",
                        thread_id=task.thread_id,
                    )
                except Exception:
                    logger.exception(
                        "failed to post wedge notice for task %s", task.task_id
                    )
                return False
        return False

    async def _save_attachments(self, task_id: str, msg: MessageLike) -> list[Path]:
        """Download Discord attachments to disk under ATTACHMENTS_DIR/<task_id>/.

        Filenames are sanitized to the basename and prefixed with the message id
        to avoid collisions when the same name is attached multiple times.
        Returns absolute paths in attachment order. Failures are logged and
        skipped (caller still relays whatever succeeded).
        """
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
                local.write_bytes(data)
                saved.append(local)
            except Exception:
                logger.exception("failed to save attachment %s", raw_name)
        return saved

    async def handle_event(self, hook_event_name: str, body: dict) -> None:
        """Dispatch event to appropriate handler by name."""
        session_id = body.get("session_id")
        bound = bool(session_id and self.get_by_session_id(session_id))
        logger.info(
            "hook event: %s session_id=%s bound=%s",
            hook_event_name,
            (session_id[:8] + "…") if session_id else "<missing>",
            bound,
        )
        handler_name = self._HANDLERS.get(hook_event_name)
        if handler_name is None:
            logger.info(f"Unknown hook event: {hook_event_name}")
            return

        handler = getattr(self, handler_name, None)
        if handler is None:
            logger.warning(f"Handler not found: {handler_name}")
            return

        try:
            await handler(body)
        except Exception:
            logger.exception(f"Error handling {hook_event_name}")

    async def _on_session_start(self, body: dict) -> None:
        """Handle SessionStart event.

        Branches on matcher value:
        - 'startup': first bind. Look up by CC_BRIDGE_TASK_ID (or CC_DISCORD_TASK_ID for backward compat),
          populate session/transcript, flip status to running, post '🟢 Task started' notice.
        - 'clear':   /clear inside TUI. Rebind same task to new session id, post '🧹' notice.
        - 'compact': /compact inside TUI. Rebind same task to new session id, post '🧰' notice.
        - 'resume':  claude --resume by /restart. Same as startup but no notice (user already
                     saw the /restart command's reply).
        - default:   unknown matcher → fall through to startup behavior (safest).

        Per AC2.4: silently drops SessionStart events with no CC_BRIDGE_TASK_ID or CC_DISCORD_TASK_ID.
        Also drops if session_id or transcript_path is missing.
        """
        session_id = body.get("session_id")
        transcript_path = body.get("transcript_path")
        env_passthrough = body.get("env_passthrough", {})
        task_id = env_passthrough.get("CC_BRIDGE_TASK_ID") or env_passthrough.get("CC_DISCORD_TASK_ID")
        matcher = body.get("matcher") or "startup"

        logger.info(f"SessionStart: session_id={session_id}, task_id={task_id}, matcher={matcher}")

        # AC2.4: drop SessionStart without task_id, session_id, or transcript_path
        if not task_id or not session_id or not transcript_path:
            if not task_id:
                logger.debug("SessionStart: no CC_BRIDGE_TASK_ID or CC_DISCORD_TASK_ID in env_passthrough")
            elif not session_id:
                logger.warning("SessionStart: no session_id in body")
            elif not transcript_path:
                logger.warning("SessionStart: no transcript_path in body")
            return

        # session_id flows from a hook body into argv (claude --resume
        # <session_id>) and into the KDL layout file. claude's own
        # session ids are UUIDs; reject anything that doesn't fit that
        # shape so a malformed/hostile field can't smuggle in newlines,
        # quotes, control bytes, or argv-flag-shaped content.
        if not _SESSION_ID_RE.match(session_id):
            logger.warning(
                "SessionStart: rejecting session_id with unexpected shape: %r",
                session_id[:80],
            )
            return

        task = self.get_by_task_id(task_id)
        if not task:
            logger.warning(f"SessionStart: task_id {task_id} not found in registry")
            return

        # Detach from old session id index; re-index under new
        old_session_id = task.current_claude_session_id
        task.current_claude_session_id = session_id
        task.current_transcript_path = transcript_path
        task.status = "running"
        task.last_activity = int(time.time())
        # New session = new turn boundary; drop any prior streaming state.
        task.posted_assistant_uuids.clear()
        task.subagent_blocks.clear()
        task.task_list_state.clear()
        task.last_task_list_message_id = None
        if task.task_list_post_timer is not None and not task.task_list_post_timer.done():
            task.task_list_post_timer.cancel()
            task.task_list_post_timer = None
        if old_session_id and old_session_id in self._by_session_id:
            del self._by_session_id[old_session_id]
        if session_id:
            self._by_session_id[session_id] = task

        # Persist
        await self._persist(task)

        # Post appropriate notice based on matcher
        notice = None
        if matcher == "startup":
            short_session_id = session_id[:8] if session_id else "?"
            notice = f"🟢 Task started — claude session `{short_session_id}`"
        elif matcher == "clear":
            short_session_id = session_id[:8] if session_id else "?"
            notice = f"🧹 Context cleared (new session: `{short_session_id}`)"
        elif matcher == "compact":
            notice = "🧰 Context compacted"
        elif matcher == "resume":
            # Don't post — the user just ran /restart; spamming is noisy.
            notice = None
        else:
            # Unknown matcher: log and post a small bind notice
            logger.info("SessionStart with unrecognized matcher %r", matcher)
            short_session_id = session_id[:8] if session_id else "?"
            notice = f"🟢 Bound to session `{short_session_id}` (matcher={matcher})"

        if notice:
            try:
                await self._bot.post(notice, thread_id=task.thread_id)
            except Exception:
                logger.exception("failed to post session start notice")

    async def _on_user_prompt_submit(self, body: dict) -> None:
        """Handle UserPromptSubmit event. Start typing indicator and cancel pending TUI prompts."""
        session_id = body.get("session_id")
        if not session_id:
            return
        task = self.get_by_session_id(session_id)
        if task is None:
            return
        # Cancel any pending TUI prompts for this thread — user typed in zellij.
        if self._approval_router:
            cancelled = await self._approval_router.cancel_thread_tui(task.thread_id)
            if cancelled > 0:
                logger.info("cancelled %d pending TUI prompts for task %s (answered in zellij)", cancelled, task.task_id)
        task.last_activity = int(time.time())
        # New turn — start fresh streaming. Keep subagent_blocks across
        # turns: the JSONL files for prior subagents persist on disk, and
        # clearing the dict here would cause the next refresh to re-create
        # duplicate Discord messages for those same agent_ids.
        task.posted_assistant_uuids.clear()
        await self._persist(task)
        await self._start_typing(task)

    async def _on_pre_tool_use(self, body: dict) -> None:
        """Handle PreToolUse for `AskUserQuestion` / `ExitPlanMode` only —
        those are the matchers we register in `_write_task_settings`. The
        body carries the full structured `tool_input`, so we can post a
        proper Discord embed with the question and option reactions
        BEFORE the tool blocks for TUI input. The user's reaction reply
        gets typed into the pane via the existing TUI handler flow.

        Other PreToolUse events would only arrive if a stale settings
        file still has wider matchers; for those we no-op so claude's
        permission mode keeps driving approvals.
        """
        tool_name = body.get("tool_name")
        if tool_name not in ("AskUserQuestion", "ExitPlanMode"):
            return
        session_id = body.get("session_id")
        if not session_id:
            return
        task = self.get_by_session_id(session_id)
        if task is None:
            return

        existing = self._tui_handler_tasks.get(task.task_id)
        if existing is not None and not existing.done():
            # Already being handled (probably from an earlier PreToolUse for
            # the same call). Don't double-post.
            return

        pending = {
            "id": body.get("tool_use_id") or str(uuid.uuid4()),
            "name": tool_name,
            "input": body.get("tool_input") or {},
        }
        if tool_name == "AskUserQuestion":
            handler_task = asyncio.create_task(
                self._handle_ask_user_question(task, pending),
                name=f"tui-ask_question-{task.task_id[:8]}",
            )
        else:  # ExitPlanMode
            handler_task = asyncio.create_task(
                self._handle_exit_plan_mode(task, pending),
                name=f"tui-exit_plan-{task.task_id[:8]}",
            )
        self._track_tui_handler_task(task.task_id, handler_task)

    async def _on_post_tool_use(self, body: dict) -> None:
        """Handle PostToolUse event. Stream pending main-agent prose, append
        the tool summary to the main aggregator, and post a diff/content
        block for Edit/MultiEdit/Write. Subagent activity bypasses the
        aggregator entirely — it's collated into per-subagent live blocks
        via `_refresh_subagent_blocks`.
        """
        session_id = body.get("session_id")
        if not session_id:
            return
        task = self.get_by_session_id(session_id)
        if task is None:
            return
        task.last_activity = int(time.time())
        await self._persist(task)

        # Always refresh subagent blocks first — they have their own
        # detection and rendering, independent of main-agent flow.
        await self._refresh_subagent_blocks(task)

        # Suppress main-agent emission for tool calls that came from a
        # subagent (the block already shows them).
        if self._is_sidechain_tool(body, body.get("tool_name", "?")):
            return

        # Stream any prose Claude wrote between the previous boundary and now.
        await self._stream_assistant_progress(task)

        tool_name = body.get("tool_name", "?")
        tool_input = body.get("tool_input", {}) or {}
        tool_response = body.get("tool_response", {}) or {}

        # TaskCreate / TaskUpdate / TaskList don't get inline aggregator
        # lines — the debounced full-list block already shows their effect,
        # and the inline lines duplicate that.
        if tool_name not in ("TaskCreate", "TaskUpdate", "TaskList"):
            line = tool_summary.summarize(tool_name, tool_input, tool_response)
            self._agg_for(task).append(line)
            await self._post_tool_diff(task, tool_name, tool_input)
        else:
            self._update_task_list_state(task, tool_name, tool_input, tool_response)
            if tool_name in ("TaskCreate", "TaskUpdate"):
                self._schedule_task_list_post(task)

    def _update_task_list_state(
        self,
        task: Task,
        tool_name: str,
        tool_input: dict,
        tool_response: dict,
    ) -> None:
        """Apply a TaskCreate/TaskUpdate/TaskList event to `task.task_list_state`.

        The shape of `tool_response` for these isn't fully documented; we
        accept either a top-level list of tasks or a `{"tasks": [...]}`
        envelope, and pull task ids from `id`/`taskId`/`task_id`.
        """
        if tool_name == "TaskCreate":
            task_id = self._extract_task_id(tool_response, tool_input)
            if task_id is None:
                return
            entry = task.task_list_state.setdefault(str(task_id), {})
            for key in ("subject", "description", "owner"):
                val = tool_input.get(key)
                if isinstance(val, str):
                    entry[key] = val
            entry.setdefault("status", "pending")
            return

        if tool_name == "TaskUpdate":
            task_id = tool_input.get("taskId") or tool_input.get("task_id")
            if task_id is None:
                return
            entry = task.task_list_state.setdefault(str(task_id), {})
            for key in ("status", "subject", "description", "owner"):
                val = tool_input.get(key)
                if isinstance(val, str):
                    entry[key] = val
            return

        if tool_name == "TaskList":
            tasks_payload = tool_response.get("tasks") if isinstance(tool_response, dict) else None
            if tasks_payload is None and isinstance(tool_response, list):
                tasks_payload = tool_response
            if not isinstance(tasks_payload, list):
                return
            new_state: dict[str, dict] = {}
            for t in tasks_payload:
                if not isinstance(t, dict):
                    continue
                tid = t.get("id") or t.get("taskId") or t.get("task_id")
                if tid is None:
                    continue
                new_state[str(tid)] = {
                    "subject": t.get("subject") or "",
                    "status": t.get("status") or "pending",
                    "owner": t.get("owner") or "",
                    "description": t.get("description") or "",
                }
            task.task_list_state = new_state

    @staticmethod
    def _extract_task_id(tool_response: dict, tool_input: dict) -> str | None:
        """Pull a task id out of a TaskCreate response. Falls back to a
        synthesized id derived from the subject if nothing's in the
        response (rendering still works; subsequent TaskUpdate by real id
        won't merge but that's a minor display glitch)."""
        for src in (tool_response, tool_input):
            if not isinstance(src, dict):
                continue
            for key in ("id", "taskId", "task_id"):
                val = src.get(key)
                if val is not None:
                    return str(val)
        # No id found — synthesize from subject so it at least renders.
        subject = tool_input.get("subject") if isinstance(tool_input, dict) else None
        if isinstance(subject, str) and subject.strip():
            return f"~{subject.strip()[:40]}"
        return None

    def _schedule_task_list_post(self, task: Task) -> None:
        """Debounce: post the current task list 1s after the last
        TaskCreate/TaskUpdate. Bursts (5 quick TaskCreates → one plan
        message) coalesce into a single post.
        """
        existing = task.task_list_post_timer
        if existing is not None and not existing.done():
            existing.cancel()
        task.task_list_post_timer = asyncio.create_task(
            self._post_task_list_after_delay(task),
            name=f"task-list-post-{task.task_id[:8]}",
        )

    async def _post_task_list_after_delay(
        self, task: Task, *, delay: float = 1.0
    ) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if not task.task_list_state:
            return
        embed = self._render_task_list_embed(task)

        # If our most recent task-list post is STILL the most recent
        # message in the thread (i.e. nothing else has been posted since),
        # edit it in place to avoid stacking duplicate-looking messages.
        # Otherwise post a fresh one — that way the latest state is
        # always the bottom-most thing in the thread when activity has
        # moved on past the previous render.
        if task.last_task_list_message_id is not None:
            try:
                channel = await self._bot.fetch_messageable(task.thread_id)
                last_id = getattr(channel, "last_message_id", None)
                if last_id == task.last_task_list_message_id:
                    await self._bot.edit_message(
                        task.thread_id,
                        task.last_task_list_message_id,
                        embed=embed,
                    )
                    return
            except BotNotReady:
                return
            except Exception:
                logger.exception(
                    "task-list edit-in-place check failed for task %s; posting fresh",
                    task.task_id,
                )

        try:
            task.last_task_list_message_id = await self._bot.post_embed(
                embed, thread_id=task.thread_id
            )
        except BotNotReady:
            return
        except Exception:
            logger.exception("failed to post task list for task %s", task.task_id)

    @staticmethod
    def _render_task_list_embed(task: Task) -> discord.Embed:
        """Render the task_list_state mirror as a Discord embed so it stands
        apart from the thread's prose flow with a colored border."""
        # Sort by id; ids are typically integer-stringy so try numeric sort.
        def _sort_key(tid: str) -> tuple[int, int | str]:
            try:
                return (0, int(tid))
            except ValueError:
                return (1, tid)

        lines: list[str] = []
        total = 0
        done = 0
        in_progress = 0
        for tid in sorted(task.task_list_state.keys(), key=_sort_key):
            entry = task.task_list_state[tid]
            status = entry.get("status") or "pending"
            subject = (entry.get("subject") or "").strip()
            if status == "completed":
                mark, done = "✅", done + 1
            elif status == "in_progress":
                mark, in_progress = "▶️", in_progress + 1
            elif status == "deleted":
                mark = "🗑"
            else:
                mark = "⬜"
            total += 1
            line = f"{mark} #{tid}"
            if subject:
                line += f" {subject}"
            lines.append(line)

        description = "\n".join(lines) or "_(no tasks)_"
        if len(description) > 4000:
            description = description[:3997] + "…"

        # Color: green when everything's done, yellow if any in progress,
        # otherwise neutral grey.
        if total > 0 and done == total:
            color = 0x57F287
        elif in_progress > 0:
            color = 0xFEE75C
        else:
            color = 0x95A5A6

        embed = discord.Embed(
            title="📋 Tasks",
            description=description,
            color=color,
        )
        embed.set_footer(text=f"{done}/{total} done")
        return embed

    def _is_sidechain_tool(self, body: dict, tool_name: str) -> bool:
        """Best-effort: did the most recent `tool_use` of `tool_name` come from
        a subagent? Modern CC versions write subagent activity to separate
        `<session>/subagents/agent-*.jsonl` files instead of marking entries
        in the main transcript with `isSidechain`, so check both:

          1. Legacy: the main transcript has a recent `tool_use` whose
             parent assistant entry has `isSidechain: true`.
          2. Current: any subagent file under the session contains a recent
             `tool_use` matching `tool_name`.
        """
        tp = body.get("transcript_path")
        if not isinstance(tp, str):
            return False
        main_path = Path(tp)
        try:
            if transcript.is_recent_tool_use_sidechain(main_path, tool_name):
                return True
        except Exception:
            logger.exception("is_recent_tool_use_sidechain failed for %s", tp)

        subagents_dir = main_path.parent / main_path.stem / "subagents"
        if not subagents_dir.is_dir():
            return False
        try:
            files = sorted(
                subagents_dir.glob("agent-*.jsonl"),
                key=lambda f: -f.stat().st_mtime,
            )
        except OSError:
            return False
        for f in files:
            try:
                # A tool_use in a subagent file is by definition sidechain.
                for e in reversed(list(transcript.read_entries(f))):
                    if e.get("type") != "assistant":
                        continue
                    msg = e.get("message")
                    if not isinstance(msg, dict):
                        continue
                    content = msg.get("content")
                    if not isinstance(content, list):
                        continue
                    if any(
                        isinstance(b, dict)
                        and b.get("type") == "tool_use"
                        and b.get("name") == tool_name
                        for b in content
                    ):
                        return True
                    break  # only check the most recent assistant entry
            except Exception:
                logger.exception("subagent sidechain check failed for %s", f)
        return False

    async def _on_post_tool_use_failure(self, body: dict) -> None:
        """Handle PostToolUseFailure event. Force-failure summary."""
        session_id = body.get("session_id")
        if not session_id:
            return
        task = self.get_by_session_id(session_id)
        if task is None:
            return
        task.last_activity = int(time.time())
        await self._persist(task)
        # Stream any prose written before the failure too.
        await self._stream_assistant_progress(task)
        # Force-failure: synthesize a tool_response.is_error=True if missing.
        tool_response = (body.get("tool_response") or {}).copy()
        tool_response["is_error"] = True
        line = tool_summary.summarize(
            body.get("tool_name", "?"),
            body.get("tool_input", {}) or {},
            tool_response,
        )
        self._agg_for(task).append(line)

    async def _post_tool_diff(
        self, task: Task, tool_name: str, tool_input: dict
    ) -> None:
        """For Edit/MultiEdit/Write: post the actual change as a fenced block.

        Posted separately from the one-liner summary so the aggregator can
        keep coalescing summaries while diffs surface as their own messages.
        """
        block = tool_summary.diff_block(tool_name, tool_input)
        if not block:
            return
        try:
            await self._bot.post(block, thread_id=task.thread_id)
        except Exception:
            logger.exception("failed to post diff block for task %s", task.task_id)

    async def _on_stop(self, body: dict) -> None:
        """Handle Stop event. Cancel pending TUI, stop typing, flush summaries, and post final turn."""
        session_id = body.get("session_id")
        if not session_id:
            return
        task = self.get_by_session_id(session_id)
        if task is None:
            return
        # Defensively cancel any pending TUI prompts when Stop fires
        if self._approval_router:
            await self._approval_router.cancel_thread_tui(task.thread_id)
        task.last_activity = int(time.time())
        await self._persist(task)
        await self._stop_typing(task.task_id)

        # Flush pending tool summaries first
        agg = self._aggregators.get(task.task_id)
        if agg is not None:
            await agg.flush_now()

        # Stream any remaining assistant content for the turn. Stop fires
        # before Claude's transcript writer flushes the final entry, so wait
        # for the file to settle, then stream once more.
        transcript_path = body.get("transcript_path") or task.current_transcript_path
        if transcript_path:
            tp = Path(transcript_path)
            await self._wait_for_transcript_stable(tp)
            await self._stream_assistant_progress(task, tp)
            # Final pass at subagent blocks so any in-flight ones end up in
            # their `finished` state (with the latest 5 actions captured).
            await self._refresh_subagent_blocks(task)
            await self._post_stats_footer(task, tp)
        else:
            logger.info("Stop: no transcript_path in body or task; skipping final stream")

    async def _post_stats_footer(self, task: Task, transcript_path: Path) -> None:
        """After Stop, post a one-line model/tokens/cost summary to Discord."""
        try:
            stats = usage.compute_stats(transcript_path)
        except Exception:
            logger.exception("compute_stats failed for task %s", task.task_id)
            return
        if stats is None:
            return
        try:
            await self._bot.post(
                usage.format_summary(stats), thread_id=task.thread_id
            )
        except Exception:
            logger.exception("failed to post stats footer for task %s", task.task_id)

    # Max time to wait at Stop for the transcript writer to flush. Empirically,
    # claude finishes writing the final assistant entry within 1-2s of Stop on
    # short responses but can take 5-8s on long ones; 10s leaves headroom
    # without delaying truly stuck cases too long. Tests override to 0.
    _STOP_TRANSCRIPT_RETRY_SECS: float = 10.0

    async def _wait_for_transcript_stable(
        self, path: Path, *, stable_secs: float = 0.25
    ) -> None:
        """Wait until path's size hasn't changed for `stable_secs`, or budget elapses.

        Polls at 50ms and considers the file "stable" once 250ms have passed
        with no size change — generous enough to absorb a slow writer's
        between-chunks pause, tight enough to avoid noticeable user lag.
        Bounded by `_STOP_TRANSCRIPT_RETRY_SECS`. No-op if file doesn't exist.
        """
        if not path.is_file():
            return
        if self._STOP_TRANSCRIPT_RETRY_SECS <= 0:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._STOP_TRANSCRIPT_RETRY_SECS
        last_size = -1
        stable_since: float | None = None
        while True:
            try:
                size = path.stat().st_size
            except OSError:
                return
            now = loop.time()
            if size == last_size:
                if stable_since is None:
                    stable_since = now
                elif now - stable_since >= stable_secs:
                    return
            else:
                last_size = size
                stable_since = None
            if now >= deadline:
                return
            await asyncio.sleep(0.05)

    async def _stream_assistant_progress(
        self, task: Task, path: Path | None = None
    ) -> None:
        """Post any assistant content (text/thinking blocks) for the current
        turn that hasn't been streamed yet. Idempotent — entries already in
        `task.posted_assistant_uuids` are skipped.

        Concurrency: uses a per-task `asyncio.Lock` so a Stop event firing
        while a still-in-flight PostToolUse streamer is awaiting `bot.post`
        can't race. Inside the lock we only mark a uuid as posted AFTER its
        post succeeds — so a transient post failure leaves the uuid
        un-marked and the next streamer call (next PostToolUse, or Stop)
        will reconsider and retry it. This means the final assistant text
        survives a single bot.post failure rather than getting silently
        dropped.

        Walks from the last real-user prompt forward; only emits text and
        thinking blocks (tool_use blocks are handled by PostToolUse).
        """
        if path is None:
            if not task.current_transcript_path:
                return
            path = Path(task.current_transcript_path)
        if not path.is_file():
            return

        lock = self._streamer_locks.setdefault(task.task_id, asyncio.Lock())
        async with lock:
            entries = list(transcript.read_entries(path))
            if not entries:
                return

            last_user_idx = -1
            for i in range(len(entries) - 1, -1, -1):
                e = entries[i]
                if e.get("type") != "user":
                    continue
                if e.get("isSidechain") is True or e.get("isMeta") is True:
                    continue
                msg = e.get("message")
                if not isinstance(msg, dict):
                    continue
                if isinstance(msg.get("content"), str):
                    last_user_idx = i
                    break

            for e in entries[last_user_idx + 1:]:
                if e.get("type") != "assistant":
                    continue
                if e.get("isMeta") is True:
                    continue
                uid = e.get("uuid")
                if not isinstance(uid, str) or uid in task.posted_assistant_uuids:
                    continue
                sidechain = e.get("isSidechain") is True
                prefix = "↳ " if sidechain else ""

                msg = e.get("message")
                content = msg.get("content") if isinstance(msg, dict) else None
                if not isinstance(content, list):
                    # Nothing postable in this entry — but mark posted so
                    # we don't keep re-considering it on every refresh.
                    task.posted_assistant_uuids.add(uid)
                    continue

                entry_succeeded = True
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text = block.get("text")
                        if isinstance(text, str) and text.strip():
                            cleaned, attach_paths = _parse_attach_markers(text)
                            body = (prefix + cleaned) if cleaned else None
                            try:
                                if attach_paths:
                                    await self._bot.post_with_attachments(
                                        attach_paths,
                                        thread_id=task.thread_id,
                                        text=body,
                                    )
                                elif body:
                                    await self._bot.post(
                                        body, thread_id=task.thread_id
                                    )
                            except Exception:
                                logger.exception(
                                    "failed to stream text for task %s", task.task_id
                                )
                                entry_succeeded = False
                    elif btype == "thinking":
                        # Extended-thinking blocks are encrypted by default
                        # (`thinking` is empty). Only post when content is
                        # visible.
                        thought = block.get("thinking") or ""
                        if isinstance(thought, str) and thought.strip():
                            try:
                                await self._bot.post(
                                    prefix + self._format_thinking(thought),
                                    thread_id=task.thread_id,
                                )
                            except Exception:
                                logger.exception(
                                    "failed to stream thinking for task %s", task.task_id
                                )
                                entry_succeeded = False
                # Only mark the uid posted if every block we tried to send
                # succeeded — this lets the next streamer call re-try the
                # whole entry on transient post failure.
                if entry_succeeded:
                    task.posted_assistant_uuids.add(uid)

    @staticmethod
    def _format_thinking(text: str) -> str:
        """Discord-render a thinking block: 🤔 + multi-line italics."""
        return f"🤔 *{text.strip()}*"

    async def _refresh_subagent_blocks(self, task: Task) -> None:
        """Scan `<session>/subagents/agent-*.jsonl` and create/update one
        `SubagentBlock` per file. Each block is a single Discord message
        edited in place to show the last N actions of that subagent.

        Idempotent — uses `last_entry_uuid` for change detection so we only
        edit when there's new content. Best-effort: errors are logged and
        skipped so a single bad file doesn't break the loop.
        """
        if not task.current_transcript_path:
            return
        main_path = Path(task.current_transcript_path)
        # Subagent files live at <project>/<session>/subagents/agent-*.jsonl
        subagents_dir = main_path.parent / main_path.stem / "subagents"
        if not subagents_dir.is_dir():
            return
        for f in sorted(subagents_dir.glob("agent-*.jsonl")):
            agent_id = f.stem.removeprefix("agent-")
            try:
                await self._refresh_one_subagent_block(task, agent_id, f)
            except Exception:
                logger.exception(
                    "subagent block refresh failed for %s", agent_id
                )

    async def _refresh_one_subagent_block(
        self, task: Task, agent_id: str, agent_file: Path
    ) -> None:
        entries = list(transcript.read_entries(agent_file))
        if not entries:
            return

        attribution = next(
            (
                e["attributionAgent"]
                for e in entries
                if isinstance(e.get("attributionAgent"), str)
            ),
            agent_id,
        )

        # Build action lines from the assistant entries' content blocks.
        actions: list[str] = []
        total_actions = 0
        for e in entries:
            if e.get("type") != "assistant":
                continue
            msg = e.get("message")
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                line = self._format_subagent_action(block)
                if line:
                    actions.append(line)
                    total_actions += 1
        if not actions:
            return

        last_actions = actions[-_SUBAGENT_BLOCK_MAX_ACTIONS:]
        last_uuid = entries[-1].get("uuid")

        # Heuristic for "finished": the most recent assistant entry has no
        # tool_use blocks (i.e. it's terminal text).
        finished = self._is_subagent_finished(entries)

        block = task.subagent_blocks.get(agent_id)
        now = time.time()

        if block is None:
            block = SubagentBlock(
                agent_id=agent_id,
                attribution=attribution,
                started_at=now,
                last_entry_uuid=last_uuid,
                last_edit_at=now,
                finished_at=now if finished else None,
            )
            embed = self._render_subagent_embed(
                block, last_actions, total_actions, finished
            )
            # Post FIRST, persist only on success. If the post fails (most
            # commonly: bot not yet logged in during the brief startup window
            # where the HTTP server is already accepting hook events), don't
            # add the block to task.subagent_blocks — that lets the next
            # refresh re-try as if fresh, instead of leaving a zombie block
            # whose `message_id is None` blocks future edits.
            try:
                block.message_id = await self._bot.post_embed(
                    embed, thread_id=task.thread_id
                )
            except BotNotReady:
                logger.info(
                    "bot not ready for subagent block %s; will retry on next refresh",
                    agent_id,
                )
                return
            except Exception:
                logger.exception(
                    "failed to post initial subagent block for %s", agent_id
                )
                return
            task.subagent_blocks[agent_id] = block
            return

        if block.last_entry_uuid == last_uuid and block.finished_at is not None:
            return  # nothing to do
        if block.last_entry_uuid == last_uuid and not finished:
            return
        if not finished and now - block.last_edit_at < _SUBAGENT_EDIT_THROTTLE_SECS:
            return  # throttle; will catch up on the next refresh

        block.last_entry_uuid = last_uuid
        block.last_edit_at = now
        if finished and block.finished_at is None:
            block.finished_at = now

        if block.message_id is None:
            return  # initial post failed; nothing to edit
        embed = self._render_subagent_embed(
            block, last_actions, total_actions, finished
        )
        try:
            await self._bot.edit_message(
                task.thread_id, block.message_id, embed=embed
            )
        except Exception:
            logger.exception(
                "failed to edit subagent block for %s", agent_id
            )

    def _format_subagent_action(self, block: dict) -> str | None:
        """Format one assistant content block as a single bulleted line."""
        btype = block.get("type")
        if btype == "tool_use":
            line = tool_summary.summarize(
                block.get("name", "?"),
                block.get("input") or {},
                None,
            )
            return f"• {line}"
        if btype == "text":
            txt = (block.get("text") or "").strip().splitlines()
            head = txt[0][:140] if txt else ""
            return f"• 💬 {head}" if head else None
        if btype == "thinking":
            thought = (block.get("thinking") or "").strip().splitlines()
            head = thought[0][:140] if thought else ""
            return f"• 💭 *{head}*" if head else None
        return None

    @staticmethod
    def _is_subagent_finished(entries: list[dict]) -> bool:
        """Subagent considered finished when the latest assistant entry has
        no tool_use blocks — i.e. it stopped to deliver a final response."""
        for e in reversed(entries):
            if e.get("type") != "assistant":
                continue
            msg = e.get("message")
            if not isinstance(msg, dict):
                return False
            content = msg.get("content")
            if not isinstance(content, list):
                return False
            return not any(
                isinstance(b, dict) and b.get("type") == "tool_use"
                for b in content
            )
        return False

    def _render_subagent_embed(
        self,
        block: SubagentBlock,
        last_actions: list[str],
        total_actions: int,
        finished: bool,
    ) -> discord.Embed:
        status = "finished" if finished else "running"
        # finished_at may not have been set yet on the first observed-finished
        # refresh; fall back to now to avoid a None subtraction.
        end = (block.finished_at if finished else None) or time.time()
        elapsed = end - block.started_at
        dur = (
            f"{elapsed:.0f}s" if elapsed < 60 else f"{elapsed / 60:.1f}m"
        )
        # Color cues: yellow while running, green when finished cleanly.
        color = 0x57F287 if finished else 0xFEE75C  # discord brand yellow/green

        # Discord embed.description hard cap is 4096; truncate safely.
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

    def _track_tui_handler_task(self, task_id: str, handler_task: asyncio.Task) -> None:
        """Track a TUI handler task and remove it from tracking when it completes.

        The done-callback runs via `loop.call_soon`, so it can fire AFTER
        a successor handler has already been registered for the same
        task_id (when the previous handler finished moments before the
        new one was spawned). Without the identity check the callback
        would happily evict the new in-flight handler from the map,
        making it invisible to kill_task / _mark_stopped — and a TUI
        prompt would sit there for the full router timeout. Only pop
        when the entry is still THIS task.
        """
        def _evict(t: asyncio.Task, k: str = task_id) -> None:
            if self._tui_handler_tasks.get(k) is t:
                self._tui_handler_tasks.pop(k, None)

        handler_task.add_done_callback(_evict)
        self._tui_handler_tasks[task_id] = handler_task

    async def _on_notification(self, body: dict) -> None:
        """Handle Notification event. Stop typing indicator and spawn TUI handlers asynchronously.

        Spawns TUI handler tasks fire-and-forget so the HTTP request returns immediately.
        Handlers are tracked in _tui_handler_tasks for cancellation on stop_task/kill_task.
        """
        session_id = body.get("session_id")
        if not session_id:
            return
        task = self.get_by_session_id(session_id)
        if task is None:
            return
        task.last_activity = int(time.time())
        await self._persist(task)
        await self._stop_typing(task.task_id)

        # Cancel any pending tool-summary aggregator (no harm; flush_now is idempotent)
        agg = self._aggregators.get(task.task_id)
        if agg is not None:
            await agg.flush_now()

        # If a TUI handler is already in flight (most likely dispatched by
        # `_on_pre_tool_use` for AskUserQuestion / ExitPlanMode just before
        # this Notification arrived), don't double-post. The Notification's
        # role is just to confirm claude is blocking; the structured
        # prompt has already been sent.
        existing = self._tui_handler_tasks.get(task.task_id)
        if existing is not None and not existing.done():
            return

        # Otherwise it's a generic stall (free-text input expected). Spawn
        # the standard free-text-stall handler.
        handler_task = asyncio.create_task(
            self._handle_free_text_stall(task),
            name=f"tui-free_text-{task.task_id[:8]}",
        )
        self._track_tui_handler_task(task.task_id, handler_task)


    async def _on_session_end(self, body: dict) -> None:
        """Handle SessionEnd event.

        Checks exit_reason: None/empty/"exit" = normal; anything else = abnormal.
        - Abnormal exit → flip status to 'crashed', post 💥, archive thread, cleanup settings.
        - Normal exit from 'running' task → flip status to 'stopped', archive thread.
        - Already stopped/crashed → idempotent (no status change).
        """
        session_id = body.get("session_id")
        if not session_id:
            return
        task = self.get_by_session_id(session_id)
        if task is None:
            return
        await self._stop_typing(task.task_id)

        fut = self._stop_futures.get(task.task_id)
        if fut is not None and not fut.done():
            fut.set_result(None)

        exit_reason = body.get("exit_reason")
        is_abnormal = exit_reason not in (None, "exit", "")
        # 'None' means the field wasn't sent (older Claude versions) — assume normal.
        # '' is empty — assume normal.

        if is_abnormal and task.status not in ("stopped", "crashed"):
            task.status = "crashed"
            task.last_activity = int(time.time())
            await self._persist(task)
            # Catch BotNotReady (early-startup window) and any other post
            # error; we still need to do the rest of the bookkeeping or
            # we'll leave the task indexed-but-crashed with no archive.
            try:
                await self._bot.post(
                    f"💥 Claude process exited (`{exit_reason}`)",
                    thread_id=task.thread_id,
                )
            except BotNotReady:
                logger.info(
                    "crash notice deferred (bot not ready) for task %s",
                    task.task_id,
                )
            except Exception:
                logger.exception("failed to post crash notice for task %s", task.task_id)
            await self._archive_thread(task.thread_id)
            _cleanup_task_artifacts(task.task_id)
            # Remove from live indexes; crashed tasks should not be returned by get_by_thread_id/get_by_session_id
            self._by_thread_id.pop(task.thread_id, None)
            if task.current_claude_session_id is not None:
                self._by_session_id.pop(task.current_claude_session_id, None)
            self._streamer_locks.pop(task.task_id, None)
        elif task.status == "running":
            # Graceful exit; mark stopped. Note: stop_task also calls _mark_stopped which
            # archives the thread, so we may double-archive here. That's OK — archive_thread
            # is idempotent.
            task.status = "stopped"
            task.last_activity = int(time.time())
            await self._persist(task)
            await self._archive_thread(task.thread_id)
            _cleanup_task_artifacts(task.task_id)
            # Remove from live indexes; stopped tasks should not be returned by get_by_thread_id/get_by_session_id
            self._by_thread_id.pop(task.thread_id, None)
            if task.current_claude_session_id is not None:
                self._by_session_id.pop(task.current_claude_session_id, None)
            self._streamer_locks.pop(task.task_id, None)

    async def _on_subagent_stop(self, body: dict) -> None:
        """Handle SubagentStop event: refresh subagent blocks so any agents
        that just finished get their final-state edit."""
        session_id = body.get("session_id")
        if not session_id:
            return
        task = self.get_by_session_id(session_id)
        if task is None:
            return
        await self._refresh_subagent_blocks(task)

    async def _on_pre_compact(self, body: dict) -> None:
        """Handle PreCompact event. No bridge-side action — claude rotates
        the session_id at the next SessionStart, which is where we re-bind."""
        logger.debug("PreCompact received")

    # Brief wait before streaming preceding assistant text on a TUI
    # prompt, so the transcript writer has time to flush the assistant
    # entry that carries the lead-in text. Tests override to 0.
    _PRE_PROMPT_FLUSH_SECS: float = 0.5

    async def _flush_pending_text_before_prompt(self, task: Task) -> None:
        """Stream any preceding assistant text before posting a TUI prompt.

        Why: PreToolUse fires the moment claude decides to invoke
        AskUserQuestion / ExitPlanMode, but the assistant entry that
        carries the introductory text alongside the tool_use block isn't
        always flushed to the JSONL transcript yet at that instant. If
        we post the prompt now, Discord shows it before the lead-in
        text — order inverted vs the TUI. Sleep briefly to let the
        transcript writer catch up, then run the streamer so the text
        lands first.
        """
        if self._PRE_PROMPT_FLUSH_SECS > 0:
            await asyncio.sleep(self._PRE_PROMPT_FLUSH_SECS)
        try:
            await self._stream_assistant_progress(task)
        except Exception:
            logger.exception(
                "pre-prompt streamer failed for task %s", task.task_id
            )

    async def _handle_ask_user_question(self, task: Task, pending: dict) -> None:
        """Handle AskUserQuestion prompt: post each question to Discord with
        option reactions, in sequence.

        Single-select: numeric reactions resolve immediately to the selected
        option's number, which is typed into the pane (Enter advances).

        Multi-select: numeric reactions toggle freely; ✅ confirms. The bridge
        types each toggled option's digit into the pane (TUI hot-keys toggle),
        then sends Tab (byte 9) to advance to the next question.

        After all questions are answered, claude's TUI shows a final submit
        screen — the bridge sends Enter (byte 13) to confirm the whole batch.
        """
        # Give the transcript writer a beat to flush the assistant entry
        # holding the just-decided tool_use (often it carries preceding text
        # like "Here are the options…" alongside the tool_use block). Then
        # stream that text so it lands in Discord BEFORE the question — same
        # order as the TUI.
        await self._flush_pending_text_before_prompt(task)
        if not self._approval_router:
            logger.warning(
                "approval_router not configured; cannot dispatch TUI prompt for task %s",
                task.task_id,
            )
            return
        questions = pending["input"].get("questions") or []
        if not questions:
            return await self._handle_free_text_stall(task)

        total = len(questions)
        any_answered = False
        logger.info(
            "ask_question: dispatching %d question(s) for task %s",
            total, task.task_id[:8],
        )
        for idx, q in enumerate(questions, start=1):
            options = [opt.get("label", "") for opt in (q.get("options") or [])]
            if not options:
                logger.info(
                    "ask_question: Q%d/%d has no options → free_text_stall",
                    idx, total,
                )
                await self._handle_free_text_stall(task)
                continue

            is_multi = bool(q.get("multiSelect"))
            logger.info(
                "ask_question: Q%d/%d multi=%s n_options=%d",
                idx, total, is_multi, len(options),
            )
            counter = f" ({idx}/{total})" if total > 1 else ""
            # Mention only on the first question — subsequent ones are
            # immediate follow-ups, no need to spam notifications.
            mention = self._notify_mention_prefix() if idx == 1 else ""
            lines = [f"{mention}❓ **{q.get('question', '?')}**{counter}"]
            if is_multi:
                lines.append(
                    "_(multi-select: tap each option you want, then ✅ to submit)_"
                )
            for i, opt in enumerate(q.get("options") or [], start=1):
                emoji = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"][i - 1] if i <= 4 else f"{i}."
                label = opt.get("label", "")
                desc = opt.get("description", "")
                if desc:
                    lines.append(f"{emoji} **{label}** — {desc}")
                else:
                    lines.append(f"{emoji} {label}")
            body = "\n".join(lines)

            request_id = str(uuid.uuid4())
            answer, source = await self._approval_router.request_tui_answer(
                request_id=request_id,
                task_id=task.task_id,
                thread_id=task.thread_id,
                pane_id=task.zellij_pane_id or "",
                kind="multi_select" if is_multi else "ask_question",
                prompt_body=body,
                options=options,
            )
            logger.info(
                "ask_question: Q%d/%d answered source=%s answer=%r",
                idx, total, source, answer,
            )
            if source in ("cancelled", "timeout", "post_failed"):
                # User answered in zellij or it failed; stop dispatching the
                # remaining questions and don't send the final submit Enter
                # (claude is already moving on or stalled).
                logger.info(
                    "ask_question: bailing out of loop after Q%d/%d (source=%s)",
                    idx, total, source,
                )
                return

            if not task.zellij_pane_id:
                return

            if is_multi:
                # answer is list[int] of 0-based selected indices.
                if not isinstance(answer, list):
                    return
                # Type each toggle digit (TUI hot-keys), then Tab to advance.
                key_bytes: list[int] = []
                for sel in answer:
                    key_bytes.append(ord(str(sel + 1)))
                key_bytes.append(9)  # Tab
                try:
                    await self._zellij.send_keys(task.zellij_pane_id, *key_bytes)
                except Exception:
                    logger.exception(
                        "failed to type multi-select keys for task %s", task.task_id
                    )
            else:
                # Single-select: existing flow types `answer + "\n"` which
                # selects + advances.
                await self._inject_to_pane(task, answer, source)

            any_answered = True
            # Brief pause so claude's TUI has time to advance before we post
            # the next prompt (or the final Enter).
            await asyncio.sleep(0.5)

        # After the last question, the TUI shows a "submit" screen — Enter
        # confirms and lets the AskUserQuestion tool return.
        if any_answered and task.zellij_pane_id:
            logger.info(
                "ask_question: sending final submit Enter for task %s",
                task.task_id[:8],
            )
            try:
                await self._zellij.send_keys(task.zellij_pane_id, 13)
            except Exception:
                logger.exception(
                    "failed to send final submit Enter for task %s", task.task_id
                )

    async def _handle_exit_plan_mode(self, task: Task, pending: dict) -> None:
        """Handle ExitPlanMode prompt: post plan to Discord with approve/reject reactions."""
        await self._flush_pending_text_before_prompt(task)
        if not self._approval_router:
            logger.warning("approval_router not configured; cannot dispatch TUI prompt for task %s", task.task_id)
            return
        plan = pending["input"].get("plan") or "(empty plan)"
        body = (
            f"{self._notify_mention_prefix()}📋 **Plan ready for review**\n\n{plan}\n\n"
            f"React ✅ to approve, ❌ to reject, or reply in this thread to leave feedback."
        )
        request_id = str(uuid.uuid4())
        answer, source = await self._approval_router.request_tui_answer(
            request_id=request_id,
            task_id=task.task_id,
            thread_id=task.thread_id,
            pane_id=task.zellij_pane_id or "",
            kind="exit_plan",
            prompt_body=body,
        )
        await self._inject_to_pane(task, answer, source)

    async def _handle_free_text_stall(self, task: Task) -> None:
        """Handle free-text stall: post generic waiting notice."""
        if not self._approval_router:
            logger.warning("approval_router not configured; cannot dispatch TUI prompt for task %s", task.task_id)
            return
        request_id = str(uuid.uuid4())
        body = (
            f"{self._notify_mention_prefix()}🟡 Claude is waiting for input. "
            "Reply in this thread or type in zellij."
        )
        answer, source = await self._approval_router.request_tui_answer(
            request_id=request_id,
            task_id=task.task_id,
            thread_id=task.thread_id,
            pane_id=task.zellij_pane_id or "",
            kind="free_text",
            prompt_body=body,
        )
        await self._inject_to_pane(task, answer, source)

    async def _inject_to_pane(self, task: Task, answer: str, source: str) -> None:
        """Inject the answer to the pane. Short-circuit if cancelled/timed out/post failed."""
        if source in ("cancelled", "timeout", "post_failed"):
            return
        if not answer:
            return
        if not task.zellij_pane_id:
            return
        try:
            await self._zellij.write_to_pane(task.zellij_pane_id, answer + "\n")
        except Exception:
            logger.exception("failed to inject TUI answer into pane for task %s", task.task_id)

    async def write_initial_prompt(self, task_id: str, prompt: str) -> None:
        """Write initial prompt to a task's zellij pane after session bind.

        Looks up the task by task_id. If pane_id is None or task is missing,
        logs warning and returns. Otherwise writes prompt + newline and bumps last_activity.
        """
        task = self.get_by_task_id(task_id)
        if task is None:
            logger.warning("write_initial_prompt: task_id %s not found", task_id)
            return
        if task.zellij_pane_id is None:
            logger.warning("write_initial_prompt: task %s has no pane_id", task_id)
            return
        await self._zellij.write_to_pane(task.zellij_pane_id, prompt + "\n")
        task.last_activity = int(time.time())
        await self._persist(task)

    async def generate_thread_name(self, task_id: str, *, timeout: float = 30.0) -> str | None:
        """Use `claude -p` to suggest a short kebab-case name for the task's
        thread, derived from the first user prompt + first assistant response
        in the transcript. Returns the bare name (no quotes), or None if no
        transcript / generation failed.
        """
        task = self.get_by_task_id(task_id)
        if task is None or not task.current_transcript_path:
            return None
        path = Path(task.current_transcript_path)
        if not path.is_file():
            return None

        first_user: str | None = None
        first_assistant: str | None = None
        for e in transcript.read_entries(path):
            if e.get("isSidechain") is True or e.get("isMeta") is True:
                continue
            t = e.get("type")
            msg = e.get("message")
            if first_user is None and t == "user" and isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    first_user = content
            elif first_user and first_assistant is None and t == "assistant" and isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, list):
                    texts = [
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    joined = " ".join(t for t in texts if t).strip()
                    if joined:
                        first_assistant = joined
            if first_user and first_assistant:
                break

        if not first_user:
            return None

        prompt = (
            "Generate a short kebab-case name (3-5 words, lowercase, hyphens only, "
            "no leading/trailing whitespace, no quotes, no explanation) summarizing "
            "this conversation. Reply with ONLY the name on a single line.\n\n"
            f"USER: {first_user[:500]}\n\n"
            f"ASSISTANT: {(first_assistant or '')[:500]}"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude",
                "-p",
                prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("generate_thread_name: claude -p timed out for task %s", task_id)
            return None
        except Exception:
            logger.exception("generate_thread_name: claude -p failed for task %s", task_id)
            return None
        if proc.returncode != 0:
            return None
        # Take the first non-empty line; claude sometimes adds extra text despite
        # instructions.
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            s = line.strip().strip('"').strip("'")
            if s:
                return s
        return None

    async def invoke_skill(
        self, task_id: str, skill_name: str, args: str | None = None
    ) -> None:
        """Send `/<skill_name>[ <args>]` to the task's pane so the claude TUI
        invokes the skill. Raises TaskNotFound if the task isn't tracked or
        TaskSpawnError if the pane isn't ready yet.
        """
        task = self.get_by_task_id(task_id)
        if task is None:
            raise TaskNotFound(f"task {task_id[:8]} not found")
        if task.zellij_pane_id is None:
            raise TaskSpawnError(f"task {task_id[:8]} has no pane yet")
        text = f"/{skill_name}"
        if args:
            text += f" {args}"
        text += "\n"
        await self._zellij.write_to_pane(task.zellij_pane_id, text)
        task.last_activity = int(time.time())
        await self._persist(task)

    async def list_tasks(self) -> list[Task]:
        """Return active tasks ordered by last_activity DESC.

        Filters out 'stopped' and 'crashed' rows. Returns in-memory tasks;
        load_from_db at boot ensures freshness.
        """
        return sorted(
            (t for t in self._by_task_id.values() if t.status in ("spawning", "running")),
            key=lambda t: t.last_activity,
            reverse=True,
        )

    async def stop_task(self, task_id: str, *, timeout: float = 5.0) -> bool:
        """Gracefully stop a task.

        Sends `/exit\\n` to the pane, waits up to `timeout` for SessionEnd.
        Returns True if SessionEnd was observed, False if it timed out.
        On success or timeout: archives the thread, flips status to 'stopped'.

        Raises TaskNotFound if task_id doesn't exist.
        """
        task = self.get_by_task_id(task_id)
        if task is None:
            raise TaskNotFound(task_id)

        # Guard: already stopped or crashed; idempotent no-op
        if task.status not in {"running", "spawning"}:
            return True

        if task.zellij_pane_id is None:
            # Mid-spawn; treat as immediate fail-safe stop
            await self._mark_stopped(task)
            return True

        # Set up a future that SessionEnd handler will resolve.
        loop = asyncio.get_running_loop()
        self._stop_futures[task_id] = loop.create_future()
        try:
            await self._zellij.write_to_pane(task.zellij_pane_id, "/exit\n")
            try:
                await asyncio.wait_for(self._stop_futures[task_id], timeout=timeout)
                stopped_cleanly = True
            except asyncio.TimeoutError:
                stopped_cleanly = False
        finally:
            self._stop_futures.pop(task_id, None)

        await self._mark_stopped(task)
        return stopped_cleanly

    async def kill_task(self, task_id: str) -> None:
        """Immediately close a task's pane.

        Marks status='crashed' and archives the thread.
        Raises TaskNotFound if task_id doesn't exist.

        Bridge bookkeeping (DB row, indexes, artifact cleanup) runs
        unconditionally even if `close_pane` raises — `zellij.close_pane`
        already swallows ZellijError internally, but we don't want a
        bug elsewhere (subprocess explosion, asyncio cancel) to leave
        the task in `running` while commands.py reports a successful
        kill to the user.
        """
        task = self.get_by_task_id(task_id)
        if task is None:
            raise TaskNotFound(task_id)

        # Guard: already stopped or crashed; idempotent no-op
        if task.status not in {"running", "spawning"}:
            return

        if task.zellij_pane_id is not None:
            try:
                await self._zellij.close_pane(task.zellij_pane_id)
            except Exception:
                logger.exception(
                    "kill_task %s: close_pane raised; continuing with bookkeeping",
                    task.task_id,
                )

        task.status = "crashed"
        task.last_activity = int(time.time())
        await self._persist(task)
        await self._archive_thread(task.thread_id)
        # Remove from live indexes; crashed tasks should not be returned by get_by_thread_id/get_by_session_id
        self._by_thread_id.pop(task.thread_id, None)
        if task.current_claude_session_id is not None:
            self._by_session_id.pop(task.current_claude_session_id, None)
        # Clean up typing task (cancel without waiting for graceful stop), aggregator, and TUI handler task
        await self._stop_typing(task.task_id)
        self._aggregators.pop(task.task_id, None)
        handler_task = self._tui_handler_tasks.pop(task.task_id, None)
        if handler_task is not None and not handler_task.done():
            handler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await handler_task
        # Drop the per-task streamer lock; mirrors _mark_stopped.
        self._streamer_locks.pop(task.task_id, None)
        # Clean up the task-scoped settings file
        _cleanup_task_artifacts(task_id)

    async def restart_task(self, task_id: str) -> Task:
        """Resume a task by spawning a new claude with --resume <session_id>.

        Reuses the existing pane if it's still alive; spawns a new pane otherwise.
        Raises TaskNotFound if task_id doesn't exist.
        Raises TaskRestartError if there's no claude session to resume.
        """
        task = self.get_by_task_id(task_id)
        if task is None:
            raise TaskNotFound(task_id)

        if task.current_claude_session_id is None:
            raise TaskRestartError("no claude session to resume")

        pane_id = task.zellij_pane_id
        pane_alive = pane_id is not None and await self._is_pane_alive(pane_id)

        if pane_alive:
            # Just write the resume command into the existing pane; user sees the new banner.
            await self._zellij.write_to_pane(
                pane_id,
                f"\nclaude --resume {task.current_claude_session_id}\n",
            )
            # Status stays 'running' — SessionStart will rebind on the new session id
            task.last_activity = int(time.time())
            await self._persist(task)
            return task

        # Spawn a fresh pane with the resumed session.
        settings_path = _write_task_settings(task.task_id)
        env = self._build_spawn_env(task.task_id)
        tab_name = f"cc-{task.task_id[:8]}"
        layout_path = _write_task_layout(
            task.task_id,
            env=env,
            claude_argv=[
                "--settings", str(settings_path),
                "--resume", task.current_claude_session_id,
            ],
            tab_name=tab_name,
        )
        new_pane_id = await self._zellij.spawn_task(
            cwd=task.cwd,
            pane_name=tab_name,
            layout_path=str(layout_path),
        )
        task.zellij_pane_id = new_pane_id
        task.last_activity = int(time.time())
        await self._index(task)
        await self._persist(task)
        return task

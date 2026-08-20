"""Identity-aware Slack TaskRegistry for Polytoken daemon sessions.

The registry deliberately speaks only the normalized Slack contract.  A task is
bound to a ``ConversationKey`` (team, channel, root timestamp), never to a
provider-specific thread object.  Incoming messages are authenticated and
normalized before routing; daemon output is rendered as Slack text/blocks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, TYPE_CHECKING

import aiosqlite

from bridge import voice
from bridge.daemon_supervisor import DaemonSupervisor, DaemonSupervisorError
from bridge.domain import (
    ActorId,
    ChannelId,
    ConversationKey,
    EventId,
    Mode,
    Owner,
    Participant,
    ParticipantKind,
    PendingInterrogative as StoredInterrogative,
    TeamId,
    TextPin,
)
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
from bridge.polytoken_client import PolytokenClient, PolytokenClientError, SseEnvelope, TurnInFlight
from bridge.redaction import safe_error, safe_log
from bridge.state import (
    clear_pending_interrogative,
    consume_pending_interrogative,
    delete_participant,
    delete_text_pin,
    delete_root,
    end_promotion,
    get_active_promotion,
    get_pending_interrogative,
    get_root,
    get_runtime,
    list_runtime,
    replace_runtime_binding,
    restore_runtime_binding,
    RuntimeRow,
    update_runtime,
    upsert_runtime,
    get_text_pin,
    list_participants,
    list_promotion_bindings,
    list_pending_promotion_journals,
    create_promotion_journal,
    update_promotion_journal,
    list_text_pins,
    mark_event_seen,
    put_pending_interrogative,
    promote_root as persist_promotion,
    upsert_participant,
    upsert_root,
    upsert_text_pin,
)

if TYPE_CHECKING:
    from bridge.bot import Bot

log = logging.getLogger(__name__)

# Attachments are authenticated by Bot.download_file and bounded before being
# handed to the daemon.
BRIDGE_STATE_DIR = Path(os.environ.get(
    "BRIDGE_STATE_DIR",
    str(Path.home() / ".local" / "state" / "claude-slack-bridge"),
)).expanduser()
ATTACHMENTS_DIR = BRIDGE_STATE_DIR / "attachments"
ATTACHMENT_TTL_SECS = int(os.environ.get("BRIDGE_ATTACHMENT_TTL_SECS", str(7 * 86400)))
MAX_ATTACHMENT_BYTES = int(os.environ.get("BRIDGE_MAX_ATTACHMENT_BYTES", str(10 * 1024 * 1024)))
MAX_ATTACHMENTS_PER_MESSAGE = int(os.environ.get("BRIDGE_MAX_ATTACHMENTS_PER_MESSAGE", "10"))
MAX_ATTACHMENT_AGGREGATE_BYTES = int(os.environ.get("BRIDGE_MAX_ATTACHMENT_AGGREGATE_BYTES", str(50 * 1024 * 1024)))
PROVENANCE_VERSION = 1
SUBAGENT_BLOCK_MAX_ACTIONS = 5
SUBAGENT_EDIT_THROTTLE_SECS = 1.5
MAX_ATTACHMENTS_PER_POST = 10
DEFAULT_APP_EXCHANGE_BUDGET = int(os.environ.get("BRIDGE_APP_EXCHANGE_BUDGET", "20"))
ATTACH_MARKER = re.compile(r"\[\[attach:\s*([^\]]+?)\s*\]\]")


@dataclass(frozen=True, slots=True)
class SlackActor:
    """Provider-neutral actor identity supplied by the Slack adapter."""

    actor_id: str
    is_app: bool = False
    display_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "actor_id", str(self.actor_id).strip())
        if not self.actor_id:
            raise ValueError("actor_id must not be empty")
        if self.display_name is not None:
            object.__setattr__(self, "display_name", self.display_name.strip() or None)


@dataclass(frozen=True, slots=True)
class SlackFile:
    """Authenticated Slack file metadata; bytes are fetched by ``Bot``."""

    url: str
    filename: str = "attachment"
    size: int | None = None
    mimetype: str | None = None
    file_id: str | None = None


@dataclass(frozen=True, slots=True)
class MessageProvenance:
    """Authenticated metadata; never parsed from the raw body."""

    team_id: str
    channel_id: str
    root_ts: str
    message_ts: str | None
    event_id: str | None
    actor_id: str
    actor_kind: str = "human"
    version: int = PROVENANCE_VERSION

    def wire(self, body: str) -> str:
        """Serialize an injection-resistant, versioned prompt payload.

        The original body is retained as a JSON string value, not concatenated
        into metadata, so hostile text cannot forge provenance fields.
        """
        payload = {
            "version": self.version,
            "provenance": {
                "team_id": self.team_id, "channel_id": self.channel_id,
                "root_ts": self.root_ts, "message_ts": self.message_ts,
                "event_id": self.event_id, "actor_id": self.actor_id,
                "actor_kind": self.actor_kind,
            },
            "body": body,
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class PromptEnvelope:
    provenance: MessageProvenance
    body: str


@dataclass(frozen=True, slots=True)
class SlackMessage:
    """Normalized inbound Slack event used by ``maybe_route_message``."""

    team_id: str
    channel_id: str
    root_ts: str
    actor: SlackActor
    text: str = ""
    event_id: str | None = None
    message_ts: str | None = None
    files: tuple[SlackFile, ...] = ()

    @property
    def actor_id(self) -> str:
        return self.actor.actor_id


# Short names are useful to adapters and make the normalized API discoverable.
Actor = SlackActor
File = SlackFile
Message = SlackMessage


@dataclass
class SubagentBlock:
    handle: str
    attribution: str
    started_at: float
    message_ts: str | None = None
    finished_at: float | None = None
    last_edit_at: float = 0.0
    actions: list[str] = field(default_factory=list)


@dataclass
class Task:
    """A daemon plus its Slack identity and authorization policy."""

    task_id: str
    team_id: str
    channel_id: str
    root_ts: str
    owner_user_id: str
    mode: str
    cwd: str
    status: str = "spawning"
    polytoken_session_id: str | None = None
    port: int | None = None
    created_at: int = 0
    last_activity: int = 0
    app_exchange_budget: int = DEFAULT_APP_EXCHANGE_BUDGET
    app_exchanges: int = 0
    owner_alerted: bool = False
    promotion_state: str = "none"
    binding_id: str | None = None
    cleanup_pending: bool = False
    channel_owned: bool = False
    subagent_blocks: dict[str, SubagentBlock] = field(default_factory=dict)
    last_envelope: PromptEnvelope | None = None
    # Runtime-only path from ``polytoken sessions --format json``.  It is not
    # persisted in SQLite; startup reconciliation re-discovers it by session id.
    credential_file_path: str | None = None

    @property
    def key(self) -> ConversationKey:
        return ConversationKey(TeamId(self.team_id), ChannelId(self.channel_id), self.root_ts)


class TaskSpawnError(Exception):
    pass


class TaskNotFound(Exception):
    pass


class TaskRestartError(Exception):
    pass


class TaskPrivilegeError(PermissionError):
    """The actor is not the owner of the task/configuration."""


class TaskRoutingError(ValueError):
    pass


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _actor(value: Any) -> SlackActor:
    if isinstance(value, SlackActor):
        return value
    if isinstance(value, str):
        return SlackActor(value)
    nested = _value(value, "actor", None) or _value(value, "user", None)
    # Slack's raw ``user`` field is commonly a bare U... string.  Only descend
    # into structured actor objects; replacing the mapping with that string
    # would discard normalized actor_id/bot_id fields.
    if nested is not None and nested is not value and not isinstance(nested, str):
        value = nested
    actor_id = (
        _value(value, "actor_id", None)
        or _value(value, "user_id", None)
        or _value(value, "id", None)
        or _value(value, "bot_id", None)
        or (_value(value, "user", None) if isinstance(_value(value, "user", None), str) else None)
    )
    is_app = bool(_value(value, "is_app", False) or _value(value, "bot_id", None))
    return SlackActor(str(actor_id or ""), is_app=is_app, display_name=_value(value, "display_name", None))


def _file(value: Any) -> SlackFile:
    if isinstance(value, SlackFile):
        return value
    return SlackFile(
        url=str(_value(value, "url_private_download", None) or _value(value, "url", "")),
        filename=str(_value(value, "name", None) or _value(value, "filename", "attachment")),
        size=_value(value, "size", None),
        mimetype=_value(value, "mimetype", None) or _value(value, "mime_type", None),
        file_id=_value(value, "id", None),
    )


def normalize_message(value: Any) -> SlackMessage:
    """Translate a Slack adapter object/dict without retaining provider objects.

    Socket Mode dispatch receives Bot's authenticated normalized envelope.  Keep
    the nested provider event as a compatibility source, but let normalized
    top-level fields win (notably ``actor_id``, ``team_id``, and ``root_ts``).
    """
    if isinstance(value, SlackMessage):
        return value
    source = value
    nested_event = _value(value, "event", None)
    if isinstance(nested_event, Mapping):
        merged = dict(nested_event)
        if isinstance(value, Mapping):
            merged.update(value)
        source = merged
    channel = _value(source, "channel_id", None)
    if channel is None:
        channel_obj = _value(source, "channel", None)
        channel = _value(channel_obj, "id", channel_obj)
    team = _value(source, "team_id", None) or _value(source, "team", None)
    actor = _actor(source)
    message_ts = _value(source, "message_ts", None) or _value(source, "ts", None)
    root = _value(source, "root_ts", None) or _value(source, "thread_ts", None) or message_ts
    files = _value(source, "files", None) or _value(source, "attachments", ()) or ()
    raw_event_id = _value(value, "event_id", None) or _value(value, "id", None)
    event_id = (raw_event_id if isinstance(raw_event_id, str)
                else str(raw_event_id) if raw_event_id is not None and not isinstance(raw_event_id, Mapping)
                else None)
    return SlackMessage(
        team_id=str(team or ""),
        channel_id=str(channel or ""),
        root_ts=str(root or ""),
        actor=actor,
        text=str(_value(source, "text", None) or _value(source, "content", "")),
        event_id=event_id,
        message_ts=str(message_ts) if message_ts is not None else None,
        files=tuple(_file(item) for item in files),
    )


def _parse_attach_markers(text: str) -> tuple[str, list[Path]]:
    paths: list[Path] = []
    for match in ATTACH_MARKER.finditer(text):
        candidate = Path(match.group(1).strip())
        if candidate.is_absolute() and candidate.is_file():
            paths.append(candidate)
    return ATTACH_MARKER.sub("", text).strip(), paths[:MAX_ATTACHMENTS_PER_POST]


def _cleanup_task_attachments(task_id: str) -> bool:
    directory = ATTACHMENTS_DIR / task_id
    if not directory.exists():
        return True
    ok = True
    for child in list(directory.iterdir()):
        try:
            if child.is_file() or child.is_symlink():
                child.unlink()
            elif child.is_dir():
                shutil.rmtree(child)
        except OSError:
            ok = False
            log.warning("failed to remove attachment")
    try:
        directory.rmdir()
    except OSError:
        ok = False
    return ok


def sweep_old_attachments(*, ttl_secs: int = ATTACHMENT_TTL_SECS) -> None:
    if not ATTACHMENTS_DIR.exists():
        return
    cutoff = time.time() - ttl_secs
    for directory in ATTACHMENTS_DIR.iterdir():
        if not directory.is_dir():
            continue
        for child in directory.iterdir():
            with contextlib.suppress(OSError):
                if child.is_file() and child.stat().st_mtime < cutoff:
                    child.unlink()
        with contextlib.suppress(OSError):
            if not any(directory.iterdir()):
                directory.rmdir()


class _ToolSummaryAggregator:
    FLUSH_WINDOW = 1.0
    SLOW_FLUSH_WINDOW = 5.0

    def __init__(self, bot: "Bot", task: Task) -> None:
        self._bot = bot
        self._task = task
        self._lines: list[str] = []
        self._flush_task: asyncio.Task | None = None
        self._slow_mode = False

    def append(self, line: str) -> None:
        self._lines.append(line)
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_after_window())

    async def _flush_after_window(self) -> None:
        try:
            await asyncio.sleep(self.SLOW_FLUSH_WINDOW if self._slow_mode else self.FLUSH_WINDOW)
        except asyncio.CancelledError:
            return
        await self.flush_now()

    async def flush_now(self) -> None:
        if self._flush_task is not None and self._flush_task is not asyncio.current_task() and not self._flush_task.done():
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
        if not self._lines:
            return
        body = "\n".join(self._lines)
        self._lines.clear()
        try:
            await self._bot.post(body, channel_id=self._task.channel_id, root_ts=self._task.root_ts)
        except Exception as exc:
            if getattr(exc, "status", None) == 429:
                self._slow_mode = True
            self._lines[0:0] = body.splitlines()
            log.exception("failed to flush Slack tool summary")


class TaskRegistry:
    """Route normalized Slack messages to one Polytoken daemon per task."""

    def __init__(
        self,
        conn: aiosqlite.Connection,
        bot: "Bot | None",
        supervisor: DaemonSupervisor,
        *,
        app_actor_id: str | None = None,
        home_channel_id: str | None = None,
        app_exchange_budget: int = DEFAULT_APP_EXCHANGE_BUDGET,
    ) -> None:
        self._conn = conn
        self._bot = bot
        self._supervisor = supervisor
        # ``app_actor_id`` is the bridge's own Slack user identity.  Keep the
        # external app's stable B... identity separate: it is a participant,
        # not the bridge itself.
        self.app_actor_id = getattr(bot, "bot_user_id", None) or app_actor_id
        self._bridge_bot_id = getattr(bot, "bot_id", None)
        self._daemon_presence_known = False
        self._known_daemon_sessions: set[str] = set()
        self.home_channel_id = home_channel_id or getattr(bot, "home_channel_id", None)
        if app_exchange_budget <= 0:
            raise ValueError("app_exchange_budget must be positive")
        self.app_exchange_budget = app_exchange_budget
        self._by_task_id: dict[str, Task] = {}
        self._by_key: dict[ConversationKey, Task] = {}
        self._by_session_id: dict[str, Task] = {}
        self._disabled_roots: set[ConversationKey] = set()
        self._clients: dict[str, PolytokenClient] = {}
        self._consumers: dict[str, asyncio.Task] = {}
        self._translators: dict[str, Translator] = {}
        self._aggregators: dict[str, _ToolSummaryAggregator] = {}
        self._pending: dict[tuple[ConversationKey, str], StoredInterrogative] = {}
        self._torn_down: set[str] = set()
        self._pin_spawn_locks: dict[ConversationKey, asyncio.Lock] = {}
        self._task_locks: dict[str, asyncio.Lock] = {}
        self._message_seen: set[tuple[str, str]] = set()
        self._startup_notices: list[tuple[Task, str]] = []
        self._deferred_consumers: set[str] = set()

    def bind_bot(self, bot: "Bot") -> None:
        self._bot = bot
        self.app_actor_id = getattr(bot, "bot_user_id", None) or self.app_actor_id
        self._bridge_bot_id = getattr(bot, "bot_id", None)
        self.home_channel_id = self.home_channel_id or getattr(bot, "home_channel_id", None)

    def _require_bot(self) -> "Bot":
        if self._bot is None:
            raise RuntimeError("Slack Bot is not bound")
        return self._bot

    def _require_owner(self, task: Task, actor_id: str | SlackActor | None) -> None:
        candidate = _actor(actor_id).actor_id if actor_id is not None else ""
        if candidate != task.owner_user_id:
            raise TaskPrivilegeError(f"actor {candidate or '<unknown>'} is not task owner")

    def _require_task(self, task_id: str, actor_id: str | SlackActor | None = None) -> Task:
        task = self._by_task_id.get(task_id)
        if task is None:
            raise TaskNotFound(task_id)
        if actor_id is not None:
            self._require_owner(task, actor_id)
        return task

    def get_by_task_id(self, task_id: str) -> Task | None:
        return self._by_task_id.get(task_id)

    def get_by_key(self, key: ConversationKey) -> Task | None:
        return self._by_key.get(key)

    def get_by_conversation(self, team_id: str, channel_id: str, root_ts: str) -> Task | None:
        return self._by_key.get(ConversationKey(team_id, channel_id, root_ts))

    def get_by_session_id(self, session_id: str) -> Task | None:
        return self._by_session_id.get(session_id)

    async def _index(self, task: Task) -> None:
        old = self._by_task_id.get(task.task_id)
        if old is not None and old.key != task.key:
            self._by_key.pop(old.key, None)
            self._disabled_roots.add(old.key)
        self._by_task_id[task.task_id] = task
        self._by_key[task.key] = task
        if task.polytoken_session_id:
            self._by_session_id[task.polytoken_session_id] = task

    def _task_lock(self, task: Task) -> asyncio.Lock:
        return self._task_locks.setdefault(task.task_id, asyncio.Lock())

    def _runtime(self, task: Task) -> RuntimeRow:
        return RuntimeRow(
            task_id=task.task_id, key=task.key, session_id=task.polytoken_session_id,
            port=task.port, status=task.status, cwd=task.cwd,
            owner=Owner(ActorId(task.owner_user_id), mode=Mode(task.mode)),
            created_at=task.created_at, last_activity=task.last_activity,
            app_exchange_budget=task.app_exchange_budget, app_exchanges=task.app_exchanges,
            owner_alerted=task.owner_alerted, promotion_state=task.promotion_state,
            binding_id=task.binding_id, cleanup_pending=task.cleanup_pending,
            channel_owned=task.channel_owned,
        )

    async def _persist_root(self, task: Task, *, now: int | None = None) -> None:
        await upsert_runtime(self._conn, self._runtime(task), now=now)

    async def _persist_task(self, task: Task, *, now: int | None = None) -> None:
        await upsert_runtime(self._conn, self._runtime(task), now=now)

    # -- startup / shutdown -------------------------------------------------

    async def load_from_db(self, *, reconcile_with_daemons: bool = False) -> None:
        """Rehydrate durable runtimes and reconcile their daemon bindings."""
        sessions: dict[str, Any] = {}
        listing_ok = True
        if reconcile_with_daemons:
            try:
                sessions = {str(item.session_id): item for item in await self._supervisor.list_sessions()}
                self._known_daemon_sessions = set(sessions)
                self._daemon_presence_known = True
            except DaemonSupervisorError:
                listing_ok = False
                self._daemon_presence_known = False
        # Incomplete promotions are reconciled after Bot startup. Loading DB
        # must remain side-effect free because Slack is not authenticated yet.
        runtimes = await list_runtime(self._conn)
        # A legacy crash may have persisted the transient rebinding marker before
        # the journal existed.  Never rehydrate that half-bound runtime as live.
        # A pending journal (if present) is repaired after Slack login.
        pending_task_ids = {j.task_id for j in await list_pending_promotion_journals(self._conn)}
        active_keys: set[ConversationKey] = set()
        for runtime in runtimes:
            status = runtime.status
            if runtime.task_id not in pending_task_ids and status in {"rebinding", "promoting"}:
                # Pre-journal legacy rows are unsafe to route. Keep the old
                # binding and mark crashed rather than guessing a new channel.
                status = "crashed"
                await update_runtime(self._conn, runtime.task_id, status=status, promotion_state="failed", channel_owned=False)
            credential_file_path: str | None = None
            if listing_ok and reconcile_with_daemons:
                info = sessions.get(str(runtime.session_id)) if runtime.session_id else None
                if runtime.session_id and info is None and status in {"spawning", "running", "paused", "rebinding"}:
                    status = "crashed"
                    await update_runtime(self._conn, runtime.task_id, status=status, promotion_state="failed", now=int(time.time()))
                    self._startup_notices.append((Task(runtime.task_id, str(runtime.key.team_id), str(runtime.key.channel_id), str(runtime.key.root_id), str(runtime.owner.actor_id), str(runtime.owner.mode), runtime.cwd, status, runtime.session_id, runtime.port, runtime.created_at, runtime.last_activity, runtime.app_exchange_budget, runtime.app_exchanges, runtime.owner_alerted, runtime.promotion_state, runtime.binding_id, runtime.cleanup_pending, runtime.channel_owned, credential_file_path=None), "💥 The session daemon was not found; task is crashed."))
                elif info is not None:
                    credential_file_path = getattr(info, "credential_file_path", None)
                    if runtime.port != info.port or runtime.cwd != info.project_path:
                        await update_runtime(self._conn, runtime.task_id, port=info.port, cwd=info.project_path)
                        runtime = RuntimeRow(runtime.task_id, runtime.key, runtime.session_id, info.port, runtime.status, info.project_path, runtime.owner, runtime.created_at, runtime.last_activity, runtime.app_exchange_budget, runtime.app_exchanges, runtime.owner_alerted, runtime.promotion_state, runtime.binding_id, runtime.cleanup_pending, runtime.channel_owned)
            task = Task(runtime.task_id, str(runtime.key.team_id), str(runtime.key.channel_id), str(runtime.key.root_id), str(runtime.owner.actor_id), str(runtime.owner.mode), runtime.cwd, status, runtime.session_id, runtime.port, runtime.created_at, runtime.last_activity, runtime.app_exchange_budget, runtime.app_exchanges, runtime.owner_alerted, runtime.promotion_state, runtime.binding_id, runtime.cleanup_pending, runtime.channel_owned, credential_file_path=credential_file_path)
            if task.channel_owned and self._bot is not None:
                remember = getattr(self._bot, "remember_owned_channel", None)
                if callable(remember):
                    remember(task.channel_id)
            if status in {"running", "spawning", "paused", "rebinding"}:
                if task.key in active_keys:
                    task.status = "crashed"
                    await update_runtime(self._conn, task.task_id, status="crashed")
                else:
                    active_keys.add(task.key)
            await self._index(task)
            for participant in await list_participants(self._conn, task.key):
                # Reading participants here deliberately preserves APP/HUMAN and display_name.
                pass
            if task.status in {"running", "spawning"}:
                self._deferred_consumers.add(task.task_id)

    async def reconcile_promotion_journals(self) -> None:
        """Restore old bindings and retry verified cleanup after Slack login."""
        if self._bot is None:
            return
        for journal in await list_pending_promotion_journals(self._conn):
            runtime = await get_runtime(self._conn, journal.task_id)
            if runtime is not None:
                # Restore all old-binding fields even when the key already
                # matches: a crash can leave only status/mode/ownership stale.
                # Daemon reconciliation is tri-state: a successful listing
                # proves present/absent, while a failed listing is unknown and
                # must preserve the retryable preexisting status.
                if not self._daemon_presence_known:
                    recovered_status = runtime.status
                elif runtime.session_id and str(runtime.session_id) in self._known_daemon_sessions:
                    recovered_status = "running"
                else:
                    recovered_status = "crashed"
                restored = await restore_runtime_binding(
                    self._conn, journal.task_id, journal.old_key,
                    status=recovered_status,
                    binding_id=journal.old_binding_id,
                )
                task = self.get_by_task_id(journal.task_id)
                if task is not None and restored is not None:
                    self._by_key.pop(task.key, None)
                    task.channel_id = str(journal.old_key.channel_id)
                    task.root_ts = str(journal.old_key.root_id)
                    task.mode = journal.old_mode
                    task.status = restored.status
                    task.promotion_state = restored.promotion_state
                    task.binding_id = journal.old_binding_id
                    task.channel_owned = bool(journal.old_mode == "collaborative" and str(journal.old_key.channel_id) != str(self.home_channel_id or ""))
                    await self._index(task)
                    await self._persist_task(task)
                    if task.status in {"running", "spawning"}:
                        self._deferred_consumers.discard(task.task_id)
                        self._start_consumer(task)
                    else:
                        self._deferred_consumers.discard(task.task_id)
                        consumer = self._consumers.pop(task.task_id, None)
                        if consumer is not None and not consumer.done():
                            consumer.cancel()
            if not journal.new_channel_id:
                await update_promotion_journal(self._conn, journal.journal_id, state="failed", side_effect="restore_old_binding", side_effect_state="complete")
                continue
            try:
                await update_promotion_journal(self._conn, journal.journal_id, state="cleanup_pending", side_effect="archive_channel", side_effect_state="started")
                # Ownership is an in-memory capability and is lost on restart;
                # restore it from the durable journal before archive_channel's
                # independent private/team/member verification.
                remember = getattr(self._require_bot(), "remember_owned_channel", None)
                if callable(remember):
                    remember(journal.new_channel_id)
                await self._require_bot().archive_channel(journal.new_channel_id)
            except Exception:
                await update_promotion_journal(self._conn, journal.journal_id, state="cleanup_pending", side_effect="archive_channel", side_effect_state="failed", error_code="archive_unverified")
            else:
                await update_promotion_journal(self._conn, journal.journal_id, state="failed", side_effect="archive_channel", side_effect_state="complete")

    async def flush_startup_notices(self) -> None:
        if self._bot is None:
            return
        for task, notice in self._startup_notices:
            with contextlib.suppress(Exception):
                await self._post(task, notice)
        self._startup_notices.clear()

    async def attach_task(self, task: Task, *, start_consumer: bool = False) -> Task:
        await self._index(task)
        await self._persist_root(task)
        if start_consumer and task.status in {"running", "spawning"}:
            self._start_consumer(task)
        return task

    async def start_event_consumers(self) -> None:
        for task in list(self._by_task_id.values()):
            if task.status not in {"running", "spawning"}:
                continue
            if self._daemon_presence_known and (
                not task.polytoken_session_id
                or task.polytoken_session_id not in self._known_daemon_sessions
            ):
                self._deferred_consumers.discard(task.task_id)
                continue
            self._start_consumer(task)
        self._deferred_consumers.clear()

    async def shutdown(self) -> None:
        for consumer in list(self._consumers.values()):
            if not consumer.done():
                consumer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await consumer
        self._consumers.clear()
        for client in list(self._clients.values()):
            with contextlib.suppress(Exception):
                await client.aclose()
        self._clients.clear()

    # -- daemon events ------------------------------------------------------

    def _client_for(self, task: Task) -> PolytokenClient:
        if task.port is None:
            raise TaskSpawnError(f"task {task.task_id[:8]} is not connected to a daemon")
        client = self._clients.get(task.task_id)
        if (
            client is None
            or client.port != task.port
            or getattr(client, "credential_file_path", None) != task.credential_file_path
        ):
            if client is not None:
                asyncio.create_task(client.aclose())
            client = PolytokenClient(task.port, credential_file_path=task.credential_file_path)
            self._clients[task.task_id] = client
        return client

    def _start_consumer(self, task: Task) -> None:
        previous = self._consumers.get(task.task_id)
        if previous is not None and not previous.done():
            return
        self._translators.setdefault(task.task_id, Translator())
        self._consumers[task.task_id] = asyncio.create_task(self._consume_events(task.task_id))

    async def _consume_events(self, task_id: str) -> None:
        translator = self._translators.setdefault(task_id, Translator())
        backoff = 1.0
        while True:
            task = self.get_by_task_id(task_id)
            if task is None or task.status not in {"running", "spawning"}:
                return
            try:
                async for envelope in self._client_for(task).stream_events(last_seq=translator.last_seq):
                    for action in translator.handle(envelope):
                        await self._render(task, action)
                backoff = 1.0
            except asyncio.CancelledError:
                return
            except PolytokenClientError as exc:
                log.warning("event stream for %s dropped: %s", task_id[:8], safe_error(exc, "event stream transport failure"))
                if await self._daemon_is_gone(task):
                    await self._handle_daemon_death(task)
                    return
            except Exception:
                log.exception("event consumer failed for %s", task_id[:8])
            await asyncio.sleep(min(backoff, 10.0))
            backoff = min(backoff * 2, 10.0)

    async def _daemon_is_gone(self, task: Task) -> bool:
        if not task.polytoken_session_id:
            return False
        try:
            return await self._supervisor.find_session(task.polytoken_session_id) is None
        except DaemonSupervisorError:
            return False

    async def _handle_daemon_death(self, task: Task) -> None:
        await self._post(task, "💥 The session daemon exited; this task is crashed.")
        await self._teardown_task(task, status="crashed", cancel_consumer=False)

    async def _render(self, task: Task, action: Any) -> None:
        try:
            if isinstance(action, AssistantText):
                if action.subagent_handle:
                    await self._subagent_activity(task, action.subagent_handle, f"• 💬 {action.text[:140]}")
                else:
                    await self._post_assistant_text(task, action.text)
            elif isinstance(action, AssistantThinking):
                if not action.subagent_handle:
                    await self._post(task, f"💭 {action.text}")
            elif isinstance(action, ToolLine):
                self._agg_for(task).append(action.line)
            elif isinstance(action, (ToolDiff, ToolFailure)):
                await self._post(task, action.block if isinstance(action, ToolDiff) else action.line)
            elif isinstance(action, SubagentStarted):
                await self._subagent_started(task, action)
            elif isinstance(action, SubagentActivity):
                await self._subagent_activity(task, action.handle, action.line)
            elif isinstance(action, SubagentCompleted):
                await self._subagent_completed(task, action)
            elif isinstance(action, (AskQuestion, Clarification, Confirmation)):
                await self._post_interrogative(task, action)
            elif isinstance(action, TurnStarted):
                pass  # Slack has no provider-neutral typing contract.
            elif isinstance(action, TurnComplete):
                await self._end_turn(task)
            elif isinstance(action, TurnCancelled):
                await self._end_turn(task)
                await self._post(task, f"🛑 Turn cancelled ({action.reason})")
            elif isinstance(action, ModelError):
                await self._end_turn(task)
                await self._post(task, "⚠ Model error: the daemon could not complete this request.")
            elif isinstance(action, TitleChange):
                await self._post(task, f"*{action.title.strip()[:200]}*")
            elif isinstance(action, StatusNote):
                await self._post(task, action.text)
            elif isinstance(action, AttentionPing):
                await self._post(task, f"<@{task.owner_user_id}> 🔔 {action.summary}")
            elif isinstance(action, ImageResolved):
                log.info("image reference resolved for %s", task.task_id[:8])
            elif isinstance(action, StateRefresh):
                return
            elif isinstance(action, Reconcile):
                await self._handle_reconcile(task, action.reason)
        except Exception:
            log.exception("failed to render %s for %s", type(action).__name__, task.task_id[:8])

    async def _post(self, task: Task, text: str, *, blocks: list[dict[str, Any]] | None = None) -> list[str]:
        return await self._require_bot().post(text, channel_id=task.channel_id, root_ts=task.root_ts, blocks=blocks)

    async def _post_assistant_text(self, task: Task, text: str) -> None:
        cleaned, paths = _parse_attach_markers(text)
        if paths:
            await self._require_bot().post_with_attachments(
                [str(path) for path in paths[:MAX_ATTACHMENTS_PER_POST]], channel_id=task.channel_id, root_ts=task.root_ts, text=cleaned
            )
        elif cleaned:
            await self._post(task, cleaned)

    async def _handle_reconcile(self, task: Task, reason: str) -> None:
        await self._post(task, f"⚠️ Event stream gap detected ({reason}); state was re-synced.")
        state = await self._state_snapshot(task)
        if not state:
            return
        for pending in state.get("pending_interrogatives") or []:
            if isinstance(pending, dict):
                envelope = SseEnvelope(seq=None, session_id=None, emitted_at=None, event=pending)
                for action in self._translators.setdefault(task.task_id, Translator()).handle(envelope):
                    await self._render(task, action)

    # -- Slack blocks / subagents ------------------------------------------

    @staticmethod
    def _subagent_blocks(block: SubagentBlock, actions: list[str], total: int, finished: bool) -> list[dict[str, Any]]:
        status = "finished" if finished else "running"
        elapsed = ((block.finished_at if finished else None) or time.time()) - block.started_at
        duration = f"{elapsed:.0f}s" if elapsed < 60 else f"{elapsed / 60:.1f}m"
        text = "\n".join(actions) or "_(no actions yet)_"
        return [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*🤖 {block.attribution}*\n{text[:2900]}"}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"{status} · {total} actions · {duration}"}]},
        ]

    async def _subagent_started(self, task: Task, action: SubagentStarted) -> None:
        block = SubagentBlock(action.handle, action.subagent_type or action.handle, time.time())
        task.subagent_blocks[action.handle] = block
        try:
            sent = await self._post(task, "", blocks=self._subagent_blocks(block, [], 0, False))
            block.message_ts = sent[0] if sent else None
            block.last_edit_at = time.time()
        except Exception:
            log.exception("failed to post subagent block")

    async def _subagent_activity(self, task: Task, handle: str, line: str) -> None:
        block = task.subagent_blocks.get(handle)
        if block is None:
            block = SubagentBlock(handle, handle, time.time())
            task.subagent_blocks[handle] = block
            sent = await self._post(task, "", blocks=self._subagent_blocks(block, [], 0, False))
            block.message_ts = sent[0] if sent else None
        block.actions.append(line)
        await self._maybe_edit_subagent(task, block, False)

    async def _subagent_completed(self, task: Task, action: SubagentCompleted) -> None:
        block = task.subagent_blocks.get(action.handle)
        if block is None:
            return
        block.finished_at = time.time()
        if action.result_summary:
            block.actions.append(f"• ✅ {action.result_summary[:140]}")
        await self._maybe_edit_subagent(task, block, True, force=True)

    async def _maybe_edit_subagent(self, task: Task, block: SubagentBlock, finished: bool, *, force: bool = False) -> None:
        now = time.time()
        if block.message_ts is None or (not force and now - block.last_edit_at < SUBAGENT_EDIT_THROTTLE_SECS):
            return
        blocks = self._subagent_blocks(block, block.actions[-SUBAGENT_BLOCK_MAX_ACTIONS:], len(block.actions), finished)
        await self._require_bot().edit_message(task.channel_id, block.message_ts, text="", blocks=blocks)
        block.last_edit_at = now

    # -- interrogatives -----------------------------------------------------

    @staticmethod
    def _action_target(action: Any, task: Task) -> str:
        payload = getattr(action, "payload", None) or {}
        return str(payload.get("actor_id") or payload.get("target_actor_id") or task.owner_user_id)

    async def _post_interrogative(self, task: Task, action: Any) -> None:
        actor_id = self._action_target(action, task)
        iid = str(action.interrogative_id)
        if isinstance(action, Confirmation):
            payload = {"kind": "confirmation", "question": action.question}
            text = f"<@{actor_id}> ❓ {action.question}\nReply *yes* or *no*."
        elif isinstance(action, Clarification):
            payload = {"kind": "clarification", "question": action.question, "options": action.options}
            lines = [f"<@{actor_id}> ❓ {action.question}"] + [f"{i}. {opt.get('label') or opt.get('key')}" for i, opt in enumerate(action.options, 1)]
            lines.append("_Reply with a number, option text, or free text._")
            text = "\n".join(lines)
        else:
            payload = dict(action.payload)
            payload.setdefault("kind", "ask_user_question")
            lines = [f"<@{actor_id}> ❓ Polytoken is asking:"]
            for question in payload.get("questions") or []:
                lines.append(f"*{question.get('question', '')}*")
                for i, option in enumerate(question.get("options") or [], 1):
                    lines.append(f"{i}. {option.get('label', '')} — {option.get('description', '')}".rstrip(" —"))
            lines.append("_Reply with a number or free text._")
            text = "\n".join(lines)
        now = int(time.time())
        binding = await get_active_promotion(self._conn, task.key)
        binding_id = binding.binding_id if binding is not None else (task.binding_id or "")
        if actor_id != task.owner_user_id and not await self._is_participant(task, actor_id, is_app=False):
            return
        pending = StoredInterrogative(iid, ActorId(actor_id), payload, now + 86400, now)
        await put_pending_interrogative(self._conn, task.key, pending, binding_id=binding_id, target_kind=ParticipantKind.APP if actor_id == self.app_actor_id else ParticipantKind.HUMAN)
        self._pending[(task.key, actor_id)] = pending
        await self._post(task, text[:3900])

    async def _pending_for(self, task: Task, actor_id: str) -> StoredInterrogative | None:
        cached = self._pending.get((task.key, actor_id))
        if cached is not None:
            return cached
        pending = await get_pending_interrogative(self._conn, task.key, ActorId(actor_id))
        if pending is not None:
            self._pending[(task.key, actor_id)] = pending
        return pending

    async def _answer_interrogative(self, task: Task, pending: StoredInterrogative, text: str) -> None:
        kind = pending.payload.get("kind")
        if kind == "confirmation":
            response = {"kind": "confirmation_answer", "confirmed": text.lower() in {"y", "yes", "ok", "confirm", "true", "👍"}}
        elif kind == "clarification":
            options = pending.payload.get("options") or []
            response = self._clarification_response(options, text)
        else:
            response = self._ask_question_response(pending.payload, text)
        try:
            binding = await get_active_promotion(self._conn, task.key)
            binding_id = binding.binding_id if binding is not None else (task.binding_id or "")
            claimed = await consume_pending_interrogative(
                self._conn, task.key, pending.actor_id,
                interrogative_id=pending.interrogative_id, binding_id=binding_id,
            )
            if not claimed:
                await self._post(task, "⚠ This question is expired, already answered, or no longer bound to this task.")
                return
            await self._client_for(task).respond_interrogative(pending.interrogative_id, response)
            self._pending.pop((task.key, str(pending.actor_id)), None)
        except PolytokenClientError:
            await self._post(task, "⚠ Failed to deliver your answer to the daemon.")

    @staticmethod
    def _clarification_response(options: list[dict[str, Any]], text: str) -> dict[str, Any]:
        if text.isdigit() and 0 < int(text) <= len(options):
            return {"kind": "clarification_choice", "choice": options[int(text) - 1].get("key", "")}
        lowered = text.lower()
        for option in options:
            if lowered in {str(option.get("label", "")).lower(), str(option.get("key", "")).lower()}:
                return {"kind": "clarification_choice", "choice": option.get("key", "")}
        return {"kind": "clarification_text", "text": text}

    @staticmethod
    def _ask_question_response(payload: Mapping[str, Any], text: str) -> dict[str, Any]:
        answers = []
        for question in payload.get("questions") or []:
            options = question.get("options") or []
            reply: dict[str, Any] = {"question_id": question.get("id", "")}
            if text.isdigit() and 0 < int(text) <= len(options):
                reply["selected_option_ids"] = [options[int(text) - 1].get("id", "")]
            else:
                match = next((option for option in options if text.lower() == str(option.get("label", "")).lower()), None)
                if match:
                    reply["selected_option_ids"] = [match.get("id", "")]
                else:
                    reply["free_text"] = text
            answers.append(reply)
        return {"kind": "ask_user_question_answers", "answers": answers}

    # -- spawn and inbound routing -----------------------------------------

    async def spawn_task(
        self,
        cwd: str,
        *,
        team_id: str | None = None,
        channel_id: str | None = None,
        owner_user_id: str | None = None,
        mode: str = "personal",
        root_ts: str | None = None,
        prompt: str | None = None,
        participants: list[str] | None = None,
    ) -> Task:
        if not Path(cwd).is_dir():
            raise TaskSpawnError(f"cwd does not exist: {cwd}")
        bot = self._require_bot()
        team_id = str(team_id or getattr(bot, "team_id", ""))
        channel_id = str(channel_id or getattr(bot, "home_channel_id", ""))
        owner_user_id = str(owner_user_id or getattr(bot, "owner_user_id", ""))
        if not team_id or not channel_id or not owner_user_id:
            raise TaskSpawnError("team_id, channel_id, and owner_user_id are required")
        mode = str(mode).strip().lower()
        if mode not in {"personal", "collaborative"}:
            raise TaskSpawnError("mode must be personal or collaborative")
        task_id = str(uuid.uuid4())
        created = int(time.time())
        if root_ts is None:
            roots = await bot.create_task_root(channel_id, f"Starting `{Path(cwd).name}`…")
            root_ts = str(roots)
        channel_owned = bool(mode == "collaborative" and channel_id != self.home_channel_id and channel_id in getattr(bot, "_owned_channel_ids", set()))
        task = Task(task_id, team_id, channel_id, str(root_ts), owner_user_id, mode, cwd, "spawning", created_at=created, last_activity=created, app_exchange_budget=self.app_exchange_budget, channel_owned=channel_owned)
        await self.attach_task(task)
        try:
            result = await self._supervisor.spawn(cwd)
        except DaemonSupervisorError as exc:
            task.status = "crashed"
            await self._persist_root(task)
            task.cleanup_pending = not _cleanup_task_attachments(task_id)
            await update_runtime(self._conn, task.task_id, cleanup_pending=task.cleanup_pending, status="crashed")
            if task.channel_owned:
                with contextlib.suppress(Exception):
                    remember = getattr(bot, "remember_owned_channel", None)
                    if callable(remember):
                        remember(channel_id)
                    await bot.archive_channel(channel_id)
            with contextlib.suppress(Exception):
                await self._post(task, "💥 Task failed to spawn a Polytoken daemon.")
            raise TaskSpawnError("polytoken daemon failed to spawn") from exc
        task.status = "running"
        task.polytoken_session_id = result.session_id
        task.port = result.port
        task.credential_file_path = getattr(result, "credential_file_path", None)
        await update_runtime(
            self._conn,
            task.task_id,
            session_id=result.session_id,
            port=result.port,
            status="running",
            last_activity=task.last_activity,
        )
        await self._index(task)
        self._by_session_id[result.session_id] = task
        self._start_consumer(task)
        if participants and mode == "collaborative":
            await self._invite_then_persist(task, participants)
        if prompt:
            await self._prompt(task, prompt)
        return task

    async def write_initial_prompt(self, task_id: str, prompt: str, owner_user_id: str | SlackActor | None = None) -> None:
        task = self._require_task(task_id, owner_user_id)
        await self._prompt(task, prompt)

    async def _dedup_message(self, msg: SlackMessage) -> bool:
        identifiers: list[str] = []
        if msg.event_id:
            identifiers.append(f"event:{msg.event_id}")
        if msg.message_ts:
            identifiers.append(f"message:{msg.message_ts}")
        if not identifiers:
            return True
        for identifier in identifiers:
            marker = (msg.team_id, identifier)
            if marker in self._message_seen:
                return False
            if not await mark_event_seen(self._conn, TeamId(msg.team_id), EventId(identifier)):
                return False
            self._message_seen.add(marker)
        return True

    async def maybe_route_message(self, value: Any) -> bool:
        msg = normalize_message(value)
        if not msg.team_id or not msg.channel_id or not msg.root_ts or not msg.actor_id:
            return False
        if self.app_actor_id and msg.actor_id == str(self.app_actor_id):
            return False
        if self._bridge_bot_id and msg.actor_id == str(self._bridge_bot_id):
            return False
        task = self.get_by_key(ConversationKey(msg.team_id, msg.channel_id, msg.root_ts))
        if task is None or task.key in self._disabled_roots:
            return False
        if task.status in {"rebinding", "promoting"} or task.promotion_state in {"preparing"}:
            return False
        if task.status not in {"running", "spawning"}:
            return True
        if msg.actor.is_app and (
            (self.app_actor_id and msg.actor_id == str(self.app_actor_id))
            or (self._bridge_bot_id and msg.actor_id == str(self._bridge_bot_id))
        ):
            return False
        if not await self._authorized_message(task, msg.actor):
            return False
        if not await self._dedup_message(msg):
            return True
        pending = await self._pending_for(task, msg.actor_id)
        paths = await self._save_attachments(task, msg)
        voice_paths = [path for path in paths if voice.is_audio_path(path)]
        other_paths = [path for path in paths if path not in voice_paths]
        voice_parts: list[str] = []
        for path in voice_paths:
            transcript = await voice.transcribe(path)
            voice_parts.append(f"[voice memo] {transcript}" if transcript else f"[voice memo received: {path}]")
        raw_text = msg.text.rstrip()
        # Attachments and voice are normal prompts, never answers to a pending
        # actor-targeted interrogative.
        if pending is not None and raw_text and not msg.files and not voice_parts and not other_paths:
            await self._answer_interrogative(task, pending, raw_text)
            return True
        body_parts = [raw_text] if raw_text else []
        body_parts.extend(voice_parts)
        body_parts.extend(f"@{path}" for path in other_paths)
        if not body_parts:
            return True
        if msg.actor.is_app:
            if task.mode != "collaborative" or not await self._is_participant(task, msg.actor_id, is_app=True):
                return False
            # Admission and accepted-count update share the per-task lock so
            # concurrent app events cannot both pass the same remaining slot.
            lock = self._task_lock(task)
            async with lock:
                if task.app_exchanges >= task.app_exchange_budget:
                    task.status = "paused"
                    await self._persist_root(task)
                    if not task.owner_alerted:
                        task.owner_alerted = True
                        await self._post(task, f"<@{task.owner_user_id}> ⏸️ App exchange budget exhausted; task paused.")
                    return True
                accepted = await self._prompt(task, " ".join(body_parts), provenance=MessageProvenance(
                    msg.team_id, msg.channel_id, msg.root_ts, msg.message_ts, msg.event_id,
                    msg.actor_id, "app",
                ), lock_held=True)
                if accepted:
                    task.app_exchanges += 1
                    await update_runtime(self._conn, task.task_id, app_exchanges=task.app_exchanges)
                return accepted
        provenance = MessageProvenance(
            msg.team_id, msg.channel_id, msg.root_ts, msg.message_ts, msg.event_id,
            msg.actor_id, "app" if msg.actor.is_app else "human",
        )
        return await self._prompt(task, " ".join(body_parts), provenance=provenance)

    async def _authorized_message(self, task: Task, actor: SlackActor) -> bool:
        if actor.actor_id == task.owner_user_id:
            return not actor.is_app
        if task.mode == "personal":
            return False
        return await self._is_participant(task, actor.actor_id, is_app=actor.is_app)

    async def _is_participant(self, task: Task, actor_id: str, *, is_app: bool = False) -> bool:
        if actor_id == task.owner_user_id:
            return not is_app
        rows = await list_participants(self._conn, task.key)
        expected = ParticipantKind.APP if is_app else ParticipantKind.HUMAN
        return any(str(row.participant.actor_id) == actor_id and row.participant.kind is expected for row in rows)

    async def _prompt(
        self, task: Task, content: str, *, provenance: MessageProvenance | None = None,
        lock_held: bool = False,
    ) -> bool:
        if task.status not in {"running", "spawning"}:
            return False
        lock = self._task_lock(task)
        if lock.locked() and not lock_held:
            return False
        envelope = PromptEnvelope(provenance or MessageProvenance(task.team_id, task.channel_id, task.root_ts, None, None, task.owner_user_id), content)
        task.last_envelope = envelope
        try:
            wire_body = envelope.provenance.wire(envelope.body)
            await self._client_for(task).prompt(wire_body)
            task.last_activity = int(time.time())
            await update_runtime(self._conn, task.task_id, last_activity=task.last_activity)
            return True
        except TurnInFlight:
            await self._post(task, "⏳ A turn is already running; wait for it to finish or close the task.")
        except PolytokenClientError as exc:
            await self._post(task, f"⚠ Couldn't deliver the message to the daemon: {safe_error(exc, 'delivery failed')}.")
        return False

    async def _save_attachments(self, task: Task, msg: SlackMessage) -> list[Path]:
        if not msg.files:
            return []
        directory = ATTACHMENTS_DIR / task.task_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            directory.chmod(0o700)
        saved: list[Path] = []
        aggregate = 0
        for index, attachment in enumerate(msg.files[:MAX_ATTACHMENTS_PER_MESSAGE]):
            declared = int(attachment.size or 0)
            if aggregate + declared > MAX_ATTACHMENT_AGGREGATE_BYTES:
                break
            if not attachment.url or (attachment.size is not None and int(attachment.size) > MAX_ATTACHMENT_BYTES):
                continue
            name = Path(attachment.filename).name or f"attachment-{index}"
            destination = directory / f"{msg.message_ts or int(time.time() * 1000)}-{index}-{name}"
            try:
                await self._require_bot().download_file(attachment.url, destination, MAX_ATTACHMENT_BYTES)
                size = destination.stat().st_size
                if size > MAX_ATTACHMENT_BYTES or aggregate + size > MAX_ATTACHMENT_AGGREGATE_BYTES:
                    destination.unlink(missing_ok=True)
                    continue
                aggregate += size
                saved.append(destination)
            except Exception:
                log.warning("failed to download Slack attachment")
        return saved

    # -- participants / promotion -----------------------------------------

    async def _invite_then_persist(self, task: Task, actor_ids: list[str]) -> None:
        actor_ids = list(dict.fromkeys(str(actor) for actor in actor_ids if str(actor) != task.owner_user_id))
        if not actor_ids:
            return
        await self._require_bot().invite_participants(task.channel_id, actor_ids)
        for actor_id in actor_ids:
            kind = ParticipantKind.APP if actor_id.startswith("B") else ParticipantKind.HUMAN
            await upsert_participant(self._conn, task.key, Participant(ActorId(actor_id), kind))

    async def add_participant(
        self, task_id: str, owner_user_id: str | SlackActor, actor_id: str,
        display_name: str | None = None, *, kind: str | ParticipantKind = ParticipantKind.HUMAN,
    ) -> None:
        task = self._require_task(task_id, owner_user_id)
        actor_id = str(actor_id).strip()
        try:
            participant_kind = kind if isinstance(kind, ParticipantKind) else ParticipantKind(str(kind).strip().lower())
        except ValueError as exc:
            raise ValueError("participant kind must be human or app") from exc
        if not actor_id:
            raise ValueError("participant actor_id must not be empty")
        if participant_kind is ParticipantKind.APP and not actor_id.startswith("B"):
            raise ValueError("app participant actor_id must be Slack bot_id (B...)")
        await self._require_bot().invite_participants(task.channel_id, [actor_id])
        # Persist the stable B... app identity; Bot resolves it to U... only
        # for the Slack membership side effect above.
        await upsert_participant(self._conn, task.key, Participant(ActorId(actor_id), participant_kind, display_name))

    async def remove_participant(self, task_id: str, owner_user_id: str | SlackActor, actor_id: str) -> bool:
        task = self._require_task(task_id, owner_user_id)
        if str(actor_id) == task.owner_user_id:
            raise TaskPrivilegeError("the owner cannot be removed")
        remover = getattr(self._require_bot(), "remove_participants", None)
        if remover is None:
            raise TaskRoutingError("Bot.remove_participants is required before removing a Slack participant")
        await remover(task.channel_id, [str(actor_id)])
        return await delete_participant(self._conn, task.key, ActorId(actor_id))

    async def promote_task(self, task_id: str, owner_user_id: str | SlackActor, participant_actor_ids: list[str] | None = None, *, name: str | None = None) -> Task:
        task = self._require_task(task_id, owner_user_id)
        if task.mode == "collaborative":
            return task
        lock = self._task_lock(task)
        if lock.locked():
            raise TaskRoutingError("task promotion is already in progress")
        async with lock:
            return await self._promote_task_locked(task, participant_actor_ids, name=name)

    async def _promote_task_locked(self, task: Task, participant_actor_ids: list[str] | None = None, *, name: str | None = None) -> Task:
        old_key = task.key
        old_channel, old_root, old_mode = task.channel_id, task.root_ts, task.mode
        old_channel_owned = task.channel_owned
        participant_rows = await list_participants(self._conn, old_key)
        ids = participant_actor_ids
        if ids is None:
            # The default promotion path is a root move, not a participant
            # re-import. Preserve the complete stored records, including kind
            # and display name, while still using their stable actor IDs for
            # Slack membership invites.
            ids = [str(row.participant.actor_id) for row in participant_rows]
        else:
            ids = [str(actor_id).strip() for actor_id in ids if str(actor_id).strip()]
        bot = self._require_bot()
        new_channel: str | None = None
        new_root: str | None = None
        journal_id = f"{task.task_id}:{uuid.uuid4().hex}"
        task.status = "rebinding"
        task.promotion_state = "preparing"
        # The journal and transient runtime marker are one durable boundary.
        # Do not use the helpers here: each helper commits independently.
        stamp = int(time.time())
        await self._conn.execute("BEGIN")
        try:
            await self._conn.execute(
                """INSERT INTO promotion_journal
                   (journal_id, task_id, team_id, old_channel_id, old_root_id,
                    old_mode, old_binding_id, state, side_effect, side_effect_state,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'preparing', 'create_channel',
                           'pending', ?, ?)""",
                (journal_id, task.task_id, old_key.team_id, old_key.channel_id,
                 old_key.root_id, old_mode, task.binding_id, stamp, stamp),
            )
            # The journal is the durable in-flight marker.  Do not persist the
            # transient in-memory ``rebinding`` status: after a crash, daemon
            # reconciliation must decide whether the old runtime is live.
            await self._conn.commit()
        except BaseException:
            await self._conn.rollback()
            raise
        try:
            await update_promotion_journal(self._conn, journal_id, state="preparing", side_effect="create_channel", side_effect_state="started")
            new_channel = str(await bot.create_private_channel(name or f"task-{task.task_id[:8]}"))
            task.channel_owned = True
            await update_promotion_journal(self._conn, journal_id, new_channel_id=new_channel, state="rebinding", side_effect="invite", side_effect_state="pending")

            await update_promotion_journal(self._conn, journal_id, side_effect="invite", side_effect_state="started")
            invite_ids = list(dict.fromkeys(str(i) for i in ids if str(i) != task.owner_user_id))
            await bot.invite_participants(new_channel, invite_ids)
            await update_promotion_journal(self._conn, journal_id, side_effect="create_root", side_effect_state="pending")

            await update_promotion_journal(self._conn, journal_id, side_effect="create_root", side_effect_state="started")
            new_root = str(await bot.create_task_root(new_channel, f"Collaborative task `{task.task_id[:8]}`"))
            binding_id = f"{task.task_id}:{int(time.time() * 1000)}"
            await update_promotion_journal(self._conn, journal_id, new_root_id=new_root, new_binding_id=binding_id, state="cleanup_pending", side_effect="rebind_db", side_effect_state="pending")
            new_key = ConversationKey(task.team_id, new_channel, new_root)
            task.channel_id, task.root_ts, task.mode = new_channel, new_root, "collaborative"
            task.status = "running"
            task.promotion_state = "active"
            task.binding_id = binding_id
            if participant_actor_ids is None:
                promoted_participants = [
                    row.participant for row in participant_rows
                    if str(row.participant.actor_id) != task.owner_user_id
                ]
            else:
                promoted_participants = [
                    Participant(
                        ActorId(actor_id),
                        ParticipantKind.APP if actor_id.startswith("B") else ParticipantKind.HUMAN,
                    )
                    for actor_id in ids if actor_id != task.owner_user_id
                ]
            await replace_runtime_binding(
                self._conn, old_key, self._runtime(task), promoted_participants,
                binding=(binding_id, new_channel, "slack_private_channel", ActorId(task.owner_user_id)),
            )
            await update_promotion_journal(self._conn, journal_id, state="active", side_effect="rebind_db", side_effect_state="complete")
            self._by_key.pop(old_key, None)
            self._disabled_roots.add(old_key)
            await self._index(task)
            return task
        except Exception as exc:
            task.channel_id, task.root_ts, task.mode = old_channel, old_root, old_mode
            task.channel_owned = old_channel_owned
            task.status = "running"
            task.promotion_state = "failed"
            if journal_id:
                with contextlib.suppress(Exception):
                    await update_promotion_journal(self._conn, journal_id, state="cleanup_pending" if new_channel else "failed", side_effect_state="failed", error_code=type(exc).__name__)
            with contextlib.suppress(Exception):
                await self._persist_task(task)
            if new_channel is not None:
                task.channel_owned = True
                try:
                    await update_promotion_journal(self._conn, journal_id, state="cleanup_pending", side_effect="archive_channel", side_effect_state="started")
                    await bot.archive_channel(new_channel)
                    await update_promotion_journal(self._conn, journal_id, state="failed", side_effect_state="complete")
                    task.cleanup_pending = False
                except Exception:
                    task.cleanup_pending = True
                    with contextlib.suppress(Exception):
                        await update_promotion_journal(self._conn, journal_id, state="cleanup_pending", side_effect="archive_channel", side_effect_state="failed", error_code="archive_failed")
            if new_root is not None:
                with contextlib.suppress(Exception):
                    await delete_root(self._conn, ConversationKey(task.team_id, new_channel, new_root))
            await self._persist_task(task)
            raise TaskRoutingError("promotion failed; old root remains active") from exc

    promote = promote_task

    async def close_task(self, task_id: str, owner_user_id: str | SlackActor) -> bool:
        task = self._require_task(task_id, owner_user_id)
        return await self.stop_task(task_id, owner_user_id)

    # -- pins ---------------------------------------------------------------

    async def pin_channel(self, key: ConversationKey, text: str, owner_user_id: str | SlackActor, *, pin_id: str | None = None) -> TextPin:
        if not isinstance(key, ConversationKey):
            raise TypeError("pin_channel requires a ConversationKey")
        task = self.get_by_key(key)
        if task is not None:
            self._require_owner(task, owner_user_id)
        pin = TextPin(pin_id or str(uuid.uuid4()), key, text, ActorId(_actor(owner_user_id).actor_id), int(time.time()), int(time.time()))
        await upsert_text_pin(self._conn, pin)
        with contextlib.suppress(Exception):
            await self._require_bot().add_reaction(key.channel_id, key.root_id, "pushpin")
        return pin

    async def unpin_channel(self, key: ConversationKey, owner_user_id: str | SlackActor, pin_id: str) -> bool:
        task = self.get_by_key(key)
        if task is not None:
            self._require_owner(task, owner_user_id)
        return await delete_text_pin(self._conn, key, pin_id)

    async def get_pin_for(self, key: ConversationKey, owner_user_id: str | SlackActor | None = None, pin_id: str | None = None) -> TextPin | None:
        task = self.get_by_key(key)
        if task is not None and owner_user_id is not None:
            self._require_owner(task, owner_user_id)
        return await get_text_pin(self._conn, key, pin_id)

    async def list_all_pins(self, key: ConversationKey, owner_user_id: str | SlackActor | None = None) -> list[TextPin]:
        task = self.get_by_key(key)
        if task is not None and owner_user_id is not None:
            self._require_owner(task, owner_user_id)
        return await list_text_pins(self._conn, key)

    # -- config/session APIs ------------------------------------------------

    async def invoke_skill(self, task_id: str, skill_name: str, args: str | None = None, *, owner_user_id: str | SlackActor) -> None:
        task = self._require_task(task_id, owner_user_id)
        content = f"@{skill_name}" + (f" {args}" if args else "")
        await self._prompt(task, content)

    async def set_effort(self, task_id: str, level: str, *, owner_user_id: str | SlackActor) -> None:
        task = self._require_task(task_id, owner_user_id)
        state = await self._state_snapshot(task)
        model = state.get("active_model") if state else None
        if not model:
            raise TaskSpawnError("active model unknown; cannot set effort")
        try:
            await self._client_for(task).set_model(model, reasoning_effort=level)
        except PolytokenClientError as exc:
            raise TaskSpawnError(safe_error(exc, "daemon rejected effort change")) from exc

    async def set_model(self, task_id: str, model: str, *, owner_user_id: str | SlackActor, reasoning_effort: str | None = None) -> None:
        task = self._require_task(task_id, owner_user_id)
        try:
            await self._client_for(task).set_model(model, reasoning_effort=reasoning_effort)
        except PolytokenClientError as exc:
            raise TaskSpawnError(safe_error(exc, "daemon rejected model change")) from exc

    async def set_facet(self, task_id: str, facet: str, *, owner_user_id: str | SlackActor) -> None:
        task = self._require_task(task_id, owner_user_id)
        try:
            await self._client_for(task).set_facet(facet)
        except PolytokenClientError as exc:
            raise TaskSpawnError(safe_error(exc, "daemon rejected facet change")) from exc

    async def get_state(self, task_id: str, owner_user_id: str | SlackActor) -> dict | None:
        task = self._require_task(task_id, owner_user_id)
        return await self._state_snapshot(task)

    async def _state_snapshot(self, task: Task) -> dict:
        try:
            return await self._client_for(task).state()
        except (PolytokenClientError, TaskSpawnError):
            return {}

    async def generate_root_title(self, task_id: str, *, owner_user_id: str | SlackActor) -> str | None:
        task = self._require_task(task_id, owner_user_id)
        state = await self._state_snapshot(task)
        title = str(state.get("session_title") or "").strip()
        return title or None

    async def list_models(self, owner_user_id: str | SlackActor | None = None) -> list[str]:
        if owner_user_id is not None and self._by_task_id:
            if not any(task.owner_user_id == _actor(owner_user_id).actor_id for task in self._by_task_id.values()):
                raise TaskPrivilegeError("actor is not an owner")
        try:
            return await self._supervisor.list_models()
        except DaemonSupervisorError:
            return []

    # -- lifecycle ----------------------------------------------------------

    async def list_tasks(self, owner_user_id: str | SlackActor | None = None) -> list[Task]:
        tasks = [task for task in self._by_task_id.values() if task.status in {"spawning", "running", "paused"}]
        if owner_user_id is not None:
            actor_id = _actor(owner_user_id).actor_id
            tasks = [task for task in tasks if task.owner_user_id == actor_id]
        return sorted(tasks, key=lambda task: task.last_activity, reverse=True)

    async def stop_task(self, task_id: str, owner_user_id: str | SlackActor, *, timeout: float = 5.0) -> bool:
        task = self._require_task(task_id, owner_user_id)
        if task.status not in {"running", "spawning", "paused"}:
            return True
        with contextlib.suppress(PolytokenClientError, TaskSpawnError):
            await self._client_for(task).cancel_turn()
        try:
            await self._client_for(task).terminate()
        except PolytokenClientError as exc:
            if exc.status is not None:
                await self._post(task, "⚠ The daemon rejected termination; task remains active.")
                return False
        await self._teardown_task(task, status="stopped")
        return True

    async def kill_task(self, task_id: str, owner_user_id: str | SlackActor) -> bool:
        task = self._require_task(task_id, owner_user_id)
        if task.status not in {"running", "spawning", "paused"}:
            return True
        try:
            await self._client_for(task).terminate()
        except PolytokenClientError as exc:
            if exc.status is not None:
                return False
        await self._teardown_task(task, status="crashed")
        return True

    async def restart_task(self, task_id: str, owner_user_id: str | SlackActor) -> Task:
        self._require_task(task_id, owner_user_id)
        raise TaskRestartError("restart is unsupported; close this task and start a new one")

    async def resume_task(self, task_id: str, owner_user_id: str | SlackActor) -> Task:
        task = self._require_task(task_id, owner_user_id)
        if task.status == "paused":
            task.status = "running"
            task.owner_alerted = False
            await self._persist_root(task)
        return task

    async def _end_turn(self, task: Task) -> None:
        # App budget is a turn/exchange budget, not a process-lifetime budget.
        if task.app_exchanges:
            task.app_exchanges = 0
            task.owner_alerted = False
            if task.status == "paused":
                task.status = "running"
            await update_runtime(self._conn, task.task_id, app_exchanges=0, owner_alerted=False, status=task.status)
        aggregator = self._aggregators.get(task.task_id)
        if aggregator is not None:
            await aggregator.flush_now()

    def _agg_for(self, task: Task) -> _ToolSummaryAggregator:
        aggregator = self._aggregators.get(task.task_id)
        if aggregator is None:
            aggregator = _ToolSummaryAggregator(self._require_bot(), task)
            self._aggregators[task.task_id] = aggregator
        return aggregator

    async def _teardown_task(self, task: Task, *, status: str, cancel_consumer: bool = True) -> None:
        if task.task_id in self._torn_down:
            return
        self._torn_down.add(task.task_id)
        task.status = status
        task.last_activity = int(time.time())
        await self._persist_root(task)
        consumer = self._consumers.pop(task.task_id, None)
        if cancel_consumer and consumer is not None and not consumer.done():
            consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await consumer
        self._translators.pop(task.task_id, None)
        aggregator = self._aggregators.pop(task.task_id, None)
        if aggregator is not None:
            await aggregator.flush_now()
        client = self._clients.pop(task.task_id, None)
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()
        self._by_key.pop(task.key, None)
        if task.polytoken_session_id:
            self._by_session_id.pop(task.polytoken_session_id, None)
        # Never archive the operator's home/personal conversation.  A
        # collaborative private channel is disposable and may be archived.
        if task.channel_owned:
            with contextlib.suppress(Exception):
                bot = self._require_bot()
                remember = getattr(bot, "remember_owned_channel", None)
                if callable(remember):
                    remember(task.channel_id)
                await bot.archive_channel(task.channel_id)
        task.cleanup_pending = not _cleanup_task_attachments(task.task_id)
        await update_runtime(self._conn, task.task_id, cleanup_pending=task.cleanup_pending, status=status, last_activity=task.last_activity)


__all__ = [
    "Actor", "File", "Message", "MessageProvenance", "PromptEnvelope", "SlackActor", "SlackFile", "SlackMessage",
    "Task", "TaskNotFound", "TaskPrivilegeError", "TaskRegistry", "TaskRestartError", "TaskRoutingError", "TaskSpawnError",
    "ATTACHMENTS_DIR", "BRIDGE_STATE_DIR", "MAX_ATTACHMENT_BYTES", "MAX_ATTACHMENTS_PER_MESSAGE", "MAX_ATTACHMENT_AGGREGATE_BYTES", "PROVENANCE_VERSION", "normalize_message", "sweep_old_attachments",
]

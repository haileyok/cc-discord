"""Async SQLite persistence for normalized Slack roots and bridge state.

All provider identifiers are opaque ``TEXT`` values.  A one-time migration
removes tables from the legacy runtime before creating the normalized Slack
schema.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import aiosqlite

from bridge.domain import (
    ActorId,
    ChannelId,
    ConversationKey,
    EventId,
    Mode,
    Owner,
    Participant,
    ParticipantKind,
    PendingInterrogative,
    PromotionBinding,
    RootId,
    TeamId,
    TextPin,
)

DEFAULT_STATE_DIR = Path(os.environ.get(
    "BRIDGE_STATE_DIR",
    str(Path.home() / ".local" / "state" / "claude-slack-bridge"),
)).expanduser()
DEFAULT_DB_PATH = DEFAULT_STATE_DIR / "state.db"
DEDUP_MAX_RECORDS = 10_000
_LEGACY_RESET_MARKER = "legacy_runtime_reset_v1"

RUNTIME_ACTIVE_STATUSES = ("spawning", "running", "paused", "rebinding")
PROMOTION_STATES = ("none", "preparing", "active", "failed")
PROMOTION_JOURNAL_STATES = ("preparing", "rebinding", "cleanup_pending", "active", "failed")


@dataclass(frozen=True, slots=True)
class RuntimeRow:
    task_id: str
    key: ConversationKey
    session_id: str | None
    port: int | None
    status: str
    cwd: str
    owner: Owner
    created_at: int
    last_activity: int
    app_exchange_budget: int
    app_exchanges: int
    owner_alerted: bool
    promotion_state: str = "none"
    binding_id: str | None = None
    cleanup_pending: bool = False
    channel_owned: bool = False


@dataclass(frozen=True, slots=True)
class PromotionJournalRow:
    journal_id: str
    task_id: str
    old_key: ConversationKey
    old_mode: str
    old_binding_id: str | None
    new_channel_id: str | None
    new_root_id: str | None
    new_binding_id: str | None
    state: str
    side_effect: str
    side_effect_state: str
    error_code: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class RuntimeParticipant:
    participant: Participant
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class RootRow:
    team_id: TeamId
    channel_id: ChannelId
    root_id: RootId
    owner: Owner
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class ParticipantRow:
    key: ConversationKey
    participant: Participant
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class DedupRow:
    team_id: TeamId
    event_id: EventId
    seen_at: int


# New normalized schema.  Foreign keys are intentionally not used: providers
# can deliver participants/events before the root envelope and those records
# should remain durable until the root is materialized.
_NORMALIZED_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS task_runtime (
        task_id TEXT PRIMARY KEY,
        team_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        root_id TEXT NOT NULL,
        session_id TEXT,
        port INTEGER,
        status TEXT NOT NULL,
        cwd TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        owner_kind TEXT NOT NULL CHECK(owner_kind IN ('human', 'app')),
        mode TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        last_activity INTEGER NOT NULL,
        app_exchange_budget INTEGER NOT NULL CHECK(app_exchange_budget > 0),
        app_exchanges INTEGER NOT NULL DEFAULT 0 CHECK(app_exchanges >= 0),
        owner_alerted INTEGER NOT NULL DEFAULT 0 CHECK(owner_alerted IN (0, 1)),
        promotion_state TEXT NOT NULL DEFAULT 'none' CHECK(promotion_state IN ('none', 'preparing', 'active', 'failed', 'cleanup_pending')),
        binding_id TEXT,
        cleanup_pending INTEGER NOT NULL DEFAULT 0 CHECK(cleanup_pending IN (0, 1)),
        channel_owned INTEGER NOT NULL DEFAULT 0 CHECK(channel_owned IN (0, 1)),
        updated_at INTEGER NOT NULL,
        UNIQUE(team_id, channel_id, root_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_task_runtime_session ON task_runtime(session_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_task_runtime_active_binding
    ON task_runtime(team_id, channel_id, root_id, binding_id)
    WHERE status IN ('spawning', 'running', 'paused', 'rebinding')
    """,
    """
    CREATE TABLE IF NOT EXISTS roots (
        team_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        root_id TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        owner_kind TEXT NOT NULL CHECK(owner_kind IN ('human', 'app')),
        mode TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (team_id, channel_id, root_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS participants (
        team_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        root_id TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('human', 'app')),
        display_name TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (team_id, channel_id, root_id, actor_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS promotion_bindings (
        binding_id TEXT PRIMARY KEY,
        team_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        root_id TEXT NOT NULL,
        target_id TEXT NOT NULL,
        target_kind TEXT NOT NULL,
        promoted_by TEXT,
        created_at INTEGER NOT NULL,
        ended_at INTEGER,
        active INTEGER NOT NULL CHECK(active IN (0, 1))
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_promotion
    ON promotion_bindings(team_id, channel_id, root_id) WHERE active = 1
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_promotion_root_history
    ON promotion_bindings(team_id, channel_id, root_id, created_at, binding_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS promotion_journal (
        journal_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        team_id TEXT NOT NULL,
        old_channel_id TEXT NOT NULL,
        old_root_id TEXT NOT NULL,
        old_mode TEXT NOT NULL,
        old_binding_id TEXT,
        new_channel_id TEXT,
        new_root_id TEXT,
        new_binding_id TEXT,
        state TEXT NOT NULL CHECK(state IN ('preparing', 'rebinding', 'cleanup_pending', 'active', 'failed')),
        side_effect TEXT NOT NULL,
        side_effect_state TEXT NOT NULL CHECK(side_effect_state IN ('pending', 'started', 'complete', 'failed')),
        error_code TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_promotion_journal_pending
    ON promotion_journal(state, side_effect_state, updated_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS dedup_records (
        team_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        seen_at INTEGER NOT NULL,
        PRIMARY KEY (team_id, event_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pending_interrogatives (
        team_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        root_id TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        interrogative_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        binding_id TEXT NOT NULL DEFAULT '',
        target_kind TEXT NOT NULL DEFAULT 'human' CHECK(target_kind IN ('human', 'app')),
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        PRIMARY KEY (team_id, channel_id, root_id, actor_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_pending_expiry
    ON pending_interrogatives(expires_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS text_pins (
        pin_id TEXT PRIMARY KEY,
        team_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        root_id TEXT NOT NULL,
        actor_id TEXT,
        text TEXT NOT NULL CHECK(length(trim(text)) > 0),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_text_pins_root
    ON text_pins(team_id, channel_id, root_id, updated_at DESC, pin_id)
    """,
)


def _now(value: int | None) -> int:
    return int(time.time()) if value is None else int(value)


def _key(key: ConversationKey) -> ConversationKey:
    if not isinstance(key, ConversationKey):
        raise TypeError("key must be a ConversationKey")
    return key


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _decode_json(value: str) -> dict[str, Any]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("pending interrogative payload must be a JSON object")
    return decoded


async def _migrate_legacy_state(conn: aiosqlite.Connection) -> None:
    """Drop tables owned by the legacy runtime exactly once.

    The marker prevents repeating the destructive migration, while the table
    names remain limited to the old runtime's state so normalized Slack data
    is never touched.
    """
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    cursor = await conn.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (_LEGACY_RESET_MARKER,)
    )
    if await cursor.fetchone() is not None:
        return
    for table in ("approval_log", "pins", "tasks", "sessions"):
        await conn.execute(f"DROP TABLE IF EXISTS {table}")
    await conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
        (_LEGACY_RESET_MARKER, "completed"),
    )


async def _migrate_legacy_tasks(conn: aiosqlite.Connection) -> None:
    """Drop pre-daemon task tables when present.

    This remains a defensive migration for databases that predate the marker.
    """
    cursor = await conn.execute("PRAGMA table_info(tasks)")
    cols = {row[1] for row in await cursor.fetchall()}
    legacy = {"zellij_pane_id", "current_claude_session_id", "current_transcript_path"}
    if cols & legacy:
        await conn.execute("DROP TABLE IF EXISTS tasks")
    await conn.execute("DROP TABLE IF EXISTS approval_log")


async def init_schema(conn: aiosqlite.Connection) -> None:
    """Initialize or migrate the normalized Slack persistence schema."""
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await _migrate_legacy_state(conn)
    await _migrate_legacy_tasks(conn)
    for statement in _NORMALIZED_SCHEMA:
        await conn.execute(statement)
    # Add policy columns to databases created by the first normalized schema.
    cursor = await conn.execute("PRAGMA table_info(pending_interrogatives)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "binding_id" not in columns:
        await conn.execute("ALTER TABLE pending_interrogatives ADD COLUMN binding_id TEXT NOT NULL DEFAULT ''")
    if "target_kind" not in columns:
        await conn.execute("ALTER TABLE pending_interrogatives ADD COLUMN target_kind TEXT NOT NULL DEFAULT 'human'")
    runtime_columns = {row[1] for row in await (await conn.execute("PRAGMA table_info(task_runtime)")).fetchall()}
    if "channel_owned" not in runtime_columns:
        await conn.execute("ALTER TABLE task_runtime ADD COLUMN channel_owned INTEGER NOT NULL DEFAULT 0 CHECK(channel_owned IN (0, 1))")
    await conn.commit()


async def open_db(path: Path = DEFAULT_DB_PATH) -> aiosqlite.Connection:
    ensure_state_dir(path.parent)
    path = path.expanduser()
    if path.exists():
        path.chmod(0o600)
    else:
        # aiosqlite/sqlite do not expose an open mode; create the file with
        # operator-only permissions before opening it to avoid a permissive
        # creation window under a hostile umask.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    conn = await aiosqlite.connect(path)
    await init_schema(conn)
    # SQLite creates WAL/SHM lazily. Tighten all existing and future sidecars
    # after schema setup; the directory is 0700 as a second containment layer.
    for sidecar in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        if sidecar.exists():
            with contextlib.suppress(OSError):
                sidecar.chmod(0o600)
    return conn


async def close_db(conn: aiosqlite.Connection) -> None:
    await conn.close()


def _ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def ensure_state_dir(path: Path = DEFAULT_STATE_DIR) -> Path:
    """Create the bridge state directory with operator-only permissions."""
    return _ensure_private_dir(path.expanduser())


async def _root_sql(
    conn: aiosqlite.Connection, key: ConversationKey, owner: Owner, stamp: int
) -> None:
    await conn.execute(
        """
        INSERT INTO roots
          (team_id, channel_id, root_id, owner_id, owner_kind, mode, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(team_id, channel_id, root_id) DO UPDATE SET
          owner_id=excluded.owner_id, owner_kind=excluded.owner_kind,
          mode=excluded.mode, updated_at=excluded.updated_at
        """,
        (key.team_id, key.channel_id, key.root_id, owner.actor_id, owner.kind.value,
         owner.mode, stamp, stamp),
    )


async def _participant_sql(
    conn: aiosqlite.Connection, key: ConversationKey, participant: Participant, stamp: int
) -> None:
    await conn.execute(
        """
        INSERT INTO participants
          (team_id, channel_id, root_id, actor_id, kind, display_name, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(team_id, channel_id, root_id, actor_id) DO UPDATE SET
          kind=excluded.kind, display_name=excluded.display_name, updated_at=excluded.updated_at
        """,
        (key.team_id, key.channel_id, key.root_id, participant.actor_id,
         participant.kind.value, participant.display_name, stamp, stamp),
    )


async def upsert_runtime(
    conn: aiosqlite.Connection,
    runtime: RuntimeRow,
    *,
    participants: list[Participant] | None = None,
    now: int | None = None,
) -> RuntimeRow:
    """Atomically persist a runtime row, its root, and participants.

    This is the transaction boundary used by task rehydration and normal task
    binding. Callers that need to combine this with promotion history should use
    :func:`replace_runtime_binding` instead.
    """
    if not isinstance(runtime, RuntimeRow):
        raise TypeError("runtime must be a RuntimeRow")
    stamp = _now(now)
    await conn.execute("BEGIN")
    try:
        await _root_sql(conn, runtime.key, runtime.owner, stamp)
        await conn.execute(
            """
            INSERT INTO task_runtime
              (task_id, team_id, channel_id, root_id, session_id, port, status, cwd,
               owner_id, owner_kind, mode, created_at, last_activity,
               app_exchange_budget, app_exchanges, owner_alerted, promotion_state,
               binding_id, cleanup_pending, channel_owned, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
              team_id=excluded.team_id, channel_id=excluded.channel_id, root_id=excluded.root_id,
              session_id=excluded.session_id, port=excluded.port, status=excluded.status,
              cwd=excluded.cwd, owner_id=excluded.owner_id, owner_kind=excluded.owner_kind,
              mode=excluded.mode, last_activity=excluded.last_activity,
              app_exchange_budget=excluded.app_exchange_budget, app_exchanges=excluded.app_exchanges,
              owner_alerted=excluded.owner_alerted, promotion_state=excluded.promotion_state,
              binding_id=excluded.binding_id, cleanup_pending=excluded.cleanup_pending,
              channel_owned=excluded.channel_owned, updated_at=excluded.updated_at
            """,
            (runtime.task_id, runtime.key.team_id, runtime.key.channel_id, runtime.key.root_id,
             runtime.session_id, runtime.port, runtime.status, runtime.cwd,
             runtime.owner.actor_id, runtime.owner.kind.value, runtime.owner.mode,
             runtime.created_at, runtime.last_activity, runtime.app_exchange_budget,
             runtime.app_exchanges, int(runtime.owner_alerted), runtime.promotion_state,
             runtime.binding_id, int(runtime.cleanup_pending), int(runtime.channel_owned), stamp),
        )
        for participant in participants or []:
            await _participant_sql(conn, runtime.key, participant, stamp)
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise
    return runtime


def _runtime_from_row(row: tuple[Any, ...]) -> RuntimeRow:
    return RuntimeRow(
        task_id=str(row[0]),
        key=ConversationKey(TeamId(row[1]), ChannelId(row[2]), RootId(row[3])),
        session_id=row[4], port=int(row[5]) if row[5] is not None else None,
        status=str(row[6]), cwd=str(row[7]),
        owner=Owner(ActorId(row[8]), ParticipantKind(row[9]), Mode(row[10])),
        created_at=int(row[11]), last_activity=int(row[12]),
        app_exchange_budget=int(row[13]), app_exchanges=int(row[14]),
        owner_alerted=bool(row[15]), promotion_state=str(row[16]), binding_id=row[17],
        cleanup_pending=bool(row[18]), channel_owned=bool(row[19]),
    )


_RUNTIME_SELECT = """SELECT task_id, team_id, channel_id, root_id, session_id, port,
 status, cwd, owner_id, owner_kind, mode, created_at, last_activity,
 app_exchange_budget, app_exchanges, owner_alerted, promotion_state, binding_id,
 cleanup_pending, channel_owned FROM task_runtime"""


async def get_runtime(conn: aiosqlite.Connection, task_id: str) -> RuntimeRow | None:
    cursor = await conn.execute(_RUNTIME_SELECT + " WHERE task_id = ?", (str(task_id),))
    row = await cursor.fetchone()
    return _runtime_from_row(row) if row is not None else None


async def list_runtime(conn: aiosqlite.Connection) -> list[RuntimeRow]:
    cursor = await conn.execute(_RUNTIME_SELECT + " ORDER BY last_activity DESC, task_id")
    return [_runtime_from_row(row) for row in await cursor.fetchall()]


async def update_runtime(
    conn: aiosqlite.Connection, task_id: str, *, now: int | None = None, **changes: Any
) -> RuntimeRow | None:
    allowed = {"session_id", "port", "status", "cwd", "app_exchanges", "owner_alerted",
                "promotion_state", "binding_id", "cleanup_pending", "channel_owned", "last_activity",
                "app_exchange_budget"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"unknown runtime fields: {sorted(unknown)}")
    if not changes:
        return await get_runtime(conn, task_id)
    fields = list(changes)
    values = [int(value) if field in {"owner_alerted", "cleanup_pending", "channel_owned"} else value
              for field, value in changes.items()]
    fields.append("updated_at")
    values.append(_now(now))
    await conn.execute(
        f"UPDATE task_runtime SET {', '.join(f'{field} = ?' for field in fields)} WHERE task_id = ?",
        (*values, str(task_id)),
    )
    await conn.commit()
    return await get_runtime(conn, task_id)


async def restore_runtime_binding(
    conn: aiosqlite.Connection, task_id: str, old_key: ConversationKey,
    *, status: str = "running", now: int | None = None,
) -> RuntimeRow | None:
    """Restore the old conversation binding after an interrupted promotion."""
    runtime = await get_runtime(conn, task_id)
    if runtime is None:
        return None
    restored = RuntimeRow(
        task_id=runtime.task_id, key=old_key, session_id=runtime.session_id,
        port=runtime.port, status=status, cwd=runtime.cwd, owner=runtime.owner,
        created_at=runtime.created_at, last_activity=runtime.last_activity,
        app_exchange_budget=runtime.app_exchange_budget, app_exchanges=runtime.app_exchanges,
        owner_alerted=runtime.owner_alerted, promotion_state="failed",
        binding_id=None, cleanup_pending=runtime.cleanup_pending,
        channel_owned=False,
    )
    await replace_runtime_binding(conn, old_key, restored, [], now=now)
    return restored


async def replace_runtime_binding(
    conn: aiosqlite.Connection,
    old_key: ConversationKey,
    runtime: RuntimeRow,
    participants: list[Participant],
    *,
    binding: tuple[str, str, str, ActorId | None] | None = None,
    now: int | None = None,
) -> RuntimeRow:
    """Commit a promotion swap as one DB transaction after Slack side effects."""
    stamp = _now(now)
    await conn.execute("BEGIN")
    try:
        await _root_sql(conn, old_key, Owner(runtime.owner.actor_id, runtime.owner.kind, Mode("disabled")), stamp)
        if binding is not None:
            bid, target_id, target_kind, promoted_by = binding
            await conn.execute(
                "UPDATE promotion_bindings SET active = 0, ended_at = ? WHERE team_id = ? AND channel_id = ? AND root_id = ? AND active = 1",
                (stamp, old_key.team_id, old_key.channel_id, old_key.root_id),
            )
            await conn.execute(
                "INSERT INTO promotion_bindings(binding_id, team_id, channel_id, root_id, target_id, target_kind, promoted_by, created_at, ended_at, active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 1)",
                (bid, old_key.team_id, old_key.channel_id, old_key.root_id, target_id, target_kind, promoted_by, stamp),
            )
        await _root_sql(conn, runtime.key, runtime.owner, stamp)
        await conn.execute(
            "DELETE FROM participants WHERE team_id = ? AND channel_id = ? AND root_id = ?",
            (runtime.key.team_id, runtime.key.channel_id, runtime.key.root_id),
        )
        for participant in participants:
            await _participant_sql(conn, runtime.key, participant, stamp)
        await conn.execute(
            "UPDATE task_runtime SET team_id=?, channel_id=?, root_id=?, status=?, cwd=?, session_id=?, port=?, owner_id=?, owner_kind=?, mode=?, last_activity=?, app_exchange_budget=?, app_exchanges=?, owner_alerted=?, promotion_state=?, binding_id=?, cleanup_pending=?, channel_owned=?, updated_at=? WHERE task_id=?",
            (runtime.key.team_id, runtime.key.channel_id, runtime.key.root_id, runtime.status,
             runtime.cwd, runtime.session_id, runtime.port, runtime.owner.actor_id, runtime.owner.kind.value,
             runtime.owner.mode, runtime.last_activity, runtime.app_exchange_budget, runtime.app_exchanges,
             int(runtime.owner_alerted), runtime.promotion_state, runtime.binding_id, int(runtime.cleanup_pending), int(runtime.channel_owned),
             stamp, runtime.task_id),
        )
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise
    return runtime


# -- Promotion journal -------------------------------------------------------

_JOURNAL_SELECT = """SELECT journal_id, task_id, team_id, old_channel_id, old_root_id,
 old_mode, old_binding_id, new_channel_id, new_root_id, new_binding_id, state,
 side_effect, side_effect_state, error_code, created_at, updated_at
 FROM promotion_journal"""


def _journal_from_row(row: tuple[Any, ...]) -> PromotionJournalRow:
    return PromotionJournalRow(
        journal_id=str(row[0]), task_id=str(row[1]),
        old_key=ConversationKey(TeamId(row[2]), ChannelId(row[3]), RootId(row[4])),
        old_mode=str(row[5]), old_binding_id=row[6], new_channel_id=row[7],
        new_root_id=row[8], new_binding_id=row[9], state=str(row[10]),
        side_effect=str(row[11]), side_effect_state=str(row[12]),
        error_code=row[13], created_at=int(row[14]), updated_at=int(row[15]),
    )


async def create_promotion_journal(
    conn: aiosqlite.Connection, journal_id: str, task_id: str, old_key: ConversationKey,
    old_mode: str, old_binding_id: str | None = None, *, now: int | None = None,
) -> PromotionJournalRow:
    stamp = _now(now)
    await conn.execute(
        """INSERT INTO promotion_journal
        (journal_id, task_id, team_id, old_channel_id, old_root_id, old_mode,
         old_binding_id, state, side_effect, side_effect_state, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'preparing', 'create_channel', 'pending', ?, ?)""",
        (str(journal_id), str(task_id), old_key.team_id, old_key.channel_id,
         old_key.root_id, str(old_mode), old_binding_id, stamp, stamp),
    )
    await conn.commit()
    row = await get_promotion_journal(conn, str(journal_id))
    assert row is not None
    return row


async def get_promotion_journal(conn: aiosqlite.Connection, journal_id: str) -> PromotionJournalRow | None:
    cursor = await conn.execute(_JOURNAL_SELECT + " WHERE journal_id = ?", (str(journal_id),))
    row = await cursor.fetchone()
    return _journal_from_row(row) if row is not None else None


async def list_pending_promotion_journals(conn: aiosqlite.Connection) -> list[PromotionJournalRow]:
    cursor = await conn.execute(_JOURNAL_SELECT + " WHERE state IN ('preparing', 'rebinding', 'cleanup_pending') ORDER BY created_at, journal_id")
    return [_journal_from_row(row) for row in await cursor.fetchall()]


async def update_promotion_journal(
    conn: aiosqlite.Connection, journal_id: str, *, now: int | None = None, **changes: Any
) -> PromotionJournalRow | None:
    allowed = {"new_channel_id", "new_root_id", "new_binding_id", "state", "side_effect", "side_effect_state", "error_code"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"unknown promotion journal fields: {sorted(unknown)}")
    if not changes:
        return await get_promotion_journal(conn, journal_id)
    fields = list(changes)
    values = list(changes.values())
    fields.append("updated_at")
    values.append(_now(now))
    await conn.execute(
        f"UPDATE promotion_journal SET {', '.join(f'{field} = ?' for field in fields)} WHERE journal_id = ?",
        (*values, str(journal_id)),
    )
    await conn.commit()
    return await get_promotion_journal(conn, journal_id)


# -- Normalized Slack roots -------------------------------------------------

async def upsert_root(
    conn: aiosqlite.Connection,
    key: ConversationKey,
    owner: Owner,
    *,
    now: int | None = None,
) -> RootRow:
    key = _key(key)
    if not isinstance(owner, Owner):
        raise TypeError("owner must be an Owner")
    stamp = _now(now)
    await conn.execute(
        """
        INSERT INTO roots
          (team_id, channel_id, root_id, owner_id, owner_kind, mode, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(team_id, channel_id, root_id) DO UPDATE SET
          owner_id=excluded.owner_id, owner_kind=excluded.owner_kind,
          mode=excluded.mode, updated_at=excluded.updated_at
        """,
        (
            key.team_id,
            key.channel_id,
            key.root_id,
            owner.actor_id,
            owner.kind.value,
            owner.mode,
            stamp,
            stamp,
        ),
    )
    await conn.commit()
    saved = await get_root(conn, key)
    assert saved is not None
    return saved


async def get_root(conn: aiosqlite.Connection, key: ConversationKey) -> RootRow | None:
    key = _key(key)
    cursor = await conn.execute(
        """
        SELECT owner_id, owner_kind, mode, created_at, updated_at
        FROM roots WHERE team_id = ? AND channel_id = ? AND root_id = ?
        """,
        (key.team_id, key.channel_id, key.root_id),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    owner = Owner(ActorId(row[0]), ParticipantKind(row[1]), Mode(row[2]))
    return RootRow(key.team_id, key.channel_id, key.root_id, owner, row[3], row[4])


async def delete_root(conn: aiosqlite.Connection, key: ConversationKey) -> bool:
    key = _key(key)
    cursor = await conn.execute(
        "DELETE FROM roots WHERE team_id = ? AND channel_id = ? AND root_id = ?",
        (key.team_id, key.channel_id, key.root_id),
    )
    await conn.commit()
    return cursor.rowcount > 0


async def upsert_participant(
    conn: aiosqlite.Connection,
    key: ConversationKey,
    participant: Participant,
    *,
    now: int | None = None,
) -> ParticipantRow:
    key = _key(key)
    if not isinstance(participant, Participant):
        raise TypeError("participant must be a Participant")
    stamp = _now(now)
    await conn.execute(
        """
        INSERT INTO participants
          (team_id, channel_id, root_id, actor_id, kind, display_name, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(team_id, channel_id, root_id, actor_id) DO UPDATE SET
          kind=excluded.kind, display_name=excluded.display_name, updated_at=excluded.updated_at
        """,
        (
            key.team_id, key.channel_id, key.root_id, participant.actor_id,
            participant.kind.value, participant.display_name, stamp, stamp,
        ),
    )
    await conn.commit()
    cursor = await conn.execute(
        """
        SELECT created_at, updated_at FROM participants
        WHERE team_id = ? AND channel_id = ? AND root_id = ? AND actor_id = ?
        """,
        (key.team_id, key.channel_id, key.root_id, participant.actor_id),
    )
    saved = await cursor.fetchone()
    assert saved is not None
    return ParticipantRow(key, participant, saved[0], saved[1])


async def list_participants(
    conn: aiosqlite.Connection, key: ConversationKey
) -> list[ParticipantRow]:
    key = _key(key)
    cursor = await conn.execute(
        """
        SELECT actor_id, kind, display_name, created_at, updated_at
        FROM participants
        WHERE team_id = ? AND channel_id = ? AND root_id = ?
        ORDER BY actor_id
        """,
        (key.team_id, key.channel_id, key.root_id),
    )
    return [
        ParticipantRow(
            key,
            Participant(ActorId(row[0]), ParticipantKind(row[1]), row[2]),
            row[3], row[4],
        )
        for row in await cursor.fetchall()
    ]


async def delete_participant(
    conn: aiosqlite.Connection, key: ConversationKey, actor_id: ActorId
) -> bool:
    key = _key(key)
    actor_id = ActorId(actor_id)
    cursor = await conn.execute(
        """
        DELETE FROM participants
        WHERE team_id = ? AND channel_id = ? AND root_id = ? AND actor_id = ?
        """,
        (key.team_id, key.channel_id, key.root_id, actor_id),
    )
    await conn.commit()
    return cursor.rowcount > 0


# -- Promotion binding history ----------------------------------------------

async def promote_root(
    conn: aiosqlite.Connection,
    key: ConversationKey,
    target_id: str,
    target_kind: str,
    *,
    promoted_by: ActorId | None = None,
    binding_id: str | None = None,
    now: int | None = None,
) -> PromotionBinding:
    key = _key(key)
    target_id = str(target_id).strip()
    target_kind = str(target_kind).strip()
    if not target_id or not target_kind:
        raise ValueError("promotion target_id and target_kind must not be empty")
    stamp = _now(now)
    bid = str(binding_id).strip() if binding_id is not None else ""
    if binding_id is not None and not bid:
        raise ValueError("binding_id must not be empty")
    promoted_by = ActorId(promoted_by) if promoted_by is not None else None

    # One transaction makes promotion replacement atomic. The unique partial
    # index is a second line of defense against concurrent writers. The
    # sequence suffix keeps generated IDs unique when several promotions share
    # a timestamp (which is common in tests and low-resolution event clocks).
    await conn.execute("BEGIN")
    try:
        if not bid:
            cursor = await conn.execute(
                """
                SELECT COUNT(*) FROM promotion_bindings
                WHERE team_id = ? AND channel_id = ? AND root_id = ?
                """,
                (key.team_id, key.channel_id, key.root_id),
            )
            sequence = int((await cursor.fetchone())[0]) + 1
            bid = f"{key.team_id}:{key.channel_id}:{key.root_id}:{stamp}:{sequence}"
        await conn.execute(
            """
            UPDATE promotion_bindings SET active = 0, ended_at = ?
            WHERE team_id = ? AND channel_id = ? AND root_id = ? AND active = 1
            """,
            (stamp, key.team_id, key.channel_id, key.root_id),
        )
        await conn.execute(
            """
            INSERT INTO promotion_bindings
              (binding_id, team_id, channel_id, root_id, target_id, target_kind,
               promoted_by, created_at, ended_at, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 1)
            """,
            (
                bid, key.team_id, key.channel_id, key.root_id, target_id,
                target_kind, promoted_by, stamp,
            ),
        )
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise
    return PromotionBinding(bid, target_id, target_kind, stamp, None, promoted_by, True)


async def get_active_promotion(
    conn: aiosqlite.Connection, key: ConversationKey
) -> PromotionBinding | None:
    key = _key(key)
    cursor = await conn.execute(
        """
        SELECT binding_id, target_id, target_kind, created_at, ended_at, promoted_by, active
        FROM promotion_bindings
        WHERE team_id = ? AND channel_id = ? AND root_id = ? AND active = 1
        ORDER BY created_at DESC, binding_id DESC LIMIT 1
        """,
        (key.team_id, key.channel_id, key.root_id),
    )
    row = await cursor.fetchone()
    return _promotion_from_row(row) if row is not None else None


def _promotion_from_row(row: tuple[Any, ...]) -> PromotionBinding:
    return PromotionBinding(
        row[0], row[1], row[2], row[3], row[4],
        ActorId(row[5]) if row[5] is not None else None, bool(row[6]),
    )


async def list_promotion_bindings(
    conn: aiosqlite.Connection, key: ConversationKey
) -> list[PromotionBinding]:
    key = _key(key)
    cursor = await conn.execute(
        """
        SELECT binding_id, target_id, target_kind, created_at, ended_at, promoted_by, active
        FROM promotion_bindings
        WHERE team_id = ? AND channel_id = ? AND root_id = ?
        ORDER BY created_at ASC, binding_id ASC
        """,
        (key.team_id, key.channel_id, key.root_id),
    )
    return [_promotion_from_row(row) for row in await cursor.fetchall()]


async def end_promotion(
    conn: aiosqlite.Connection, key: ConversationKey, *, now: int | None = None
) -> bool:
    key = _key(key)
    stamp = _now(now)
    cursor = await conn.execute(
        """
        UPDATE promotion_bindings SET active = 0, ended_at = ?
        WHERE team_id = ? AND channel_id = ? AND root_id = ? AND active = 1
        """,
        (stamp, key.team_id, key.channel_id, key.root_id),
    )
    await conn.commit()
    return cursor.rowcount > 0


# -- Event deduplication -----------------------------------------------------

async def mark_event_seen(
    conn: aiosqlite.Connection,
    team_id: TeamId,
    event_id: EventId,
    *,
    now: int | None = None,
    max_records: int = DEDUP_MAX_RECORDS,
) -> bool:
    """Record an event and return True only for the first occurrence.

    Retention is bounded and deterministic: oldest ``seen_at`` records are
    evicted first, with team/event IDs as stable tie breakers.
    """
    if max_records <= 0:
        raise ValueError("max_records must be positive")
    team_id, event_id, stamp = TeamId(team_id), EventId(event_id), _now(now)
    cursor = await conn.execute(
        "INSERT OR IGNORE INTO dedup_records(team_id, event_id, seen_at) VALUES (?, ?, ?)",
        (team_id, event_id, stamp),
    )
    inserted = cursor.rowcount > 0
    await conn.execute(
        """
        DELETE FROM dedup_records
        WHERE rowid IN (
            SELECT rowid FROM dedup_records
            ORDER BY seen_at ASC, team_id ASC, event_id ASC
            LIMIT MAX(0, (SELECT COUNT(*) FROM dedup_records) - ?)
        )
        """,
        (max_records,),
    )
    await conn.commit()
    return inserted


async def prune_dedup_records(
    conn: aiosqlite.Connection, *, max_records: int = DEDUP_MAX_RECORDS
) -> int:
    if max_records <= 0:
        raise ValueError("max_records must be positive")
    before = await conn.execute("SELECT COUNT(*) FROM dedup_records")
    count = int((await before.fetchone())[0])
    await conn.execute(
        """
        DELETE FROM dedup_records
        WHERE rowid IN (
            SELECT rowid FROM dedup_records
            ORDER BY seen_at ASC, team_id ASC, event_id ASC
            LIMIT MAX(0, (SELECT COUNT(*) FROM dedup_records) - ?)
        )
        """,
        (max_records,),
    )
    await conn.commit()
    return max(0, count - max_records)


async def is_event_seen(
    conn: aiosqlite.Connection, team_id: TeamId, event_id: EventId
) -> bool:
    cursor = await conn.execute(
        "SELECT 1 FROM dedup_records WHERE team_id = ? AND event_id = ?",
        (TeamId(team_id), EventId(event_id)),
    )
    return await cursor.fetchone() is not None


# -- Actor-targeted interrogatives ------------------------------------------

async def put_pending_interrogative(
    conn: aiosqlite.Connection,
    key: ConversationKey,
    pending: PendingInterrogative,
    *,
    binding_id: str = "",
    target_kind: ParticipantKind = ParticipantKind.HUMAN,
) -> PendingInterrogative:
    key = _key(key)
    if not isinstance(pending, PendingInterrogative):
        raise TypeError("pending must be a PendingInterrogative")
    await conn.execute(
        """
        INSERT INTO pending_interrogatives
          (team_id, channel_id, root_id, actor_id, interrogative_id,
           payload_json, binding_id, target_kind, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(team_id, channel_id, root_id, actor_id) DO UPDATE SET
          interrogative_id=excluded.interrogative_id,
          payload_json=excluded.payload_json,
          binding_id=excluded.binding_id,
          target_kind=excluded.target_kind,
          created_at=excluded.created_at,
          expires_at=excluded.expires_at
        """,
        (
            key.team_id, key.channel_id, key.root_id, pending.actor_id,
            pending.interrogative_id, _json(pending.payload), str(binding_id),
            ParticipantKind(target_kind).value, pending.created_at, pending.expires_at,
        ),
    )
    await conn.commit()
    return pending


async def get_pending_interrogative(
    conn: aiosqlite.Connection,
    key: ConversationKey,
    actor_id: ActorId,
    *,
    now: int | None = None,
) -> PendingInterrogative | None:
    key = _key(key)
    actor_id, stamp = ActorId(actor_id), _now(now)
    cursor = await conn.execute(
        """
        SELECT interrogative_id, payload_json, created_at, expires_at
        FROM pending_interrogatives
        WHERE team_id = ? AND channel_id = ? AND root_id = ? AND actor_id = ?
        """,
        (key.team_id, key.channel_id, key.root_id, actor_id),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    if row[3] <= stamp:
        await conn.execute(
            """
            DELETE FROM pending_interrogatives
            WHERE team_id = ? AND channel_id = ? AND root_id = ? AND actor_id = ?
            """,
            (key.team_id, key.channel_id, key.root_id, actor_id),
        )
        await conn.commit()
        return None
    return PendingInterrogative(row[0], actor_id, _decode_json(row[1]), row[3], row[2])


async def list_pending_interrogatives(
    conn: aiosqlite.Connection,
    key: ConversationKey,
    *,
    now: int | None = None,
) -> list[PendingInterrogative]:
    key = _key(key)
    stamp = _now(now)
    await conn.execute(
        "DELETE FROM pending_interrogatives WHERE expires_at <= ?", (stamp,)
    )
    cursor = await conn.execute(
        """
        SELECT interrogative_id, actor_id, payload_json, created_at, expires_at
        FROM pending_interrogatives
        WHERE team_id = ? AND channel_id = ? AND root_id = ?
        ORDER BY expires_at ASC, actor_id ASC
        """,
        (key.team_id, key.channel_id, key.root_id),
    )
    rows = await cursor.fetchall()
    await conn.commit()
    return [
        PendingInterrogative(row[0], ActorId(row[1]), _decode_json(row[2]), row[4], row[3])
        for row in rows
    ]


async def consume_pending_interrogative(
    conn: aiosqlite.Connection,
    key: ConversationKey,
    actor_id: ActorId,
    *,
    interrogative_id: str,
    binding_id: str = "",
    now: int | None = None,
) -> bool:
    """Atomically claim a non-expired exact-bound interrogative once."""
    stamp = _now(now)
    cursor = await conn.execute(
        "DELETE FROM pending_interrogatives WHERE team_id=? AND channel_id=? AND root_id=? AND actor_id=? AND interrogative_id=? AND binding_id=? AND expires_at > ?",
        (key.team_id, key.channel_id, key.root_id, ActorId(actor_id), str(interrogative_id), str(binding_id), stamp),
    )
    await conn.commit()
    return cursor.rowcount > 0


async def clear_pending_interrogative(
    conn: aiosqlite.Connection,
    key: ConversationKey,
    actor_id: ActorId,
) -> bool:
    key = _key(key)
    cursor = await conn.execute(
        """
        DELETE FROM pending_interrogatives
        WHERE team_id = ? AND channel_id = ? AND root_id = ? AND actor_id = ?
        """,
        (key.team_id, key.channel_id, key.root_id, ActorId(actor_id)),
    )
    await conn.commit()
    return cursor.rowcount > 0


# -- Text pins ---------------------------------------------------------------

async def upsert_text_pin(
    conn: aiosqlite.Connection, pin: TextPin
) -> TextPin:
    if not isinstance(pin, TextPin):
        raise TypeError("pin must be a TextPin")
    key = _key(pin.key)
    await conn.execute(
        """
        INSERT INTO text_pins
          (pin_id, team_id, channel_id, root_id, actor_id, text, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(pin_id) DO UPDATE SET
          team_id=excluded.team_id, channel_id=excluded.channel_id,
          root_id=excluded.root_id, actor_id=excluded.actor_id,
          text=excluded.text, updated_at=excluded.updated_at
        """,
        (
            pin.pin_id, key.team_id, key.channel_id, key.root_id, pin.actor_id,
            pin.text, pin.created_at, pin.updated_at,
        ),
    )
    await conn.commit()
    return pin


def _pin_from_row(row: tuple[Any, ...]) -> TextPin:
    return TextPin(
        row[0], ConversationKey(TeamId(row[1]), ChannelId(row[2]), RootId(row[3])),
        row[5], ActorId(row[4]) if row[4] is not None else None, row[6], row[7],
    )


async def get_text_pin(
    conn: aiosqlite.Connection,
    key: ConversationKey,
    pin_id: str | None = None,
) -> TextPin | None:
    key = _key(key)
    if pin_id is None:
        cursor = await conn.execute(
            """
            SELECT pin_id, team_id, channel_id, root_id, actor_id, text, created_at, updated_at
            FROM text_pins WHERE team_id = ? AND channel_id = ? AND root_id = ?
            ORDER BY updated_at DESC, pin_id DESC LIMIT 1
            """,
            (key.team_id, key.channel_id, key.root_id),
        )
    else:
        cursor = await conn.execute(
            """
            SELECT pin_id, team_id, channel_id, root_id, actor_id, text, created_at, updated_at
            FROM text_pins WHERE pin_id = ? AND team_id = ? AND channel_id = ? AND root_id = ?
            """,
            (str(pin_id), key.team_id, key.channel_id, key.root_id),
        )
    row = await cursor.fetchone()
    return _pin_from_row(row) if row is not None else None


async def list_text_pins(
    conn: aiosqlite.Connection, key: ConversationKey
) -> list[TextPin]:
    key = _key(key)
    cursor = await conn.execute(
        """
        SELECT pin_id, team_id, channel_id, root_id, actor_id, text, created_at, updated_at
        FROM text_pins WHERE team_id = ? AND channel_id = ? AND root_id = ?
        ORDER BY updated_at DESC, pin_id ASC
        """,
        (key.team_id, key.channel_id, key.root_id),
    )
    return [_pin_from_row(row) for row in await cursor.fetchall()]


async def delete_text_pin(
    conn: aiosqlite.Connection, key: ConversationKey, pin_id: str
) -> bool:
    key = _key(key)
    cursor = await conn.execute(
        """
        DELETE FROM text_pins
        WHERE pin_id = ? AND team_id = ? AND channel_id = ? AND root_id = ?
        """,
        (str(pin_id), key.team_id, key.channel_id, key.root_id),
    )
    await conn.commit()
    return cursor.rowcount > 0


# Friendly aliases used by adapters that prefer repository verbs.
create_root = upsert_root
save_root = upsert_root
add_participant = upsert_participant
record_event = mark_event_seen
seen_event = is_event_seen
create_promotion = promote_root
get_promotion = get_active_promotion
save_pending_interrogative = put_pending_interrogative
save_text_pin = upsert_text_pin


__all__ = [
    "DEDUP_MAX_RECORDS",
    "DEFAULT_DB_PATH",
    "DEFAULT_STATE_DIR",
    "PROMOTION_STATES",
    "PROMOTION_JOURNAL_STATES",
    "RUNTIME_ACTIVE_STATUSES",
    "PromotionJournalRow",
    "RuntimeRow",
    "RuntimeParticipant",
    "consume_pending_interrogative",
    "ensure_state_dir",
    "get_runtime",
    "list_runtime",
    "replace_runtime_binding",
    "restore_runtime_binding",
    "update_runtime",
    "upsert_runtime",
    "DedupRow",
    "ParticipantRow",
    "RootRow",
    "add_participant",
    "clear_pending_interrogative",
    "close_db",
    "create_promotion_journal",
    "get_promotion_journal",
    "list_pending_promotion_journals",
    "update_promotion_journal",
    "create_promotion",
    "create_root",
    "delete_participant",
    "delete_root",
    "delete_text_pin",
    "end_promotion",
    "get_active_promotion",
    "get_pending_interrogative",
    "get_promotion",
    "get_root",
    "get_text_pin",
    "init_schema",
    "is_event_seen",
    "list_participants",
    "list_pending_interrogatives",
    "list_promotion_bindings",
    "list_text_pins",
    "mark_event_seen",
    "open_db",
    "promote_root",
    "prune_dedup_records",
    "put_pending_interrogative",
    "record_event",
    "save_pending_interrogative",
    "save_root",
    "save_text_pin",
    "seen_event",
    "upsert_participant",
    "upsert_root",
    "upsert_text_pin",
]

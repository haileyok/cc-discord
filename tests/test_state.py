"""Tests for normalized Slack persistence and legacy migration."""

import aiosqlite
import pytest

from bridge import state
from bridge.domain import ConversationKey, Owner, Participant, ParticipantKind
from bridge.tasks import TaskRegistry, Task


def key() -> ConversationKey:
    return ConversationKey("T1", "C1", "R1")


@pytest.mark.asyncio
async def test_fresh_schema_contains_only_normalized_slack_tables(in_memory_db) -> None:
    cursor = await in_memory_db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    )
    names = {row[0] for row in await cursor.fetchall()}
    assert {"roots", "participants", "promotion_bindings", "dedup_records",
            "pending_interrogatives", "text_pins", "schema_meta"} <= names
    assert not {"sessions", "tasks", "pins"} & names


@pytest.mark.asyncio
async def test_compaction_operation_round_trips_in_runtime(in_memory_db) -> None:
    runtime = state.RuntimeRow(
        "task-compact", key(), "sess", 1234, "running", "/tmp",
        Owner("UOWNER"), 1, 2, 20, 0, False,
        compaction_pending=True, compaction_id="compact-123",
    )
    await state.upsert_runtime(in_memory_db, runtime)
    restored = await state.get_runtime(in_memory_db, "task-compact")
    assert restored is not None
    assert restored.compaction_pending is True
    assert restored.compaction_id == "compact-123"


@pytest.mark.asyncio
async def test_restore_runtime_binding_preserves_old_root_participants() -> None:
    conn = await aiosqlite.connect(":memory:")
    try:
        await state.init_schema(conn)
        old = ConversationKey("T1", "COLD", "R1")
        new = ConversationKey("T1", "CNEW", "R2")
        runtime = state.RuntimeRow("task", new, "sess", 1234, "rebinding", "/tmp", Owner("UOWNER"), 1, 2, 20, 0, False, "preparing", "bind", False, True)
        await state.upsert_runtime(conn, runtime)
        await state.upsert_participant(conn, old, Participant("U1", ParticipantKind.HUMAN, "Human one"), now=1)
        await state.upsert_participant(conn, old, Participant("B1", ParticipantKind.APP, "Other app"), now=2)
        restored = await state.restore_runtime_binding(conn, "task", old)
        assert restored is not None and restored.key == old
        assert [(str(row.participant.actor_id), row.participant.kind, row.participant.display_name)
                for row in await state.list_participants(conn, old)] == [
                    ("B1", ParticipantKind.APP, "Other app"),
                    ("U1", ParticipantKind.HUMAN, "Human one"),
                ]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_legacy_tables_are_dropped_once_without_losing_slack_state() -> None:
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute(
            "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, cwd TEXT, "
            "thread_id INTEGER, created_at INTEGER, last_activity INTEGER)"
        )
        await conn.execute(
            "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, thread_id INTEGER, cwd TEXT, "
            "status TEXT, polytoken_session_id TEXT, port INTEGER, created_at INTEGER, last_activity INTEGER)"
        )
        await conn.execute(
            "CREATE TABLE pins (channel_id INTEGER PRIMARY KEY, cwd TEXT, "
            "created_at INTEGER, last_used_at INTEGER)"
        )
        await conn.execute("CREATE TABLE approval_log (request_id TEXT PRIMARY KEY)")
        await conn.commit()

        await state.init_schema(conn)
        await state.upsert_root(conn, key(), Owner("U1"), now=10)
        await state.init_schema(conn)

        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        names = {row[0] for row in await cursor.fetchall()}
        assert not {"sessions", "tasks", "pins", "approval_log"} & names
        assert await state.get_root(conn, key()) is not None
        marker = await conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?", ("legacy_runtime_reset_v1",)
        )
        assert (await marker.fetchone())[0] == "completed"
    finally:
        await conn.close()

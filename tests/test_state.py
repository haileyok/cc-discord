"""Tests for normalized Slack persistence and legacy migration."""

import aiosqlite
import pytest

from bridge import state
from bridge.domain import ConversationKey, Owner


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

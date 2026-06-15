"""Tests for bridge.state with the Polytoken daemon schema."""

import aiosqlite
import pytest

from bridge import state


class TestTasks:
    async def test_upsert_and_get(self, in_memory_db) -> None:
        await state.upsert_task(
            in_memory_db, "t1", 100, "/work", "running",
            polytoken_session_id="sess-1", port=40001, now=10,
        )
        row = await state.get_task(in_memory_db, "t1")
        assert row is not None
        assert row.thread_id == 100
        assert row.cwd == "/work"
        assert row.status == "running"
        assert row.polytoken_session_id == "sess-1"
        assert row.port == 40001

    async def test_get_by_thread_and_session(self, in_memory_db) -> None:
        await state.upsert_task(
            in_memory_db, "t1", 100, "/work", "running",
            polytoken_session_id="sess-1", port=40001,
        )
        assert (await state.get_task_by_thread_id(in_memory_db, 100)).task_id == "t1"
        assert (await state.get_task_by_session_id(in_memory_db, "sess-1")).task_id == "t1"
        assert await state.get_task_by_thread_id(in_memory_db, 999) is None

    async def test_upsert_preserves_created_at(self, in_memory_db) -> None:
        await state.upsert_task(in_memory_db, "t1", 100, "/w", "spawning", now=10)
        await state.upsert_task(
            in_memory_db, "t1", 100, "/w", "running",
            polytoken_session_id="s", port=1, now=20,
        )
        row = await state.get_task(in_memory_db, "t1")
        assert row.created_at == 10
        assert row.last_activity == 20
        assert row.status == "running"

    async def test_list_active_excludes_terminal(self, in_memory_db) -> None:
        await state.upsert_task(in_memory_db, "t1", 100, "/w", "running", now=30)
        await state.upsert_task(in_memory_db, "t2", 101, "/w", "spawning", now=20)
        await state.upsert_task(in_memory_db, "t3", 102, "/w", "stopped", now=10)
        await state.upsert_task(in_memory_db, "t4", 103, "/w", "crashed", now=5)
        active = await state.list_active_tasks(in_memory_db)
        assert [t.task_id for t in active] == ["t1", "t2"]  # DESC by last_activity

    async def test_delete_task(self, in_memory_db) -> None:
        await state.upsert_task(in_memory_db, "t1", 100, "/w", "running")
        await state.delete_task(in_memory_db, "t1")
        assert await state.get_task(in_memory_db, "t1") is None


class TestMigration:
    async def test_drops_legacy_tasks_and_approval_log(self) -> None:
        conn = await aiosqlite.connect(":memory:")
        try:
            # Simulate a pre-daemon DB.
            await conn.execute(
                "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, thread_id INTEGER, "
                "zellij_pane_id TEXT, cwd TEXT, status TEXT, current_claude_session_id TEXT, "
                "current_transcript_path TEXT, created_at INTEGER, last_activity INTEGER)"
            )
            await conn.execute("CREATE TABLE approval_log (request_id TEXT PRIMARY KEY)")
            await conn.execute(
                "INSERT INTO tasks (task_id, thread_id, cwd, status, created_at, last_activity) "
                "VALUES ('old', 1, '/w', 'running', 0, 0)"
            )
            await conn.commit()

            await state.init_schema(conn)

            # Legacy rows dropped, new columns present, approval_log gone.
            assert await state.get_task(conn, "old") is None
            cur = await conn.execute("PRAGMA table_info(tasks)")
            cols = {r[1] for r in await cur.fetchall()}
            assert "polytoken_session_id" in cols and "port" in cols
            assert "zellij_pane_id" not in cols
            cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='approval_log'"
            )
            assert await cur.fetchone() is None
        finally:
            await conn.close()

    async def test_fresh_db_has_no_approval_log(self, in_memory_db) -> None:
        cur = await in_memory_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='approval_log'"
        )
        assert await cur.fetchone() is None


class TestPins:
    async def test_pin_roundtrip(self, in_memory_db) -> None:
        await state.upsert_pin(in_memory_db, 500, "/proj", now=1)
        pin = await state.get_pin(in_memory_db, 500)
        assert pin is not None and pin.cwd == "/proj"
        assert await state.delete_pin(in_memory_db, 500) is True
        assert await state.get_pin(in_memory_db, 500) is None

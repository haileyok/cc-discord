from pathlib import Path

import aiosqlite
import pytest

import time

from bridge.state import open_db, close_db, SessionRow, get_session, upsert_session, delete_session


@pytest.mark.asyncio
class TestOpenDB:
    """open_db creates and initializes the database."""

    async def test_opens_connection(self, tmp_path: Path) -> None:
        """open_db returns a usable aiosqlite connection."""
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            # Verify it's a valid connection by executing a query
            cursor = await conn.execute("SELECT 1")
            result = await cursor.fetchone()
            assert result == (1,)
        finally:
            await conn.close()

    async def test_creates_parent_dir(self, tmp_path: Path) -> None:
        """open_db creates parent directory if missing."""
        db_path = tmp_path / "subdir1" / "subdir2" / "state.db"
        conn = await open_db(db_path)
        try:
            assert db_path.exists()
        finally:
            await conn.close()

    async def test_sets_wal_mode(self, tmp_path: Path) -> None:
        """After open, PRAGMA journal_mode reports wal."""
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            cursor = await conn.execute("PRAGMA journal_mode")
            mode = await cursor.fetchone()
            assert mode[0].lower() == "wal"
        finally:
            await conn.close()

    async def test_creates_sessions_table(self, tmp_path: Path) -> None:
        """Schema verification: sessions table exists."""
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
            )
            result = await cursor.fetchone()
            assert result is not None
            assert result[0] == "sessions"
        finally:
            await conn.close()

    async def test_idempotent_multiple_opens(self, tmp_path: Path) -> None:
        """Calling open_db twice on the same path is idempotent."""
        db_path = tmp_path / "state.db"
        conn1 = await open_db(db_path)
        conn2 = await open_db(db_path)
        try:
            # Both should work and be valid
            cursor1 = await conn1.execute("SELECT 1")
            result1 = await cursor1.fetchone()
            assert result1 == (1,)

            cursor2 = await conn2.execute("SELECT 1")
            result2 = await cursor2.fetchone()
            assert result2 == (1,)
        finally:
            await conn1.close()
            await conn2.close()

    async def test_sessions_table_schema(self, tmp_path: Path) -> None:
        """Sessions table has the correct columns."""
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            cursor = await conn.execute("PRAGMA table_info(sessions)")
            columns = await cursor.fetchall()
            col_names = {col[1] for col in columns}
            assert col_names == {
                "session_id",
                "cwd",
                "thread_id",
                "created_at",
                "last_activity",
            }
        finally:
            await conn.close()

    async def test_sessions_table_thread_id_is_text(self, tmp_path: Path) -> None:
        """Sessions table thread_id column is TEXT type."""
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            cursor = await conn.execute("PRAGMA table_info(sessions)")
            columns = await cursor.fetchall()
            # col[1] is name, col[2] is type
            thread_id_col = [col for col in columns if col[1] == "thread_id"][0]
            assert thread_id_col[2].upper() == "TEXT", f"Expected TEXT, got {thread_id_col[2]}"
        finally:
            await conn.close()


@pytest.mark.asyncio
class TestCloseDB:
    """close_db properly closes the connection."""

    async def test_closes_connection(self, tmp_path: Path) -> None:
        """close_db actually closes (subsequent query raises)."""
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        await close_db(conn)

        # After closing, queries should fail
        with pytest.raises((aiosqlite.ProgrammingError, ValueError)):
            await conn.execute("SELECT 1")


@pytest.mark.asyncio
class TestSessionRow:
    """SessionRow dataclass."""

    async def test_session_row_creation(self) -> None:
        """SessionRow can be instantiated with all fields."""
        row = SessionRow(
            session_id="sess-123",
            cwd="/tmp/test",
            thread_id="999",
            created_at=1000,
            last_activity=2000,
        )
        assert row.session_id == "sess-123"
        assert row.cwd == "/tmp/test"
        assert row.thread_id == "999"
        assert row.created_at == 1000
        assert row.last_activity == 2000

    async def test_session_row_is_frozen(self) -> None:
        """SessionRow is immutable (frozen)."""
        row = SessionRow(
            session_id="sess-123",
            cwd="/tmp/test",
            thread_id="999",
            created_at=1000,
            last_activity=2000,
        )
        with pytest.raises(AttributeError):
            row.thread_id = "123"


@pytest.mark.asyncio
class TestGetSession:
    """get_session retrieves a session row by session_id."""

    async def test_returns_none_for_unknown_id(self, tmp_path: Path) -> None:
        """get_session returns None when session_id doesn't exist."""
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            result = await get_session(conn, "unknown")
            assert result is None
        finally:
            await conn.close()

    async def test_retrieves_inserted_session(self, tmp_path: Path) -> None:
        """get_session returns the inserted row with all fields intact."""
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            # Insert a session
            now = int(time.time())
            await conn.execute(
                "INSERT INTO sessions (session_id, cwd, thread_id, created_at, last_activity) VALUES (?, ?, ?, ?, ?)",
                ("sess-123", "/tmp/test", "999", now, now),
            )
            await conn.commit()

            # Retrieve it
            row = await get_session(conn, "sess-123")
            assert row is not None
            assert row.session_id == "sess-123"
            assert row.cwd == "/tmp/test"
            assert row.thread_id == "999"
            assert row.created_at == now
            assert row.last_activity == now
        finally:
            await conn.close()


@pytest.mark.asyncio
class TestUpsertSession:
    """upsert_session inserts or updates a session row."""

    async def test_inserts_new_session(self, tmp_path: Path) -> None:
        """upsert_session inserts a new session when it doesn't exist."""
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            now = int(time.time())
            await upsert_session(conn, "sess-123", "/tmp/test", "999", now=now)

            row = await get_session(conn, "sess-123")
            assert row is not None
            assert row.session_id == "sess-123"
            assert row.cwd == "/tmp/test"
            assert row.thread_id == "999"
            assert row.created_at == now
            assert row.last_activity == now
        finally:
            await conn.close()

    async def test_upsert_bumps_last_activity(self, tmp_path: Path) -> None:
        """upsert_session updates thread_id and bumps last_activity, preserves created_at."""
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            t0 = int(time.time())
            await upsert_session(conn, "sess-123", "/tmp/test", "999", now=t0)

            row1 = await get_session(conn, "sess-123")
            assert row1 is not None
            assert row1.created_at == t0
            assert row1.last_activity == t0
            assert row1.thread_id == "999"

            # Re-upsert with different thread_id and later timestamp
            t1 = t0 + 100
            await upsert_session(conn, "sess-123", "/tmp/test", "888", now=t1)

            row2 = await get_session(conn, "sess-123")
            assert row2 is not None
            assert row2.session_id == "sess-123"
            assert row2.cwd == "/tmp/test"
            assert row2.thread_id == "888"  # Updated
            assert row2.created_at == t0  # Preserved
            assert row2.last_activity == t1  # Bumped
        finally:
            await conn.close()

    async def test_upsert_uses_current_time_if_now_not_provided(self, tmp_path: Path) -> None:
        """upsert_session defaults to current time if now is None."""
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            before = int(time.time())
            await upsert_session(conn, "sess-123", "/tmp/test", "999")
            after = int(time.time())

            row = await get_session(conn, "sess-123")
            assert row is not None
            # last_activity should be between before and after
            assert before <= row.last_activity <= after + 1
        finally:
            await conn.close()


@pytest.mark.asyncio
class TestDeleteSession:
    """delete_session removes a session row."""

    async def test_deletes_existing_session(self, tmp_path: Path) -> None:
        """delete_session removes the row; subsequent get_session returns None."""
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            now = int(time.time())
            await upsert_session(conn, "sess-123", "/tmp/test", "999", now=now)

            row = await get_session(conn, "sess-123")
            assert row is not None

            await delete_session(conn, "sess-123")

            row = await get_session(conn, "sess-123")
            assert row is None
        finally:
            await conn.close()

    async def test_delete_nonexistent_is_safe(self, tmp_path: Path) -> None:
        """delete_session on unknown session_id doesn't raise."""
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            # Should not raise
            await delete_session(conn, "unknown")
        finally:
            await conn.close()


@pytest.mark.asyncio
class TestSessionPersistence:
    """Session mapping survives database restart (AC2.3)."""

    async def test_persistence_across_restart(self, tmp_path: Path) -> None:
        """Open DB, upsert, close; reopen the same path, get_session still returns the row."""
        db_path = tmp_path / "state.db"
        now = int(time.time())

        # First connection: insert
        conn1 = await open_db(db_path)
        try:
            await upsert_session(conn1, "sess-123", "/tmp/test", "999", now=now)
        finally:
            await conn1.close()

        # Second connection: retrieve
        conn2 = await open_db(db_path)
        try:
            row = await get_session(conn2, "sess-123")
            assert row is not None
            assert row.session_id == "sess-123"
            assert row.cwd == "/tmp/test"
            assert row.thread_id == "999"
            assert row.created_at == now
            assert row.last_activity == now
        finally:
            await conn2.close()


@pytest.mark.asyncio
class TestTasks:
    """Tests for task table and CRUD operations."""

    async def test_open_db_creates_tasks_table(self, tmp_path: Path) -> None:
        """open_db creates tasks table."""
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
            )
            result = await cursor.fetchone()
            assert result is not None
            assert result[0] == "tasks"
        finally:
            await conn.close()

    async def test_open_db_tasks_table_schema(self, tmp_path: Path) -> None:
        """Tasks table has correct columns."""
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            cursor = await conn.execute("PRAGMA table_info(tasks)")
            columns = await cursor.fetchall()
            col_names = {col[1] for col in columns}
            expected = {
                "task_id",
                "thread_id",
                "zellij_pane_id",
                "cwd",
                "status",
                "current_claude_session_id",
                "current_transcript_path",
                "created_at",
                "last_activity",
            }
            assert col_names == expected
        finally:
            await conn.close()

    async def test_tasks_table_thread_id_is_text(self, tmp_path: Path) -> None:
        """Tasks table thread_id column is TEXT type."""
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            cursor = await conn.execute("PRAGMA table_info(tasks)")
            columns = await cursor.fetchall()
            # col[1] is name, col[2] is type
            thread_id_col = [col for col in columns if col[1] == "thread_id"][0]
            assert thread_id_col[2].upper() == "TEXT", f"Expected TEXT, got {thread_id_col[2]}"
        finally:
            await conn.close()

    async def test_upsert_task_inserts_new_row(self, tmp_path: Path) -> None:
        """upsert_task inserts a new task row."""
        from bridge.state import upsert_task, get_task
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            now = int(time.time())
            await upsert_task(
                conn,
                "task-123",
                thread_id="999",
                cwd="/tmp/test",
                status="spawning",
                now=now,
            )
            row = await get_task(conn, "task-123")
            assert row is not None
            assert row.task_id == "task-123"
            assert row.thread_id == "999"
            assert row.cwd == "/tmp/test"
            assert row.status == "spawning"
            assert row.zellij_pane_id is None
            assert row.current_claude_session_id is None
            assert row.current_transcript_path is None
            assert row.created_at == now
            assert row.last_activity == now
        finally:
            await conn.close()

    async def test_upsert_task_bumps_last_activity(self, tmp_path: Path) -> None:
        """upsert_task updates status/pane and bumps last_activity, preserves created_at."""
        from bridge.state import upsert_task, get_task
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            t0 = int(time.time())
            await upsert_task(
                conn,
                "task-123",
                thread_id="999",
                cwd="/tmp/test",
                status="spawning",
                now=t0,
            )
            row1 = await get_task(conn, "task-123")
            assert row1 is not None
            assert row1.created_at == t0
            assert row1.last_activity == t0

            t1 = t0 + 100
            await upsert_task(
                conn,
                "task-123",
                thread_id="999",
                cwd="/tmp/test",
                status="running",
                zellij_pane_id="terminal_1",
                now=t1,
            )
            row2 = await get_task(conn, "task-123")
            assert row2 is not None
            assert row2.task_id == "task-123"
            assert row2.status == "running"
            assert row2.zellij_pane_id == "terminal_1"
            assert row2.created_at == t0  # Preserved
            assert row2.last_activity == t1  # Bumped
        finally:
            await conn.close()

    async def test_get_task_by_thread_id(self, tmp_path: Path) -> None:
        """get_task_by_thread_id returns correct row."""
        from bridge.state import upsert_task, get_task_by_thread_id
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            now = int(time.time())
            await upsert_task(
                conn,
                "task-123",
                thread_id="999",
                cwd="/tmp/test",
                status="running",
                now=now,
            )
            row = await get_task_by_thread_id(conn, "999")
            assert row is not None
            assert row.task_id == "task-123"
            assert row.thread_id == "999"

            # Non-existent thread
            row = await get_task_by_thread_id(conn, "888")
            assert row is None
        finally:
            await conn.close()

    async def test_get_task_by_session_id(self, tmp_path: Path) -> None:
        """get_task_by_session_id returns correct row when session_id is set."""
        from bridge.state import upsert_task, get_task_by_session_id
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            now = int(time.time())
            await upsert_task(
                conn,
                "task-123",
                thread_id="999",
                cwd="/tmp/test",
                status="running",
                current_claude_session_id="sess-abc",
                now=now,
            )
            row = await get_task_by_session_id(conn, "sess-abc")
            assert row is not None
            assert row.task_id == "task-123"
            assert row.current_claude_session_id == "sess-abc"

            # Non-existent session
            row = await get_task_by_session_id(conn, "sess-unknown")
            assert row is None
        finally:
            await conn.close()

    async def test_list_active_tasks(self, tmp_path: Path) -> None:
        """list_active_tasks returns only spawning/running tasks, ordered by last_activity DESC."""
        from bridge.state import upsert_task, list_active_tasks
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            now = int(time.time())
            # Insert tasks with different statuses
            await upsert_task(
                conn, "task-1", "1001", "/a", "spawning", now=now
            )
            await upsert_task(
                conn, "task-2", "1002", "/b", "running", now=now + 10
            )
            await upsert_task(
                conn, "task-3", "1003", "/c", "stopped", now=now + 20
            )
            await upsert_task(
                conn, "task-4", "1004", "/d", "crashed", now=now + 30
            )

            rows = await list_active_tasks(conn)
            assert len(rows) == 2  # Only spawning and running
            # Should be ordered by last_activity DESC
            assert rows[0].task_id == "task-2"  # running, last_activity = now+10
            assert rows[1].task_id == "task-1"  # spawning, last_activity = now
        finally:
            await conn.close()

    async def test_delete_task_cascade(self, tmp_path: Path) -> None:
        """delete_task removes row and cascades to approval_log."""
        from bridge.state import (
            upsert_task,
            delete_task,
            get_task,
            log_approval,
            list_approvals_for_task,
        )
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            now = int(time.time())
            await upsert_task(
                conn, "task-123", "999", "/tmp", "running", now=now
            )
            await log_approval(
                conn,
                "req-1",
                "task-123",
                "tool_exec",
                '{"cmd": "ls"}',
                "allow",
                "user approved",
                now=now,
            )

            # Verify approval logged
            approvals = await list_approvals_for_task(conn, "task-123")
            assert len(approvals) == 1

            # Delete task
            await delete_task(conn, "task-123")

            # Task gone
            row = await get_task(conn, "task-123")
            assert row is None

            # Approvals also gone
            approvals = await list_approvals_for_task(conn, "task-123")
            assert len(approvals) == 0
        finally:
            await conn.close()

    async def test_task_persistence_across_restart(self, tmp_path: Path) -> None:
        """Task rows survive database restart."""
        from bridge.state import upsert_task, get_task
        db_path = tmp_path / "state.db"
        now = int(time.time())

        # First connection
        conn1 = await open_db(db_path)
        try:
            await upsert_task(
                conn1,
                "task-123",
                thread_id="999",
                cwd="/tmp/test",
                status="running",
                zellij_pane_id="terminal_1",
                current_claude_session_id="sess-abc",
                current_transcript_path="/path/transcript",
                now=now,
            )
        finally:
            await conn1.close()

        # Second connection
        conn2 = await open_db(db_path)
        try:
            row = await get_task(conn2, "task-123")
            assert row is not None
            assert row.task_id == "task-123"
            assert row.thread_id == "999"
            assert row.cwd == "/tmp/test"
            assert row.status == "running"
            assert row.zellij_pane_id == "terminal_1"
            assert row.current_claude_session_id == "sess-abc"
            assert row.current_transcript_path == "/path/transcript"
            assert row.created_at == now
            assert row.last_activity == now
        finally:
            await conn2.close()


@pytest.mark.asyncio
class TestApprovalLog:
    """Tests for approval_log table and operations."""

    async def test_open_db_creates_approval_log_table(self, tmp_path: Path) -> None:
        """open_db creates approval_log table."""
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='approval_log'"
            )
            result = await cursor.fetchone()
            assert result is not None
            assert result[0] == "approval_log"
        finally:
            await conn.close()

    async def test_log_approval_inserts(self, tmp_path: Path) -> None:
        """log_approval inserts an approval row."""
        from bridge.state import upsert_task, log_approval, list_approvals_for_task
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            now = int(time.time())
            await upsert_task(
                conn, "task-123", "999", "/tmp", "running", now=now
            )
            await log_approval(
                conn,
                "req-1",
                "task-123",
                "tool_exec",
                '{"cmd": "ls"}',
                "allow",
                "user approved",
                now=now,
            )

            rows = await list_approvals_for_task(conn, "task-123")
            assert len(rows) == 1
            row = rows[0]
            assert row.request_id == "req-1"
            assert row.task_id == "task-123"
            assert row.tool_name == "tool_exec"
            assert row.tool_input_json == '{"cmd": "ls"}'
            assert row.decision == "allow"
            assert row.decision_reason == "user approved"
            assert row.decided_at == now
        finally:
            await conn.close()

    async def test_log_approval_insert_or_replace(self, tmp_path: Path) -> None:
        """log_approval with duplicate request_id overwrites."""
        from bridge.state import upsert_task, log_approval, list_approvals_for_task
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            now = int(time.time())
            await upsert_task(
                conn, "task-123", "999", "/tmp", "running", now=now
            )
            await log_approval(
                conn,
                "req-1",
                "task-123",
                "tool_exec",
                '{"cmd": "ls"}',
                "allow",
                "first approval",
                now=now,
            )
            await log_approval(
                conn,
                "req-1",
                "task-123",
                "tool_exec",
                '{"cmd": "ls"}',
                "deny",
                "second approval (override)",
                now=now + 10,
            )

            rows = await list_approvals_for_task(conn, "task-123")
            assert len(rows) == 1
            row = rows[0]
            assert row.decision == "deny"
            assert row.decision_reason == "second approval (override)"
            assert row.decided_at == now + 10
        finally:
            await conn.close()

    async def test_list_approvals_for_task_ordering(self, tmp_path: Path) -> None:
        """list_approvals_for_task returns rows in chronological order."""
        from bridge.state import upsert_task, log_approval, list_approvals_for_task
        db_path = tmp_path / "state.db"
        conn = await open_db(db_path)
        try:
            now = int(time.time())
            await upsert_task(
                conn, "task-123", "999", "/tmp", "running", now=now
            )
            await log_approval(
                conn, "req-1", "task-123", "tool_a", "{}", "allow", "a", now=now
            )
            await log_approval(
                conn,
                "req-2",
                "task-123",
                "tool_b",
                "{}",
                "deny",
                "b",
                now=now + 20,
            )
            await log_approval(
                conn,
                "req-3",
                "task-123",
                "tool_c",
                "{}",
                "allow",
                "c",
                now=now + 10,
            )

            rows = await list_approvals_for_task(conn, "task-123")
            assert len(rows) == 3
            # Should be ordered by decided_at ASC
            assert rows[0].request_id == "req-1"
            assert rows[1].request_id == "req-3"
            assert rows[2].request_id == "req-2"
        finally:
            await conn.close()

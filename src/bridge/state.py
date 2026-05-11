import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

DEFAULT_DB_PATH = Path.home() / ".local" / "state" / "cc-bridge" / "state.db"


@dataclass(frozen=True)
class SessionRow:
    """A session row from the database."""

    session_id: str
    cwd: str
    thread_id: str
    created_at: int
    last_activity: int


@dataclass(frozen=True)
class TaskRow:
    """A task row from the database."""

    task_id: str
    thread_id: str
    zellij_pane_id: str | None
    cwd: str
    status: str
    current_claude_session_id: str | None
    current_transcript_path: str | None
    created_at: int
    last_activity: int


@dataclass(frozen=True)
class ApprovalLogRow:
    """An approval log row from the database."""

    request_id: str
    task_id: str
    tool_name: str
    tool_input_json: str
    decision: str
    decision_reason: str
    decided_at: int


async def init_schema(conn: aiosqlite.Connection) -> None:
    """Create the schema (idempotent — uses CREATE TABLE IF NOT EXISTS).
    Public so tests/conftest.py can reuse it without duplicating SQL."""
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            cwd TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            last_activity INTEGER NOT NULL
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            zellij_pane_id TEXT,
            cwd TEXT NOT NULL,
            status TEXT NOT NULL,
            current_claude_session_id TEXT,
            current_transcript_path TEXT,
            created_at INTEGER NOT NULL,
            last_activity INTEGER NOT NULL
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_thread_id ON tasks(thread_id)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_session_id ON tasks(current_claude_session_id)"
    )
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS approval_log (
            request_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            tool_input_json TEXT NOT NULL,
            decision TEXT NOT NULL,
            decision_reason TEXT NOT NULL,
            decided_at INTEGER NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(task_id)
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_approval_log_task_id ON approval_log(task_id)"
    )
    await conn.commit()


async def open_db(path: Path = DEFAULT_DB_PATH) -> aiosqlite.Connection:
    """Open SQLite database, initializing schema if needed.

    Creates parent directories if missing. Sets WAL mode for concurrent access.
    Returns a ready-to-use aiosqlite.Connection.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    await init_schema(conn)
    return conn


async def close_db(conn: aiosqlite.Connection) -> None:
    """Close the database connection."""
    await conn.close()


async def get_session(conn: aiosqlite.Connection, session_id: str) -> SessionRow | None:
    """Retrieve a session row by session_id. Returns None if not found."""
    cursor = await conn.execute(
        "SELECT session_id, cwd, thread_id, created_at, last_activity FROM sessions WHERE session_id = ?",
        (session_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return SessionRow(
        session_id=row[0],
        cwd=row[1],
        thread_id=row[2],
        created_at=row[3],
        last_activity=row[4],
    )


async def upsert_session(
    conn: aiosqlite.Connection,
    session_id: str,
    cwd: str,
    thread_id: str,
    *,
    now: int | None = None,
) -> None:
    """Insert or update a session. Bumps last_activity; preserves created_at on conflict."""
    now_val = now or int(time.time())
    await conn.execute(
        """
        INSERT INTO sessions (session_id, cwd, thread_id, created_at, last_activity)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            thread_id=excluded.thread_id,
            last_activity=excluded.last_activity
        """,
        (session_id, cwd, thread_id, now_val, now_val),
    )
    await conn.commit()


async def delete_session(conn: aiosqlite.Connection, session_id: str) -> None:
    """Delete a session row by session_id."""
    await conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
    await conn.commit()


async def get_task(conn: aiosqlite.Connection, task_id: str) -> TaskRow | None:
    """Retrieve a task row by task_id. Returns None if not found."""
    cursor = await conn.execute(
        "SELECT task_id, thread_id, zellij_pane_id, cwd, status, current_claude_session_id, current_transcript_path, created_at, last_activity FROM tasks WHERE task_id = ?",
        (task_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return TaskRow(
        task_id=row[0],
        thread_id=row[1],
        zellij_pane_id=row[2],
        cwd=row[3],
        status=row[4],
        current_claude_session_id=row[5],
        current_transcript_path=row[6],
        created_at=row[7],
        last_activity=row[8],
    )


async def get_task_by_thread_id(conn: aiosqlite.Connection, thread_id: str) -> TaskRow | None:
    """Retrieve a task row by thread_id. Returns None if not found."""
    cursor = await conn.execute(
        "SELECT task_id, thread_id, zellij_pane_id, cwd, status, current_claude_session_id, current_transcript_path, created_at, last_activity FROM tasks WHERE thread_id = ?",
        (thread_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return TaskRow(
        task_id=row[0],
        thread_id=row[1],
        zellij_pane_id=row[2],
        cwd=row[3],
        status=row[4],
        current_claude_session_id=row[5],
        current_transcript_path=row[6],
        created_at=row[7],
        last_activity=row[8],
    )


async def get_task_by_session_id(conn: aiosqlite.Connection, session_id: str) -> TaskRow | None:
    """Retrieve a task row by current_claude_session_id. Returns None if not found."""
    cursor = await conn.execute(
        "SELECT task_id, thread_id, zellij_pane_id, cwd, status, current_claude_session_id, current_transcript_path, created_at, last_activity FROM tasks WHERE current_claude_session_id = ?",
        (session_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return TaskRow(
        task_id=row[0],
        thread_id=row[1],
        zellij_pane_id=row[2],
        cwd=row[3],
        status=row[4],
        current_claude_session_id=row[5],
        current_transcript_path=row[6],
        created_at=row[7],
        last_activity=row[8],
    )


async def list_active_tasks(conn: aiosqlite.Connection) -> list[TaskRow]:
    """List all active tasks (status 'spawning' or 'running'), ordered by last_activity DESC."""
    cursor = await conn.execute(
        "SELECT task_id, thread_id, zellij_pane_id, cwd, status, current_claude_session_id, current_transcript_path, created_at, last_activity FROM tasks WHERE status IN ('spawning', 'running') ORDER BY last_activity DESC"
    )
    rows = await cursor.fetchall()
    return [
        TaskRow(
            task_id=row[0],
            thread_id=row[1],
            zellij_pane_id=row[2],
            cwd=row[3],
            status=row[4],
            current_claude_session_id=row[5],
            current_transcript_path=row[6],
            created_at=row[7],
            last_activity=row[8],
        )
        for row in rows
    ]


async def upsert_task(
    conn: aiosqlite.Connection,
    task_id: str,
    thread_id: str,
    cwd: str,
    status: str,
    *,
    zellij_pane_id: str | None = None,
    current_claude_session_id: str | None = None,
    current_transcript_path: str | None = None,
    now: int | None = None,
) -> None:
    """Insert or update a task. Bumps last_activity; preserves created_at on conflict."""
    now_val = now or int(time.time())
    await conn.execute(
        """
        INSERT INTO tasks (task_id, thread_id, zellij_pane_id, cwd, status, current_claude_session_id, current_transcript_path, created_at, last_activity)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_id) DO UPDATE SET
            thread_id=excluded.thread_id,
            zellij_pane_id=excluded.zellij_pane_id,
            status=excluded.status,
            current_claude_session_id=excluded.current_claude_session_id,
            current_transcript_path=excluded.current_transcript_path,
            last_activity=excluded.last_activity
        """,
        (task_id, thread_id, zellij_pane_id, cwd, status, current_claude_session_id, current_transcript_path, now_val, now_val),
    )
    await conn.commit()


async def delete_task(conn: aiosqlite.Connection, task_id: str) -> None:
    """Delete a task row by task_id and cascade-delete from approval_log."""
    await conn.execute("DELETE FROM approval_log WHERE task_id = ?", (task_id,))
    await conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
    await conn.commit()


async def log_approval(
    conn: aiosqlite.Connection,
    request_id: str,
    task_id: str,
    tool_name: str,
    tool_input_json: str,
    decision: str,
    decision_reason: str,
    *,
    now: int | None = None,
) -> None:
    """Insert or replace an approval log entry."""
    now_val = now or int(time.time())
    await conn.execute(
        """
        INSERT OR REPLACE INTO approval_log (request_id, task_id, tool_name, tool_input_json, decision, decision_reason, decided_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (request_id, task_id, tool_name, tool_input_json, decision, decision_reason, now_val),
    )
    await conn.commit()


async def list_approvals_for_task(conn: aiosqlite.Connection, task_id: str) -> list[ApprovalLogRow]:
    """List approval log entries for a task, ordered by decided_at ASC."""
    cursor = await conn.execute(
        "SELECT request_id, task_id, tool_name, tool_input_json, decision, decision_reason, decided_at FROM approval_log WHERE task_id = ? ORDER BY decided_at ASC",
        (task_id,),
    )
    rows = await cursor.fetchall()
    return [
        ApprovalLogRow(
            request_id=row[0],
            task_id=row[1],
            tool_name=row[2],
            tool_input_json=row[3],
            decision=row[4],
            decision_reason=row[5],
            decided_at=row[6],
        )
        for row in rows
    ]

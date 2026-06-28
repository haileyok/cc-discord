import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

DEFAULT_DB_PATH = Path.home() / ".local" / "state" / "claude-discord-bridge" / "state.db"


@dataclass(frozen=True)
class SessionRow:
    """A session row from the database."""

    session_id: str
    cwd: str
    thread_id: int
    created_at: int
    last_activity: int


@dataclass(frozen=True)
class TaskRow:
    """A task row from the database.

    A task is one Polytoken daemon session driven from a Discord thread.
    ``polytoken_session_id`` is the daemon session id; ``port`` is its
    loopback HTTP port (runtime-only, captured at spawn and re-discovered via
    ``polytoken sessions`` on reconcile).
    """

    task_id: str
    thread_id: int
    cwd: str
    status: str
    polytoken_session_id: str | None
    port: int | None
    created_at: int
    last_activity: int


@dataclass(frozen=True)
class PinRow:
    """A pinned channel row from the database.

    A pin binds a Discord channel to a cwd. When a message arrives in a pinned
    channel and no live task is bound, the bridge auto-spawns a task in `cwd`
    and binds it to `channel_id`.
    """

    channel_id: int
    cwd: str
    created_at: int
    last_used_at: int


_TASKS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS tasks (
        task_id TEXT PRIMARY KEY,
        thread_id INTEGER NOT NULL,
        cwd TEXT NOT NULL,
        status TEXT NOT NULL,
        polytoken_session_id TEXT,
        port INTEGER,
        created_at INTEGER NOT NULL,
        last_activity INTEGER NOT NULL
    )
"""


async def _migrate_legacy_tasks(conn: aiosqlite.Connection) -> None:
    """Drop the legacy zellij/Claude-Code task schema on upgrade.

    The pre-daemon ``tasks`` table carried ``zellij_pane_id`` /
    ``current_claude_session_id`` / ``current_transcript_path`` columns and a
    companion ``approval_log`` table. The daemon backend can't drive those
    rows (in-flight zellij tasks don't migrate to daemons), and the state is
    disposable personal bookkeeping, so a clean recreate is the chosen path.
    """
    cursor = await conn.execute("PRAGMA table_info(tasks)")
    cols = {row[1] for row in await cursor.fetchall()}
    legacy = {"zellij_pane_id", "current_claude_session_id", "current_transcript_path"}
    if cols & legacy:
        await conn.execute("DROP TABLE IF EXISTS tasks")
    await conn.execute("DROP TABLE IF EXISTS approval_log")


async def init_schema(conn: aiosqlite.Connection) -> None:
    """Create the schema (idempotent — uses CREATE TABLE IF NOT EXISTS).
    Public so tests/conftest.py can reuse it without duplicating SQL."""
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            cwd TEXT NOT NULL,
            thread_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            last_activity INTEGER NOT NULL
        )
    """)
    await _migrate_legacy_tasks(conn)
    await conn.execute(_TASKS_SCHEMA)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_thread_id ON tasks(thread_id)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_session_id ON tasks(polytoken_session_id)"
    )
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS pins (
            channel_id INTEGER PRIMARY KEY,
            cwd TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            last_used_at INTEGER NOT NULL
        )
    """)
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
    thread_id: int,
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


_TASK_COLUMNS = (
    "task_id, thread_id, cwd, status, polytoken_session_id, port, created_at, last_activity"
)


def _task_from_row(row: tuple) -> TaskRow:
    return TaskRow(
        task_id=row[0],
        thread_id=row[1],
        cwd=row[2],
        status=row[3],
        polytoken_session_id=row[4],
        port=row[5],
        created_at=row[6],
        last_activity=row[7],
    )


async def get_task(conn: aiosqlite.Connection, task_id: str) -> TaskRow | None:
    """Retrieve a task row by task_id. Returns None if not found."""
    cursor = await conn.execute(
        f"SELECT {_TASK_COLUMNS} FROM tasks WHERE task_id = ?",
        (task_id,),
    )
    row = await cursor.fetchone()
    return _task_from_row(row) if row is not None else None


async def get_task_by_thread_id(conn: aiosqlite.Connection, thread_id: int) -> TaskRow | None:
    """Retrieve a task row by thread_id. Returns None if not found."""
    cursor = await conn.execute(
        f"SELECT {_TASK_COLUMNS} FROM tasks WHERE thread_id = ?",
        (thread_id,),
    )
    row = await cursor.fetchone()
    return _task_from_row(row) if row is not None else None


async def get_task_by_session_id(conn: aiosqlite.Connection, session_id: str) -> TaskRow | None:
    """Retrieve a task row by polytoken_session_id. Returns None if not found."""
    cursor = await conn.execute(
        f"SELECT {_TASK_COLUMNS} FROM tasks WHERE polytoken_session_id = ?",
        (session_id,),
    )
    row = await cursor.fetchone()
    return _task_from_row(row) if row is not None else None


async def list_active_tasks(conn: aiosqlite.Connection) -> list[TaskRow]:
    """List all active tasks (status 'spawning' or 'running'), ordered by last_activity DESC."""
    cursor = await conn.execute(
        f"SELECT {_TASK_COLUMNS} FROM tasks WHERE status IN ('spawning', 'running') ORDER BY last_activity DESC"
    )
    rows = await cursor.fetchall()
    return [_task_from_row(row) for row in rows]


async def upsert_task(
    conn: aiosqlite.Connection,
    task_id: str,
    thread_id: int,
    cwd: str,
    status: str,
    *,
    polytoken_session_id: str | None = None,
    port: int | None = None,
    now: int | None = None,
) -> None:
    """Insert or update a task. Bumps last_activity; preserves created_at on conflict."""
    now_val = now or int(time.time())
    await conn.execute(
        """
        INSERT INTO tasks (task_id, thread_id, cwd, status, polytoken_session_id, port, created_at, last_activity)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_id) DO UPDATE SET
            thread_id=excluded.thread_id,
            status=excluded.status,
            polytoken_session_id=excluded.polytoken_session_id,
            port=excluded.port,
            last_activity=excluded.last_activity
        """,
        (task_id, thread_id, cwd, status, polytoken_session_id, port, now_val, now_val),
    )
    await conn.commit()


async def delete_task(conn: aiosqlite.Connection, task_id: str) -> None:
    """Delete a task row by task_id."""
    await conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
    await conn.commit()


async def get_pin(conn: aiosqlite.Connection, channel_id: int) -> PinRow | None:
    """Retrieve a pin by channel_id. Returns None if not pinned."""
    cursor = await conn.execute(
        "SELECT channel_id, cwd, created_at, last_used_at FROM pins WHERE channel_id = ?",
        (channel_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return PinRow(channel_id=row[0], cwd=row[1], created_at=row[2], last_used_at=row[3])


async def upsert_pin(
    conn: aiosqlite.Connection,
    channel_id: int,
    cwd: str,
    *,
    now: int | None = None,
) -> None:
    """Insert or update a pin. Bumps last_used_at; preserves created_at on conflict."""
    now_val = now or int(time.time())
    await conn.execute(
        """
        INSERT INTO pins (channel_id, cwd, created_at, last_used_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET
            cwd=excluded.cwd,
            last_used_at=excluded.last_used_at
        """,
        (channel_id, cwd, now_val, now_val),
    )
    await conn.commit()


async def touch_pin(conn: aiosqlite.Connection, channel_id: int, *, now: int | None = None) -> None:
    """Bump last_used_at without changing cwd. No-op if the pin doesn't exist."""
    now_val = now or int(time.time())
    await conn.execute(
        "UPDATE pins SET last_used_at = ? WHERE channel_id = ?",
        (now_val, channel_id),
    )
    await conn.commit()


async def delete_pin(conn: aiosqlite.Connection, channel_id: int) -> bool:
    """Delete a pin. Returns True if a row was deleted, False if no pin existed."""
    cursor = await conn.execute("DELETE FROM pins WHERE channel_id = ?", (channel_id,))
    await conn.commit()
    return cursor.rowcount > 0


async def list_pins(conn: aiosqlite.Connection) -> list[PinRow]:
    """List all pins, ordered by last_used_at DESC."""
    cursor = await conn.execute(
        "SELECT channel_id, cwd, created_at, last_used_at FROM pins ORDER BY last_used_at DESC"
    )
    rows = await cursor.fetchall()
    return [
        PinRow(channel_id=row[0], cwd=row[1], created_at=row[2], last_used_at=row[3])
        for row in rows
    ]

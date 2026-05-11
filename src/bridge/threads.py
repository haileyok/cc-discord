"""ThreadRegistry: manages session_id -> thread_id mapping with 404 recovery."""

import asyncio
from pathlib import Path

import aiosqlite

from bridge import state
from bridge.platform import ChatPlatform


class ThreadRegistry:
    """Owns the session_id -> thread_id lookup and create-on-miss + recreate-on-404 logic.

    Uses a single global asyncio.Lock to guard the create-then-write sequence.
    This simplifies concurrency: we avoid per-session lock overhead (dict cleanup) and
    accept that all get_or_create_thread calls serialize. In practice, requests to the
    same session are rare, so contention is acceptable.
    """

    def __init__(self, bot: ChatPlatform, conn: aiosqlite.Connection) -> None:
        """Initialize the registry with a bot and database connection.

        Args:
            bot: ChatPlatform instance with create_thread() and thread_alive() methods.
            conn: Open aiosqlite.Connection to the sessions table.
        """
        self._bot = bot
        self._conn = conn
        self._lock = asyncio.Lock()

    async def get_or_create_thread(self, session_id: str, cwd: str) -> str:
        """Get or create a thread for a session.

        If session is cached and thread still exists, return the cached thread_id.
        If thread is dead (404), delete the session row and create a new thread.
        If session is not cached, create a new thread.

        Args:
            session_id: Unique session identifier.
            cwd: Current working directory (used only in thread name).

        Returns:
            The thread_id (str) for this session.
        """
        async with self._lock:
            row = await state.get_session(self._conn, session_id)
            if row is not None:
                # Try a cheap reachability check on the cached thread_id.
                if await self._thread_alive(row.thread_id):
                    await state.upsert_session(self._conn, session_id, cwd, row.thread_id)
                    return row.thread_id
                # Thread dead/archived/deleted — drop the row and fall through.
                await state.delete_session(self._conn, session_id)

            # Create new thread
            thread_id = await self._create_thread(session_id, cwd)
            await state.upsert_session(self._conn, session_id, cwd, thread_id)
            return thread_id

    async def _thread_alive(self, thread_id: str) -> bool:
        """Check if a thread still exists.

        Returns:
            True if the thread exists (even if archived).
            False if the thread was deleted (Discord 404).
        """
        return await self._bot.thread_alive(thread_id)

    async def _create_thread(self, session_id: str, cwd: str) -> str:
        """Create a new thread off the configured channel.

        Args:
            session_id: Session ID (used in thread name).
            cwd: Current working directory (used to extract cwd_leaf).

        Returns:
            The new thread's ID.
        """
        cwd_leaf = Path(cwd).name or "root"
        name = f"cc · {cwd_leaf} · {session_id[:8]}"
        return await self._bot.create_thread(name)

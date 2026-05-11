"""Listener for routing Discord messages to pending /v1/ask calls.

Implements sliding-window coalescing for multi-message replies within a grace
period. At most one pending ask per thread; FIFO ordering is enforced by
server.AskLockMap — the lock is acquired before register(), so by the time
register() runs the previous ask has already unregistered.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

GRACE_SECS = 3.0  # default sliding-window grace; overridable for tests


@dataclass
class AskResult:
    """Result of a successful /v1/ask call."""

    reply: str
    replied_at: str  # ISO8601 of the *last* coalesced message


# Anything with the discord.py-Message shape we care about. Tests pass
# small dataclasses; runtime passes real discord.Message objects.
class MessageLike(Protocol):
    """Protocol for message-like objects."""

    author: Any
    channel: Any
    content: str
    attachments: Any
    created_at: datetime


class _PendingAsk:
    """One pending /v1/ask call's listener state. Owns the sliding-window
    coalesce timer."""

    def __init__(self, asked_at: datetime, *, grace_secs: float = GRACE_SECS) -> None:
        loop = asyncio.get_running_loop()
        self.asked_at = asked_at
        self.future: asyncio.Future[AskResult] = loop.create_future()
        self.grace_secs = grace_secs
        self._author_id: int | None = None  # locked on first accepted message
        self._messages: list[MessageLike] = []
        self._coalesce_task: asyncio.Task | None = None

    def accepts(self, msg: MessageLike) -> bool:
        """Filter: post-question, non-bot, same author as first."""
        if msg.author.bot:
            return False
        if msg.created_at <= self.asked_at:
            return False
        if self._author_id is not None and msg.author.id != self._author_id:
            return False
        return True

    def feed(self, msg: MessageLike) -> None:
        """Add a message to the pending ask, resetting the coalesce timer."""
        if not self.accepts(msg):
            return
        if self._author_id is None:
            self._author_id = msg.author.id
        self._messages.append(msg)
        if self._coalesce_task is not None and not self._coalesce_task.done():
            self._coalesce_task.cancel()
        self._coalesce_task = asyncio.create_task(self._resolve_after_grace())

    async def _resolve_after_grace(self) -> None:
        """Wait for grace period, then resolve the future with coalesced messages."""
        try:
            await asyncio.sleep(self.grace_secs)
        except asyncio.CancelledError:
            return
        if self.future.done():
            return
        text_parts = [m.content for m in self._messages if (m.content or "").strip()]
        url_parts = [a.url for m in self._messages for a in (m.attachments or [])]
        body = "\n".join(text_parts)
        if url_parts:
            joined = "\n".join(f"[image] {u}" for u in url_parts)
            body = f"{body}\n{joined}" if body else joined
        replied_at = self._messages[-1].created_at.isoformat()
        self.future.set_result(AskResult(reply=body, replied_at=replied_at))

    def cancel(self) -> None:
        """Cancel the coalesce task (used in finally blocks to prevent leaks)."""
        if self._coalesce_task is not None and not self._coalesce_task.done():
            self._coalesce_task.cancel()


class Listener:
    """Routes incoming Discord messages to pending /v1/ask calls.

    At most one pending ask per thread. FIFO ordering is enforced by
    server.AskLockMap — the lock is acquired before register(), so by the
    time register() runs the previous ask has already unregistered.
    """

    def __init__(self) -> None:
        self._pending: dict[str, _PendingAsk] = {}
        self._lock = asyncio.Lock()  # guards _pending mutation

    async def register(self, thread_id: str, ask: _PendingAsk) -> None:
        """Register an ask for a thread.

        Raises RuntimeError if the thread already has a pending ask
        (AskLockMap should make this impossible).
        """
        async with self._lock:
            if thread_id in self._pending:
                # AskLockMap should make this impossible; raise loudly so the
                # invariant violation doesn't silently corrupt routing.
                raise RuntimeError(f"thread {thread_id} already has a pending ask")
            self._pending[thread_id] = ask

    async def unregister(self, thread_id: str, ask: _PendingAsk) -> None:
        """Unregister an ask from a thread.

        Cancels any pending coalesce task to prevent task leak.
        """
        async with self._lock:
            if self._pending.get(thread_id) is ask:
                del self._pending[thread_id]
        ask.cancel()

    async def deliver(self, msg: MessageLike) -> None:
        """Deliver an incoming message to the pending ask for its thread (if any)."""
        thread_id = msg.channel.id
        async with self._lock:
            ask = self._pending.get(thread_id)
        if ask is None:
            return
        ask.feed(msg)

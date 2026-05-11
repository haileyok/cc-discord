"""ApprovalRouter — bridges PreToolUse hook calls to Discord reaction round-trips.

Holds an in-memory dict[request_id -> Future]. The HTTP handler in server.py registers
a Future, posts the approval prompt in Discord, and awaits the Future with a 600s timeout.
The bot's reaction handler resolves Futures by request_id (looked up via the message_id
the bot just posted).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

from bridge import state
from bridge.platform import ChatPlatform

logger = logging.getLogger(__name__)


DEFAULT_APPROVAL_TIMEOUT = 600.0  # seconds

TUI_DEFAULT_TIMEOUT = 600.0


_NUMERIC_REACTIONS = {
    "1️⃣": 0,
    "2️⃣": 1,
    "3️⃣": 2,
    "4️⃣": 3,
}

_PLAN_REACTIONS = {
    "✅": "1",
    "❌": "2",
}

# Reaction emoji -> decision
_REACTION_DECISIONS = {
    "✅": ("allow", "approved via reaction"),
    "❌": ("deny", "denied via reaction"),
}


@dataclass
class _PendingApproval:
    request_id: str
    task_id: str
    tool_name: str
    tool_input: dict[str, Any]
    thread_id: str
    created_at: int
    future: asyncio.Future  # resolves to (decision: str, reason: str)
    message_id: int | None = None  # Discord message id; set after we post the prompt


@dataclass
class _PendingTuiAnswer:
    request_id: str
    task_id: str
    thread_id: str
    pane_id: str
    kind: str  # "ask_question" | "multi_select" | "exit_plan" | "free_text"
    options: list[str]  # [] for free_text/exit_plan; option labels for ask_question/multi_select
    # Future resolves to (answer, source). `answer` is `str` for ask_question /
    # exit_plan / free_text, and `list[int]` (0-based selected indices) for
    # multi_select. `source` is one of "reaction" | "reply" | "cancelled" |
    # "timeout" | "post_failed".
    future: asyncio.Future
    message_id: int | None = None
    created_at: float = field(default_factory=time.time)


class ApprovalRouter:
    def __init__(
        self,
        bot: ChatPlatform | None,
        conn: aiosqlite.Connection,
        *,
        timeout: float = DEFAULT_APPROVAL_TIMEOUT,
        tui_timeout: float = TUI_DEFAULT_TIMEOUT,
    ) -> None:
        # `bot` may be None at construction time (server.serve constructs the
        # router before the Bot exists, then calls `bind_bot` once it does)
        # — this avoids a chicken-and-egg between ChatPlatform's reaction callback and
        # the router that callback dispatches to.
        self._bot = bot
        self._conn = conn
        self._timeout = timeout
        self._tui_timeout = tui_timeout
        self._by_request_id: dict[str, _PendingApproval] = {}
        self._by_message_id: dict[int, _PendingApproval] = {}
        self._tui_pending: dict[str, _PendingTuiAnswer] = {}
        self._tui_by_message_id: dict[int, _PendingTuiAnswer] = {}
        self._tui_by_thread_id: dict[str, list[_PendingTuiAnswer]] = {}
        self._recently_cancelled_threads: dict[str, float] = {}  # thread_id -> cancel_timestamp
        self._lock = asyncio.Lock()

    def bind_bot(self, bot: ChatPlatform) -> None:
        """Attach the ChatPlatform instance after construction. Called once by
        `server.serve` after the platform is created."""
        self._bot = bot

    async def request_permission(
        self,
        *,
        request_id: str,
        task_id: str,
        thread_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> tuple[str, str]:
        """Post the approval prompt to Discord, wait for resolution.

        Returns (decision, reason). Always returns — never raises (timeout → ('deny', '...')).
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        created_at = int(time.time())
        pending = _PendingApproval(
            request_id=request_id,
            task_id=task_id,
            tool_name=tool_name,
            tool_input=tool_input,
            thread_id=thread_id,
            created_at=created_at,
            future=fut,
        )
        async with self._lock:
            self._by_request_id[request_id] = pending

        body = self._format_prompt(tool_name, tool_input)
        try:
            message_ids = await self._bot.post(body, thread_id=thread_id)
            if not message_ids:
                # Empty response - fail closed
                await self._cleanup(request_id)
                return ("deny", "could not post approval prompt to Discord")
            primary_msg_id = message_ids[0]
            pending.message_id = primary_msg_id
            async with self._lock:
                self._by_message_id[primary_msg_id] = pending
            # Add reactions to the FIRST chunk only (the one users react on).
            try:
                await self._bot.add_reactions(primary_msg_id, thread_id, ["✅", "❌"])
            except Exception:
                logger.exception("failed to add approval reactions")
                await self._cleanup(request_id)
                return ("deny", "failed to add approval reactions (check bot permissions)")
        except Exception:
            logger.exception("failed to post approval prompt")
            await self._cleanup(request_id)
            return ("deny", "failed to post approval prompt")

        try:
            decision, reason = await asyncio.wait_for(fut, timeout=self._timeout)
        except asyncio.TimeoutError:
            decision, reason = ("deny", "approval timed out")
            try:
                await self._bot.post("🛡 ❌ Denied (timeout)", thread_id=thread_id)
            except Exception:
                logger.exception("failed to post timeout notice")
        finally:
            await self._cleanup(request_id)

        # Persist to approval_log
        try:
            await state.log_approval(
                self._conn,
                request_id=request_id,
                task_id=task_id,
                tool_name=tool_name,
                tool_input_json=json.dumps(tool_input, default=str),
                decision=decision,
                decision_reason=reason,
            )
        except Exception:
            logger.exception("failed to log approval decision")

        return (decision, reason)

    async def resolve_by_reaction(self, message_id: int, emoji: str, user_is_self_bot: bool) -> bool:
        """Called by the bot on reaction-add. Returns True if a Future was resolved.

        Filters out reactions added by the bridge's own bot user (user_is_self_bot=True).
        Reactions from other bots in the channel ARE processed.
        """
        if user_is_self_bot:
            return False
        if emoji not in _REACTION_DECISIONS:
            return False
        async with self._lock:
            pending = self._by_message_id.get(message_id)
            if pending is None:
                return False
        if pending.future.done():
            return False
        decision, reason = _REACTION_DECISIONS[emoji]
        pending.future.set_result((decision, reason))
        return True

    async def resolve_by_text(self, thread_id: str, text: str, author_is_bot: bool) -> bool:
        """Called by the bot on a thread message. Resolves any pending approval for this thread
        as deny-with-reason only if the message contains non-whitespace text.

        Empty or whitespace-only messages (e.g., image-only posts) do NOT resolve the approval;
        they fall through to task routing or the listener.

        At most one pending approval per thread at a time is the common case — the design
        implies sequential approvals. When multiple exist, the most recently created one is
        selected.

        Free-text reply order: if the user reacts AND types text, the first to land wins
        (the loser's resolution attempt is dropped because the future is already done).
        """
        if author_is_bot:
            return False
        # Only resolve if the message contains non-whitespace text
        stripped = text.strip()
        if not stripped:
            return False
        async with self._lock:
            # Find all pending approvals for this thread
            candidates = [
                p for p in self._by_request_id.values()
                if p.thread_id == thread_id and not p.future.done()
            ]
        if not candidates:
            return False
        # Most-recent-first: select by created_at
        pending = max(candidates, key=lambda p: p.created_at)
        pending.future.set_result(("deny", stripped))
        return True

    def _format_prompt(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """🛡 Approve `<tool>`? followed by compact tool input dump."""
        body = f"🛡 Approve `{tool_name}`?\n```json\n"
        # Truncate huge tool_input
        s = json.dumps(tool_input, indent=2, default=str)
        if len(s) > 1500:
            s = s[:1500] + "\n  ...(truncated)..."
        body += s + "\n```"
        return body

    async def request_tui_answer(
        self,
        *,
        request_id: str,
        task_id: str,
        thread_id: str,
        pane_id: str,
        kind: str,
        prompt_body: str,
        options: list[str] | None = None,
        timeout: float | None = None,
    ) -> tuple[str, str]:
        """Post the prompt, await user response, return (answer_text_to_inject, source).

        Returns ("", "cancelled") if the user answered in zellij (UserPromptSubmit fired).
        """
        if timeout is None:
            timeout = self._tui_timeout
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        pending = _PendingTuiAnswer(
            request_id=request_id,
            task_id=task_id,
            thread_id=thread_id,
            pane_id=pane_id,
            kind=kind,
            options=options or [],
            future=fut,
        )

        async with self._lock:
            self._tui_pending[request_id] = pending
            self._tui_by_thread_id.setdefault(thread_id, []).append(pending)

            # Check if this thread was recently cancelled (race condition)
            # If cancel_thread_tui ran before we registered, resolve immediately.
            recent_cancel_time = self._recently_cancelled_threads.get(thread_id)
            cancelled_early = (
                recent_cancel_time is not None
                and recent_cancel_time > pending.created_at - 1.0
            )

        if cancelled_early:
            pending.future.set_result(("", "cancelled"))
            await self._cleanup_tui(request_id)
            return ("", "cancelled")

        try:
            message_ids = await self._bot.post(prompt_body, thread_id=thread_id)
            if message_ids:
                pending.message_id = message_ids[0]
                async with self._lock:
                    self._tui_by_message_id[pending.message_id] = pending
                if kind == "ask_question" and options:
                    n = min(len(options), 4)
                    emojis = list(_NUMERIC_REACTIONS.keys())[:n]
                    await self._bot.add_reactions(pending.message_id, thread_id, emojis)
                elif kind == "multi_select" and options:
                    n = min(len(options), 4)
                    emojis = list(_NUMERIC_REACTIONS.keys())[:n] + ["✅"]
                    await self._bot.add_reactions(pending.message_id, thread_id, emojis)
                elif kind == "exit_plan":
                    # Two reactions only. Users who want to leave feedback type a thread
                    # reply directly — text replies always resolve via `resolve_tui_by_text`.
                    await self._bot.add_reactions(pending.message_id, thread_id, ["✅", "❌"])
                # free_text: no reactions
        except Exception:
            logger.exception("failed to post tui prompt")
            await self._cleanup_tui(request_id)
            return ("", "post_failed")

        try:
            answer, source = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            answer, source = ("", "timeout")
            try:
                await self._bot.post("⏱ TUI prompt timed out", thread_id=thread_id)
            except Exception:
                logger.exception("failed to post timeout notice")
        finally:
            await self._cleanup_tui(request_id)

        # Post notice if cancelled (answered in zellij)
        if source == "cancelled":
            try:
                await self._bot.post("(Answered in zellij)", thread_id=thread_id)
            except Exception:
                logger.exception("failed to post cancel notice")

        return (answer, source)

    async def resolve_tui_by_reaction(self, message_id: int, emoji: str, user_is_bot: bool) -> bool:
        if user_is_bot:
            return False
        async with self._lock:
            pending = self._tui_by_message_id.get(message_id)
            if pending is None:
                return False
        if pending.future.done():
            return False

        if pending.kind == "ask_question":
            if emoji not in _NUMERIC_REACTIONS:
                return False
            idx = _NUMERIC_REACTIONS[emoji]
            if idx >= len(pending.options):
                return False
            # Resolve with the option's 1-indexed number — Claude's
            # AskUserQuestion TUI selects on numeric hot-key.
            pending.future.set_result((str(idx + 1), "reaction"))
            return True

        if pending.kind == "exit_plan":
            if emoji not in _PLAN_REACTIONS:
                return False
            answer = _PLAN_REACTIONS[emoji]
            # ExitPlanMode in TUI is resolved via choosing "Yes, proceed" (1) or "No, go back" (2).
            # _PLAN_REACTIONS maps ✅→1 and ❌→2.
            pending.future.set_result((answer, "reaction"))
            return True

        if pending.kind == "multi_select":
            # Numeric reactions toggle freely; we only resolve on the ✅
            # confirm. At submit time we re-fetch the message and read the
            # current set of non-bot reactions to determine what's selected.
            if emoji != "✅":
                return False
            try:
                indices = await self._fetch_selected_indices(pending)
            except Exception:
                logger.exception("failed to fetch multi-select reactions")
                indices = []
            pending.future.set_result((indices, "reaction"))
            return True
        return False

    async def _fetch_selected_indices(self, pending: "_PendingTuiAnswer") -> list[int]:
        """Re-fetch the prompt message and return option indices (0-based)
        that have at least one non-bot reaction. Used by multi_select on ✅.
        """
        if not self._bot or pending.message_id is None:
            return []
        bot_user = self._bot.client.user if self._bot.client else None
        bot_user_id = bot_user.id if bot_user else None
        target = await self._bot.fetch_messageable(pending.thread_id)
        msg = await target.fetch_message(pending.message_id)
        selected: set[int] = set()
        for r in msg.reactions:
            emoji = str(r.emoji)
            idx = _NUMERIC_REACTIONS.get(emoji)
            if idx is None or idx >= len(pending.options):
                continue
            async for user in r.users():
                if user.id != bot_user_id:
                    selected.add(idx)
                    break
        return sorted(selected)

    async def resolve_tui_by_text(self, thread_id: str, text: str, author_is_bot: bool) -> bool:
        if author_is_bot:
            return False
        async with self._lock:
            pendings = self._tui_by_thread_id.get(thread_id, [])
            candidates = [p for p in pendings if not p.future.done()]
        if not candidates:
            return False
        # Most recently created wins
        pending = max(candidates, key=lambda p: p.created_at)

        # multi_select expects list[int] selected indices. Parse comma- /
        # space- / newline-separated digits from the text. If none parse,
        # we still resolve (with an empty list) so the user can type "skip"
        # or similar to advance with no selections — preferable to silently
        # black-holing the message.
        if pending.kind == "multi_select":
            indices: list[int] = []
            for token in re.split(r"[\s,]+", text.strip()):
                if not token:
                    continue
                try:
                    n = int(token)
                except ValueError:
                    continue
                idx = n - 1  # users type 1-based numbers
                if 0 <= idx < len(pending.options) and idx not in indices:
                    indices.append(idx)
            pending.future.set_result((sorted(indices), "reply"))
            return True

        # For exit_plan we always treat text as the comment body.
        # For ask_question we treat text as the typed answer (could be a number or a free-form
        # answer; inject literally — Claude's TUI parses).
        # For free_text we just inject the text.
        pending.future.set_result((text.strip(), "reply"))
        return True

    async def cancel_thread_tui(self, thread_id: str) -> int:
        """Cancel all pending TUI prompts in this thread (used when zellij UserPromptSubmit
        fires while a Discord prompt is still pending). Returns count cancelled.

        Uses sentinel approach: resolves with ("", "cancelled") instead of calling .cancel(),
        so request_tui_answer doesn't raise CancelledError.

        Records the thread_id and timestamp to handle race where cancel_thread_tui is called
        before request_tui_answer registers the pending.
        """
        async with self._lock:
            pendings = self._tui_by_thread_id.get(thread_id, [])
            to_cancel = [p for p in pendings if not p.future.done()]
            now = time.time()
            # Record this cancellation for late-arriving requests, and reap
            # stale entries inline so the dict can't grow without bound when
            # nothing else triggers _cleanup_tui's reaper.
            self._recently_cancelled_threads = {
                tid: ts
                for tid, ts in self._recently_cancelled_threads.items()
                if now - ts < 5.0
            }
            self._recently_cancelled_threads[thread_id] = now
        for p in to_cancel:
            if not p.future.done():
                p.future.set_result(("", "cancelled"))
        return len(to_cancel)

    async def _cleanup_tui(self, request_id: str) -> None:
        async with self._lock:
            pending = self._tui_pending.pop(request_id, None)
            if pending is None:
                return
            if pending.message_id is not None:
                self._tui_by_message_id.pop(pending.message_id, None)
            tlist = self._tui_by_thread_id.get(pending.thread_id, [])
            tlist[:] = [p for p in tlist if p.request_id != request_id]
            if not tlist:
                self._tui_by_thread_id.pop(pending.thread_id, None)

            # Garbage-collect _recently_cancelled_threads entries older than 5s
            now = time.time()
            self._recently_cancelled_threads = {
                tid: ts
                for tid, ts in self._recently_cancelled_threads.items()
                if now - ts < 5.0
            }

    async def _cleanup(self, request_id: str) -> None:
        async with self._lock:
            pending = self._by_request_id.pop(request_id, None)
            if pending is not None and pending.message_id is not None:
                self._by_message_id.pop(pending.message_id, None)

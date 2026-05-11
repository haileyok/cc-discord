# cc-bridge Multiplatform — Phase 6: Mattermost Approvals + Reactions

**Goal:** Wire Mattermost WebSocket `reaction_added` events to the ApprovalRouter and TUI answer system. Map Mattermost emoji names to approval decisions. Text-based approval fallback (already generic from Phase 2).

**Architecture:** The existing `ApprovalRouter` uses `resolve_by_reaction(message_id, emoji, user_is_self_bot)` and `resolve_by_text(thread_id, text, author_is_bot)` — both already work with string IDs after Phase 2. The Mattermost backend wires its WebSocket `reaction_added` events into these methods, converting Mattermost emoji names (e.g., `white_check_mark`) to the Unicode emoji strings the router expects (e.g., `✅`). Text-based replies in threads route to `resolve_by_text` and `resolve_tui_by_text`.

**Tech Stack:** Python 3.12, asyncio (Futures for approval round-trips)

**Scope:** 8 phases from original design (phase 6 of 8)

**Codebase verified:** 2026-05-10

---

## Acceptance Criteria Coverage

This phase implements and tests:

### cc-bridge-multiplatform.AC2: Mattermost Backend Feature Parity (partial)
- **cc-bridge-multiplatform.AC2.2 Success:** Emoji reactions (✅/❌) on approval prompts resolve PreToolUse approvals
- **cc-bridge-multiplatform.AC2.3 Success:** Text replies to approval prompts resolve as denials with the reply text as reason

### cc-bridge-multiplatform.F4: Approval Timeout
- **cc-bridge-multiplatform.F4:** If approval emoji reaction times out (600s), the tool use is denied with a timeout reason

---

<!-- START_SUBCOMPONENT_A (tasks 1-3) -->
<!-- START_TASK_1 -->
### Task 1: Add Mattermost emoji name mapping

**Files:**
- Modify: `src/bridge/backends/mattermost/bot.py`

**Implementation:**

The existing `_emoji_to_mattermost()` function (from Phase 4) converts Unicode → Mattermost names for outbound reactions. Now add the reverse mapping for inbound reactions from WebSocket events:

```python
_MATTERMOST_TO_UNICODE: dict[str, str] = {
    "white_check_mark": "✅",
    "x": "❌",
    "one": "1️⃣",
    "two": "2️⃣",
    "three": "3️⃣",
    "four": "4️⃣",
    "thumbsup": "👍",
    "thumbsdown": "👎",
}


def _mattermost_to_emoji(mm_name: str) -> str:
    """Convert Mattermost emoji name to Unicode emoji for the approval router."""
    return _MATTERMOST_TO_UNICODE.get(mm_name, mm_name)
```

**Testing:**

Tests must verify:
- All mapped emoji names convert correctly in both directions
- Unknown emoji names pass through unchanged

Test file: `tests/backends/mattermost/test_bot.py` (extend)

**Verification:**

```bash
uv run pytest tests/backends/mattermost/test_bot.py -v
```

**Commit:** `feat: add Mattermost emoji name ↔ Unicode mapping`

<!-- END_TASK_1 -->

<!-- START_TASK_2 -->
### Task 2: Wire reaction events to ApprovalRouter

**Verifies:** cc-bridge-multiplatform.AC2.2, cc-bridge-multiplatform.F4

**Files:**
- Modify: `src/bridge/backends/mattermost/bot.py` (update `_handle_event` for reactions)

**Implementation:**

The `on_reaction` callback in `MattermostBot` receives the raw reaction dict from WebSocket. The server's reaction dispatch function (wired in `serve()`) calls `approval_router.resolve_by_reaction()` and `approval_router.resolve_tui_by_reaction()` — same pattern as Discord.

Update the `_handle_event` method's `reaction_added` branch:

```python
elif event == "reaction_added":
    reaction = data.get("reaction", {})
    if reaction.get("user_id") == self._bot_user_id:
        return
    if self._on_reaction:
        # Convert MM emoji name to Unicode for the approval router
        emoji_unicode = _mattermost_to_emoji(reaction.get("emoji_name", ""))
        normalized_reaction = {
            "post_id": reaction.get("post_id"),
            "user_id": reaction.get("user_id"),
            "emoji": emoji_unicode,
        }
        await self._on_reaction(normalized_reaction)
```

The reaction dispatch in `server.py` uses:
- `reaction["post_id"]` as `message_id`
- `reaction["emoji"]` as the emoji string
- `reaction["user_id"] == bot_user_id` for `user_is_self_bot`

The ApprovalRouter's 600s timeout (`DEFAULT_APPROVAL_TIMEOUT`) applies identically — no changes needed in the router itself.

**Testing:**

Tests must verify:
- cc-bridge-multiplatform.AC2.2: white_check_mark reaction resolves approval as "allow"
- cc-bridge-multiplatform.AC2.2: x reaction resolves approval as "deny"
- cc-bridge-multiplatform.F4: Approval future times out after configured seconds and returns deny with timeout reason
- Self-bot reactions are filtered out
- TUI reactions (number emoji) route to resolve_tui_by_reaction

Test file: `tests/backends/mattermost/test_bot.py` (extend)

**Verification:**

```bash
uv run pytest tests/backends/mattermost/test_bot.py -v
```

**Commit:** `feat: wire Mattermost reactions to approval router`

<!-- END_TASK_2 -->

<!-- START_TASK_3 -->
### Task 3: Wire text replies to approval/TUI resolvers

**Verifies:** cc-bridge-multiplatform.AC2.3

**Files:**
- Modify: `src/bridge/backends/mattermost/bot.py` (update message handler for text-based approvals)

**Implementation:**

When a message arrives in a thread that has a pending approval or TUI prompt, it should be routed to the approval router's text-based resolve methods before being treated as a new user message.

Update the `_handle_event` posted branch to check for pending approvals:

```python
if event == "posted":
    post = data.get("post", {})
    # ... existing filters ...

    message = post.get("message", "")
    thread_id = post.get("root_id") or None
    is_reply_in_thread = bool(post.get("root_id"))

    # Check text commands first
    parsed = parse_text_command(message)
    if parsed:
        # ... existing command dispatch ...
        return

    # If reply in a thread, check for pending approval/TUI resolution
    if is_reply_in_thread and self._approval_router:
        resolved = await self._approval_router.resolve_by_text(
            thread_id, message, author_is_bot=False
        )
        if resolved:
            return
        resolved = await self._approval_router.resolve_tui_by_text(
            thread_id, message, author_is_bot=False
        )
        if resolved:
            return

    # Normal message handling
    if self._on_message:
        await self._on_message(post)
```

The `MattermostBot` needs access to the `ApprovalRouter` — add via `bind_approval_router()` or constructor parameter, following the same deferred binding pattern used in the Discord backend.

**Testing:**

Tests must verify:
- cc-bridge-multiplatform.AC2.3: Text reply in thread with pending approval resolves as deny with reply text as reason
- Text reply in thread with pending TUI prompt resolves the prompt
- Text reply in thread with NO pending prompt passes through to on_message

Test file: `tests/backends/mattermost/test_bot.py` (extend)

**Verification:**

```bash
uv run pytest tests/backends/mattermost/test_bot.py -v
```

**Commit:** `feat: wire Mattermost text replies to approval/TUI resolvers`

<!-- END_TASK_3 -->
<!-- END_SUBCOMPONENT_A -->

<!-- START_TASK_4 -->
### Task 4: Full regression verification

**Files:**
- None (verification only)

**Verification:**

```bash
uv run pytest -v
```

Expected: All tests pass.

<!-- END_TASK_4 -->

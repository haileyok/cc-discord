# cc-bridge Multiplatform — Phase 5: Mattermost Commands + Formatting

**Goal:** Implement text command parser (`!start`, `!stop`, etc.), optional slash command HTTP handlers, and markdown table formatting for task lists, stats, and subagent blocks in the Mattermost backend.

**Architecture:** Text commands are parsed from `posted` WebSocket events by a command parser that calls the shared command handlers from Phase 3. Slash commands are optional — when configured, Mattermost POSTs to webhook URLs on the bridge's HTTP server. Formatting replaces Discord embeds with Mattermost-compatible markdown tables. Subagent blocks use edited posts instead of edited embeds.

**Tech Stack:** Python 3.12, aiohttp (server endpoints for slash commands)

**Scope:** 8 phases from original design (phase 5 of 8)

**Codebase verified:** 2026-05-10

---

## Acceptance Criteria Coverage

This phase implements and tests:

### cc-bridge-multiplatform.AC2: Mattermost Backend Feature Parity (partial)
- **cc-bridge-multiplatform.AC2.4 Success:** `!start`, `!stop`, `!kill`, `!list`, `!restart`, `!stats`, `!rename`, `!skill`, `!tasks` text commands work
- **cc-bridge-multiplatform.AC2.5 Success:** Slash commands work when Mattermost is configured with webhook URLs

---

<!-- START_SUBCOMPONENT_A (tasks 1-3) -->
<!-- START_TASK_1 -->
### Task 1: Create Mattermost text command parser

**Verifies:** cc-bridge-multiplatform.AC2.4

**Files:**
- Create: `src/bridge/backends/mattermost/commands.py`

**Implementation:**

Parse `!`-prefixed commands from Mattermost message content. The parser extracts the command name and arguments, then delegates to the shared handlers from `bridge.command_handlers`.

The `on_message` callback in `MattermostBot` receives the raw post dict from WebSocket. The command parser checks if the message starts with `!` and routes accordingly.

```python
from __future__ import annotations

import logging
import shlex
from typing import Any

from bridge.command_handlers import (
    CommandResult,
    handle_start,
    handle_stop,
    handle_kill,
    handle_list,
    handle_restart,
    handle_stats,
    handle_rename,
    handle_skill,
    handle_tasks,
)

logger = logging.getLogger(__name__)

COMMAND_PREFIX = "!"


def parse_text_command(message: str) -> tuple[str, list[str]] | None:
    """Parse a !command from message text. Returns (command_name, args) or None."""
    if not message.startswith(COMMAND_PREFIX):
        return None
    try:
        parts = shlex.split(message[len(COMMAND_PREFIX):])
    except ValueError:
        parts = message[len(COMMAND_PREFIX):].split()
    if not parts:
        return None
    return parts[0].lower(), parts[1:]


async def dispatch_text_command(
    command: str,
    args: list[str],
    registry: Any,
    thread_id: str | None,
) -> CommandResult:
    """Dispatch a parsed text command to the shared handler."""
    if command == "start":
        if not args:
            return CommandResult(success=False, message="Usage: !start <cwd> [prompt]")
        cwd = args[0]
        prompt = " ".join(args[1:]) if len(args) > 1 else None
        return await handle_start(registry, cwd, prompt=prompt)

    elif command == "stop":
        task_id = args[0] if args else None
        return await handle_stop(registry, thread_id=thread_id, task_id=task_id)

    elif command == "kill":
        task_id = args[0] if args else None
        return await handle_kill(registry, thread_id=thread_id, task_id=task_id)

    elif command == "list":
        return await handle_list(registry)

    elif command == "restart":
        task_id = args[0] if args else None
        return await handle_restart(registry, thread_id=thread_id, task_id=task_id)

    elif command == "stats":
        task_id = args[0] if args else None
        return await handle_stats(registry, thread_id=thread_id, task_id=task_id)

    elif command == "rename":
        name = " ".join(args) if args else None
        return await handle_rename(registry, thread_id=thread_id, name=name)

    elif command == "skill":
        if not args:
            return CommandResult(success=False, message="Usage: !skill <name> [args]")
        skill_name = args[0]
        skill_args = " ".join(args[1:]) if len(args) > 1 else None
        if not thread_id:
            return CommandResult(success=False, message="!skill must be used in a task thread")
        return await handle_skill(registry, thread_id, skill_name, args=skill_args)

    elif command == "tasks":
        task_id = args[0] if args else None
        return await handle_tasks(registry, thread_id=thread_id, task_id=task_id)

    else:
        return CommandResult(success=False, message=f"Unknown command: !{command}")
```

**Testing:**

Tests must verify:
- cc-bridge-multiplatform.AC2.4: Each text command parses correctly and dispatches to the right handler
- `parse_text_command` handles edge cases: empty commands, quoted args, no prefix

Test file: `tests/backends/mattermost/test_commands.py`

**Verification:**

```bash
uv run pytest tests/backends/mattermost/test_commands.py -v
```

**Commit:** `feat: add Mattermost text command parser`

<!-- END_TASK_1 -->

<!-- START_TASK_2 -->
### Task 2: Add slash command HTTP handlers

**Verifies:** cc-bridge-multiplatform.AC2.5

**Files:**
- Modify: `src/bridge/backends/mattermost/commands.py` (add HTTP handler functions)
- Modify: `src/bridge/server.py` (register routes when platform is Mattermost)

**Implementation:**

Mattermost slash commands POST to configured URLs on the bridge. Each handler receives a form-encoded body with `command`, `text`, `channel_id`, `user_id`, etc., and returns JSON.

Add HTTP handler functions to `commands.py`:

```python
from aiohttp import web


async def slash_handler(
    request: web.Request,
    command: str,
    registry: Any,
) -> web.Response:
    """Generic slash command HTTP handler for Mattermost."""
    data = await request.post()
    text = data.get("text", "")
    channel_id = data.get("channel_id", "")
    args = shlex.split(text) if text else []

    # thread_id context: Mattermost doesn't pass thread context in slash commands
    # Commands that need thread context resolve via channel_id + active task lookup
    result = await dispatch_text_command(command, args, registry, thread_id=None)

    return web.json_response({
        "text": result.message,
        "response_type": "ephemeral" if not result.success else "in_channel",
    })
```

Register routes in `server.py` when the Mattermost backend is active:

```python
# Only when BRIDGE_PLATFORM=mattermost
for cmd in ["start", "stop", "kill", "list", "restart", "stats", "rename", "skill", "tasks"]:
    app.router.add_post(
        f"/v1/mattermost/slash/{cmd}",
        lambda req, c=cmd: slash_handler(req, c, task_registry),
    )
```

**Testing:**

Tests must verify:
- cc-bridge-multiplatform.AC2.5: Each slash endpoint returns correct JSON response format
- Response includes `response_type` field (ephemeral for errors, in_channel for success)

Test file: `tests/backends/mattermost/test_commands.py` (extend)

**Verification:**

```bash
uv run pytest tests/backends/mattermost/test_commands.py -v
```

**Commit:** `feat: add Mattermost slash command HTTP handlers`

<!-- END_TASK_2 -->

<!-- START_TASK_3 -->
### Task 3: Wire text commands into MattermostBot message handler

**Files:**
- Modify: `src/bridge/backends/mattermost/bot.py`

**Implementation:**

Update the `_handle_event` method to check for text commands before passing to the generic message handler:

```python
async def _handle_event(self, event: str, data: dict) -> None:
    if event == "posted":
        post = data.get("post", {})
        if post.get("user_id") == self._bot_user_id:
            return
        if self._allowed_user_ids and post.get("user_id") not in self._allowed_user_ids:
            return
        if post.get("channel_id") != self._channel_id:
            return

        message = post.get("message", "")
        parsed = parse_text_command(message)
        if parsed:
            command, args = parsed
            thread_id = post.get("root_id") or post.get("id")
            result = await dispatch_text_command(
                command, args, self._registry, thread_id
            )
            await self.post(result.message, thread_id=thread_id)
            return

        if self._on_message:
            await self._on_message(post)
```

The `MattermostBot` constructor needs a `registry` parameter (or it receives it via `bind_registry`), similar to how the Discord backend receives the TaskRegistry.

**Testing:**

Tests must verify:
- Text command messages are intercepted and dispatched
- Non-command messages pass through to `on_message`
- Command results are posted back to the thread

Test file: `tests/backends/mattermost/test_bot.py` (extend)

**Verification:**

```bash
uv run pytest tests/backends/mattermost/test_bot.py -v
```

**Commit:** `feat: wire text commands into MattermostBot message handler`

<!-- END_TASK_3 -->
<!-- END_SUBCOMPONENT_A -->

<!-- START_SUBCOMPONENT_B (tasks 4-5) -->
<!-- START_TASK_4 -->
### Task 4: Create Mattermost formatting module

**Files:**
- Create: `src/bridge/backends/mattermost/formatting.py`

**Implementation:**

Replace Discord embed rendering with Mattermost markdown. The existing code uses three embed patterns that need Mattermost equivalents:

1. **Task list** (`_render_task_list_embed` in tasks.py): Status marks + task details
2. **Subagent blocks** (`_render_subagent_embed` in tasks.py): Per-agent live-edited cards
3. **Stats** (`usage.format_summary`): Already text-based, no change needed

```python
from __future__ import annotations

from typing import Any


def format_task_list(tasks: list[Any]) -> str:
    """Format task list as Mattermost markdown table."""
    if not tasks:
        return "No active tasks."

    lines = ["| Status | Task | CWD | Age |", "|--------|------|-----|-----|"]
    for task in tasks:
        status_mark = {
            "running": "▶️",
            "spawning": "🔄",
            "stopped": "⏹",
            "crashed": "💥",
            "archived": "📦",
        }.get(task.status, "❓")
        lines.append(
            f"| {status_mark} | `{task.task_id[:8]}` | {task.cwd_leaf} | {task.age} |"
        )
    return "\n".join(lines)


def format_subagent_block(
    attribution: str,
    last_actions: list[str],
    total_actions: int,
    finished: bool,
    duration_str: str,
) -> str:
    """Format subagent block as Mattermost markdown."""
    status = "finished" if finished else "running"
    status_emoji = "🟢" if finished else "🟡"

    actions_text = "\n".join(f"• {a}" for a in last_actions[-5:])
    if len(actions_text) > 3500:
        actions_text = actions_text[:3500] + "\n…(truncated)"

    return (
        f"**🤖 {attribution}**\n"
        f"{actions_text}\n"
        f"_{status_emoji} {status} · {total_actions} actions · {duration_str}_"
    )


def format_tool_diff(tool_name: str, diff_text: str) -> str:
    """Format a tool diff block for Mattermost (same markdown as Discord)."""
    # Mattermost supports the same fenced code blocks
    return diff_text  # tool_summary.diff_block already produces markdown


def format_task_todos(todos: list[dict]) -> str:
    """Format TodoWrite as Mattermost checklist."""
    lines = []
    for todo in todos:
        status = todo.get("status", "pending")
        mark = {"completed": "✅", "in_progress": "▶️", "pending": "⬜", "deleted": "🗑"}.get(status, "⬜")
        lines.append(f"{mark} {todo.get('content', '')}")
    return "\n".join(lines)
```

**Testing:**

Tests must verify:
- Task list renders as a markdown table with correct columns
- Subagent block renders with attribution, actions, and status footer
- Empty task list returns "No active tasks."
- Long action lists truncate at 3500 chars

Test file: `tests/backends/mattermost/test_formatting.py`

**Verification:**

```bash
uv run pytest tests/backends/mattermost/test_formatting.py -v
```

**Commit:** `feat: add Mattermost markdown formatting module`

<!-- END_TASK_4 -->

<!-- START_TASK_5 -->
### Task 5: Create MattermostRichFormatter implementing RichFormatter protocol

**Files:**
- Create: `src/bridge/backends/mattermost/rich_formatter.py`

**Implementation:**

In Phase 2, a `RichFormatter` protocol was defined in `platform.py` and `tasks.py` was refactored to call `self._formatter.post_rich(...)` / `self._formatter.edit_rich(...)` instead of Discord-specific embed methods. The Discord backend has `DiscordRichFormatter` (created in Phase 2).

Now create the Mattermost equivalent that renders the same data as markdown:

```python
from bridge.backends.mattermost.formatting import (
    format_subagent_block,
    format_task_list,
    format_task_todos,
)


class MattermostRichFormatter:
    """RichFormatter implementation using Mattermost markdown."""

    def __init__(self, bot: MattermostBot) -> None:
        self._bot = bot

    async def post_rich(self, thread_id: str, block_type: str, data: dict) -> str:
        if block_type == "subagent_block":
            text = format_subagent_block(
                data["attribution"], data["actions"],
                data["total_actions"], data["finished"], data["duration"],
            )
        elif block_type == "task_list":
            text = format_task_list(data["tasks"])
        elif block_type == "todo_list":
            text = format_task_todos(data["todos"])
        else:
            text = str(data)

        msg_ids = await self._bot.post(text, thread_id=thread_id)
        return msg_ids[0]

    async def edit_rich(
        self, thread_id: str, message_id: str, block_type: str, data: dict
    ) -> None:
        if block_type == "subagent_block":
            text = format_subagent_block(
                data["attribution"], data["actions"],
                data["total_actions"], data["finished"], data["duration"],
            )
        else:
            text = str(data)

        await self._bot.edit_message(thread_id, message_id, content=text)
```

Wire `MattermostRichFormatter` into `TaskRegistry.bind_formatter()` in `server.py` when `BRIDGE_PLATFORM=mattermost`.

**Testing:**

Tests must verify:
- MattermostRichFormatter.post_rich() calls bot.post() with formatted markdown
- MattermostRichFormatter.edit_rich() calls bot.edit_message() with updated content
- Each block_type renders correctly

Test file: `tests/backends/mattermost/test_formatting.py` (extend)

**Verification:**

```bash
uv run pytest tests/backends/mattermost/test_formatting.py -v
```

**Commit:** `feat: create MattermostRichFormatter for rich content rendering`

<!-- END_TASK_5 -->
<!-- END_SUBCOMPONENT_B -->

<!-- START_TASK_6 -->
### Task 6: Full regression verification

**Files:**
- None (verification only)

**Verification:**

```bash
uv run pytest -v
```

Expected: All tests pass.

<!-- END_TASK_6 -->

# cc-bridge Multiplatform — Phase 3: Command Handler Extraction

**Goal:** Separate command business logic from Discord slash command registration so that both Discord slash commands and Mattermost text/slash commands can share the same handlers.

**Architecture:** The existing `src/bridge/commands.py` already contains all 9 slash commands as thin wrappers calling `TaskRegistry` methods. This phase extracts the business logic into platform-agnostic handler functions that return result objects, then moves the Discord-specific `Interaction`/`followup`/`Embed` plumbing into `backends/discord/commands.py`. The shared handlers take plain parameters and return plain results.

**Tech Stack:** Python 3.12, discord.py app_commands

**Scope:** 8 phases from original design (phase 3 of 8)

**Codebase verified:** 2026-05-10

---

## Acceptance Criteria Coverage

This phase implements and tests:

### cc-bridge-multiplatform.AC5: Discord Regression-Free (partial)
- **cc-bridge-multiplatform.AC5.3 Success:** Slash commands sync and function correctly

---

<!-- START_SUBCOMPONENT_A (tasks 1-3) -->
<!-- START_TASK_1 -->
### Task 1: Create shared command handler module

**Files:**
- Create: `src/bridge/command_handlers.py`

**Implementation:**

Create platform-agnostic command handlers. Each handler takes plain typed parameters and returns a result dataclass. No Discord types, no Interaction objects.

The investigation confirmed 9 commands, each calling TaskRegistry methods directly. The shared handlers extract the business logic:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class CommandResult:
    """Result from a command handler, consumed by platform-specific formatters."""
    success: bool
    message: str
    task: Any | None = None
    tasks: list[Any] | None = None
    embed_data: dict | None = None


async def handle_start(
    registry: "TaskRegistry",
    cwd: str,
    prompt: str | None = None,
) -> CommandResult:
    ...

async def handle_stop(
    registry: "TaskRegistry",
    thread_id: str | None = None,
    task_id: str | None = None,
) -> CommandResult:
    ...

async def handle_kill(
    registry: "TaskRegistry",
    thread_id: str | None = None,
    task_id: str | None = None,
) -> CommandResult:
    ...

async def handle_list(registry: "TaskRegistry") -> CommandResult:
    ...

async def handle_restart(
    registry: "TaskRegistry",
    thread_id: str | None = None,
    task_id: str | None = None,
) -> CommandResult:
    ...

async def handle_stats(
    registry: "TaskRegistry",
    thread_id: str | None = None,
    task_id: str | None = None,
) -> CommandResult:
    ...

async def handle_rename(
    registry: "TaskRegistry",
    thread_id: str | None = None,
    name: str | None = None,
) -> CommandResult:
    ...

async def handle_skill(
    registry: "TaskRegistry",
    thread_id: str,
    skill_name: str,
    args: str | None = None,
) -> CommandResult:
    ...

async def handle_tasks(
    registry: "TaskRegistry",
    thread_id: str | None = None,
    task_id: str | None = None,
) -> CommandResult:
    ...
```

Each handler contains the business logic currently in the corresponding slash command function — the TaskRegistry calls, error handling, result formatting. The `CommandResult` carries enough data for any platform to render the response.

Include the existing helper functions that aren't Discord-specific:
- `_humanize_age(epoch: int) -> str` (currently at commands.py:304-313)
- `_wait_for_session_bind(registry, task_id, *, timeout)` (currently at commands.py:316-329)

The `_resolve_task` helper (commands.py:288-301) is Discord-specific because it takes `discord.Interaction` and `discord.Thread`. The shared equivalent should take `thread_id: str | None` and `task_id: str | None` instead.

**Verification:**

```bash
uv run python -c "from bridge.command_handlers import handle_start, CommandResult; print('OK')"
```

**Commit:** `feat: create platform-agnostic command handlers`

<!-- END_TASK_1 -->

<!-- START_TASK_2 -->
### Task 2: Move Discord slash commands to backends/discord/commands.py

**Files:**
- Move: `src/bridge/commands.py` → `src/bridge/backends/discord/commands.py`
- Modify: `src/bridge/server.py` (update import path)

**Implementation:**

Move the existing commands module into the Discord backend:

```bash
git mv src/bridge/commands.py src/bridge/backends/discord/commands.py
```

Update the moved file to:
1. Import shared handlers from `bridge.command_handlers`
2. Keep the `build_tree()` function and all Discord-specific plumbing (`Interaction`, `defer`, `followup`, `Embed`)
3. Each slash command becomes a thin wrapper: parse Discord-specific params → call shared handler → format result using Discord patterns (ephemeral followup, embed, etc.)

Example transformation for `/start`:

```python
# Before (business logic inline):
@tree.command(name="start", ...)
async def start(interaction: discord.Interaction, cwd: str, prompt: str | None = None):
    await interaction.response.defer(ephemeral=True)
    task = await registry.spawn_task(cwd, prompt=prompt)
    await interaction.followup.send(f"Started task {task.task_id}")

# After (delegates to shared handler):
@tree.command(name="start", ...)
async def start(interaction: discord.Interaction, cwd: str, prompt: str | None = None):
    await interaction.response.defer(ephemeral=True)
    result = await handle_start(registry, cwd, prompt=prompt)
    await interaction.followup.send(result.message)
```

Update `src/bridge/server.py` import:
```python
# Before:
from bridge.commands import build_tree
# After:
from bridge.backends.discord.commands import build_tree
```

Also update `src/bridge/backends/discord/__init__.py` to export the commands module.

**Verification:**

```bash
uv run python -c "from bridge.backends.discord.commands import build_tree; print('OK')"
```

**Commit:** `refactor: move Discord slash commands to backends/discord/`

<!-- END_TASK_2 -->

<!-- START_TASK_3 -->
### Task 3: Update command tests

**Verifies:** cc-bridge-multiplatform.AC5.3

**Files:**
- Modify: `tests/test_commands.py` (update imports, add shared handler tests)

**Implementation:**

Update imports in the existing test file:
```python
# Update import path
from bridge.backends.discord.commands import build_tree
from bridge.command_handlers import handle_start, handle_stop, ...
```

Add tests for the shared command handlers:
- Test each `handle_*` function with a `FakePlatform` and mock `TaskRegistry`
- Verify they return appropriate `CommandResult` objects
- Existing Discord-specific tests (FakeInteraction pattern) continue to test the slash command wrappers

**Testing:**

Tests must verify:
- cc-bridge-multiplatform.AC5.3: Slash commands still function through the wrapper → shared handler pipeline

**Verification:**

```bash
uv run pytest tests/test_commands.py -v
```

**Commit:** `test: update command tests for extracted handlers`

<!-- END_TASK_3 -->
<!-- END_SUBCOMPONENT_A -->

<!-- START_TASK_4 -->
### Task 4: Full regression verification

**Files:**
- None (verification only)

**Verification:**

```bash
# Verify no discord imports in shared handler module
grep -n "import discord" src/bridge/command_handlers.py

# Full test suite
uv run pytest -v
```

Expected: No discord imports in command_handlers.py. All tests pass.

<!-- END_TASK_4 -->

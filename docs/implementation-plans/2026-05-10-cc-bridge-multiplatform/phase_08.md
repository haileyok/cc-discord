# cc-bridge Multiplatform — Phase 8: Integration Testing + Documentation

**Goal:** Add end-to-end integration tests with FakePlatform, update all documentation to reflect the cc-bridge multiplatform architecture, and update deployment templates.

**Architecture:** Integration tests verify the full request pipeline (HTTP hook → TaskRegistry → ChatPlatform → response) using FakePlatform. Documentation updates cover README, CLAUDE.md, deployment templates, and a migration guide for existing cc-discord users.

**Tech Stack:** Python 3.12, pytest, aiohttp test_utils

**Scope:** 8 phases from original design (phase 8 of 8)

**Codebase verified:** 2026-05-10

---

## Acceptance Criteria Coverage

This phase implements and tests:

### cc-bridge-multiplatform.AC5: Discord Regression-Free
- **cc-bridge-multiplatform.AC5.2 Success:** Discord bot connects, receives messages, streams responses, handles approvals — identical behaviour to pre-refactor

### cc-bridge-multiplatform.AC1: Platform Abstraction (integration verification)
- **cc-bridge-multiplatform.AC1.3 Success:** A `FakePlatform` implementation passes all core tests without any real backend

---

<!-- START_SUBCOMPONENT_A (tasks 1-2) -->
<!-- START_TASK_1 -->
### Task 1: Add multiplatform integration tests

**Verifies:** cc-bridge-multiplatform.AC1.3, cc-bridge-multiplatform.AC5.2

**Files:**
- Create: `tests/test_tasks_multiplatform.py`
- Create: `tests/test_approvals_multiplatform.py`

**Implementation:**

Create integration tests that exercise core logic through FakePlatform, verifying the full pipeline works without any real backend.

`tests/test_tasks_multiplatform.py`:
- Task lifecycle with FakePlatform: spawn → running → stop
- Hook event dispatch posts to FakePlatform
- Transcript streaming posts to FakePlatform
- startup reconciliation with FakePlatform

`tests/test_approvals_multiplatform.py`:
- PreToolUse approval round-trip with FakePlatform
- Reaction-based approval (resolve_by_reaction with string IDs)
- Text-based denial (resolve_by_text)
- TUI prompts (AskUserQuestion, ExitPlanMode) with FakePlatform
- Timeout handling

These tests prove the platform abstraction works end-to-end without Discord or Mattermost.

Follow the existing test patterns:
- `@pytest.mark.asyncio` on test classes
- `in_memory_db` fixture for state
- `FakePlatform` from `tests/fakes.py`
- `FakeZellij` from `tests/fakes.py`

**Testing:**

Tests must verify:
- cc-bridge-multiplatform.AC1.3: All core operations work through FakePlatform
- cc-bridge-multiplatform.AC5.2: The same test logic that passes with FakePlatform also passes with FakeBot (Discord), proving regression-free behaviour

**Verification:**

```bash
uv run pytest tests/test_tasks_multiplatform.py tests/test_approvals_multiplatform.py -v
```

**Commit:** `test: add multiplatform integration tests with FakePlatform`

<!-- END_TASK_1 -->

<!-- START_TASK_2 -->
### Task 2: Add platform protocol conformance test

**Files:**
- Create: `tests/test_platform.py` (if not already created in Phase 2)

**Implementation:**

Verify that both `DiscordBot` and `MattermostBot` satisfy the `ChatPlatform` protocol:

```python
from bridge.platform import ChatPlatform
from bridge.backends.discord.bot import DiscordBot
from bridge.backends.mattermost.bot import MattermostBot
from tests.fakes import FakePlatform


def test_discord_bot_satisfies_protocol() -> None:
    """DiscordBot structurally conforms to ChatPlatform."""
    # Structural check — verify all protocol methods exist with compatible signatures
    assert _has_protocol_methods(DiscordBot)


def test_mattermost_bot_satisfies_protocol() -> None:
    """MattermostBot structurally conforms to ChatPlatform."""
    assert _has_protocol_methods(MattermostBot)


def test_fake_platform_satisfies_protocol() -> None:
    """FakePlatform structurally conforms to ChatPlatform."""
    assert _has_protocol_methods(FakePlatform)


def _has_protocol_methods(cls: type) -> bool:
    """Check that cls has all ChatPlatform protocol methods."""
    import inspect
    protocol_methods = [
        name for name, _ in inspect.getmembers(ChatPlatform, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]
    for method in protocol_methods:
        if not hasattr(cls, method):
            return False
    return True
```

**Verification:**

```bash
uv run pytest tests/test_platform.py -v
```

**Commit:** `test: add ChatPlatform protocol conformance checks`

<!-- END_TASK_2 -->
<!-- END_SUBCOMPONENT_A -->

<!-- START_SUBCOMPONENT_B (tasks 3-5) -->
<!-- START_TASK_3 -->
### Task 3: Update README.md for multiplatform

**Files:**
- Modify: `README.md`

**Implementation:**

Major restructure of README to cover both platforms:

1. **Title**: `# cc-bridge` (was `# claude-discord-bridge`)
2. **What it does**: Update to describe multiplatform support
3. **Prereqs**: Add Mattermost prerequisites alongside Discord
4. **Setup**: Split into Discord setup and Mattermost setup subsections
   - Discord: mostly unchanged content
   - Mattermost: server URL, bot token, channel ID, allowed users
5. **Usage**: Add Mattermost text commands (`!start`, `!stop`, etc.)
6. **Environment variables**: Add `BRIDGE_PLATFORM`, Mattermost-specific vars
7. **Architecture**: Update diagram to show platform abstraction layer
8. **Deployment**: Update service names, add Mattermost examples

**Verification:**

Review README renders correctly as markdown.

**Commit:** `docs: update README for multiplatform cc-bridge`

<!-- END_TASK_3 -->

<!-- START_TASK_4 -->
### Task 4: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

**Implementation:**

Update project documentation to reflect the new architecture:

1. **Title and description**: `cc-bridge` — multiplatform Claude Code bridge
2. **Architecture**: Describe ChatPlatform protocol, backends/ structure
3. **Path references**: All updated to `cc-bridge` (done in Phase 1, verify complete)
4. **New gotchas**: Mattermost WebSocket double-encoding, emoji mapping
5. **Backend-specific notes**: Which code is platform-specific vs shared
6. **Freshness date**: Update to current date

**Verification:**

```bash
grep -n "claude-discord-bridge" CLAUDE.md
```

Expected: No matches (except possibly in migration context).

**Commit:** `docs: update CLAUDE.md for multiplatform architecture`

<!-- END_TASK_4 -->

<!-- START_TASK_5 -->
### Task 5: Update deployment templates

**Files:**
- Modify: `packaging/cc-bridge.service` (add BRIDGE_PLATFORM env)
- Modify: `scripts/install-systemd-user.sh`

**Implementation:**

Update the systemd service to include `BRIDGE_PLATFORM`:

```ini
[Service]
Type=simple
Environment=BRIDGE_PLATFORM=discord
ExecStart=%h/.local/bin/cc-bridge serve
Restart=on-failure
RestartSec=5s
```

Add a LaunchAgent plist template at `packaging/local.cc-bridge.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>local.cc-bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/USERNAME/.local/bin/cc-bridge</string>
        <string>serve</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>BRIDGE_PLATFORM</key>
        <string>discord</string>
    </dict>
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/cc-bridge.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/cc-bridge.err</string>
</dict>
</plist>
```

**Verification:**

```bash
ls packaging/cc-bridge.service packaging/local.cc-bridge.plist
grep BRIDGE_PLATFORM packaging/cc-bridge.service
```

**Commit:** `docs: update deployment templates for cc-bridge with BRIDGE_PLATFORM`

<!-- END_TASK_5 -->
<!-- END_SUBCOMPONENT_B -->

<!-- START_TASK_6 -->
### Task 6: Create migration guide

**Files:**
- Create: `docs/MIGRATION.md`

**Implementation:**

Create a migration guide for existing `claude-discord-bridge` users covering:

1. **Package rename**: `claude-discord-bridge` → `cc-bridge` (CLI command changes)
2. **Config path migration**: `~/.config/claude-discord-bridge/` → `~/.config/cc-bridge/`
   - Backward compat: old path still works with deprecation warning
   - Manual: `mv ~/.config/claude-discord-bridge ~/.config/cc-bridge`
3. **State path migration**: `~/.local/state/claude-discord-bridge/` → `~/.local/state/cc-bridge/`
   - Manual: `mv ~/.local/state/claude-discord-bridge ~/.local/state/cc-bridge`
4. **Environment variables**: `CC_DISCORD_TASK_ID` → `CC_BRIDGE_TASK_ID` (both accepted during transition)
5. **Zellij session name**: Default changed from `meow` to `cc-bridge-worker`
   - Users with `BRIDGE_ZELLIJ_SESSION=meow` explicitly set are unaffected
   - Others: kill old session and let the new default create a new one
6. **New required env var**: `BRIDGE_PLATFORM=discord` (or `mattermost`)
7. **Systemd/launchd**: Update unit names and add `BRIDGE_PLATFORM` env
   - `claude-discord-bridge.service` → `cc-bridge.service`
   - `local.claude-discord-bridge.plist` → `local.cc-bridge.plist`
8. **Hook scripts**: Same paths, but now accept both old and new env vars

**Verification:**

Review the migration guide for completeness.

**Commit:** `docs: add migration guide for existing cc-discord users`

<!-- END_TASK_6 -->

<!-- START_TASK_7 -->
### Task 7: Update skills

**Files:**
- Modify: `skills/SKILL.md`
- Create: `skills/ask-bridge/` symlink (or update existing)

**Implementation:**

Update `skills/SKILL.md` to reference `cc-bridge` instead of `claude-discord-bridge`. Add an `ask-bridge` alias alongside the existing `ask-discord` skill for backward compatibility.

**Verification:**

```bash
grep "cc-bridge" skills/SKILL.md
```

**Commit:** `docs: update skills for cc-bridge`

<!-- END_TASK_6 -->

<!-- START_TASK_8 -->
### Task 8: Final full verification

**Files:**
- None (verification only)

**Verification:**

```bash
# Full test suite
uv run pytest -v

# Verify no stale references
grep -rn "claude-discord-bridge" src/ hooks/ tests/ packaging/ scripts/

# Verify platform protocol conformance
uv run pytest tests/test_platform.py -v

# Verify multiplatform integration tests
uv run pytest tests/test_tasks_multiplatform.py tests/test_approvals_multiplatform.py -v
```

Expected: All tests pass. No stale `claude-discord-bridge` references in source code.

<!-- END_TASK_8 -->

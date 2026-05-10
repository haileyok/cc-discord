# cc-bridge Multiplatform Design

## Summary
Refactor cc-discord into cc-bridge: a multi-platform Claude Code bridge supporting Discord and Mattermost as selectable backends. Introduces a `ChatPlatform` protocol for common operations (post, thread, file, reaction) with backend modules for platform-specific concerns (slash commands, rich formatting, connection lifecycle). The Mattermost backend provides full feature parity including transcript streaming, emoji+text approvals, bidirectional file attachments, and both text and slash commands. Project is renamed from `claude-discord-bridge` to `cc-bridge` across package name, CLI, config paths, and deployment units. Runtime selects one platform via `BRIDGE_PLATFORM` env var.

## Definition of Done
Refactor cc-discord into cc-bridge — a multi-platform Claude Code bridge that supports both Discord and Mattermost as selectable backends via configuration. The existing Discord functionality must remain fully intact.

**Success criteria:**
- Platform abstraction layer that cleanly separates chat-platform concerns from core bridge logic (hooks, tasks, zellij, transcripts)
- Mattermost backend with feature parity: message relay, transcript streaming, emoji + text approvals, slash commands + text commands, bidirectional file attachments
- One-at-a-time runtime — config selects Discord or Mattermost, not both simultaneously
- Project renamed from cc-discord to cc-bridge (repo, package name, CLI entrypoint, systemd/launchd units)
- Existing Discord functionality unchanged — no regressions

**Out of scope:**
- Simultaneous multi-platform in a single daemon
- Porting the TypeScript claude-mattermost-zellij project's viewport-scraping approach
- New features beyond platform parity (no new commands, no new hook types)

## Acceptance Criteria

### AC1: Platform Abstraction Layer
- **cc-bridge-multiplatform.AC1.1**: Core modules (`tasks.py`, `threads.py`, `approvals.py`, `server.py`) reference only `ChatPlatform` protocol, never `discord.*` or mattermost-specific types
- **cc-bridge-multiplatform.AC1.2**: `thread_id` is `str` everywhere (code, SQLite schema, hook env vars)
- **cc-bridge-multiplatform.AC1.3**: A `FakePlatform` implementation passes all core tests without any real backend

### AC2: Mattermost Backend Feature Parity
- **cc-bridge-multiplatform.AC2.1**: Messages posted in a Mattermost channel are relayed to a zellij pane and Claude Code responses stream back to the thread
- **cc-bridge-multiplatform.AC2.2**: Emoji reactions (✅/❌) on approval prompts resolve PreToolUse approvals
- **cc-bridge-multiplatform.AC2.3**: Text replies to approval prompts resolve as denials with the reply text as reason
- **cc-bridge-multiplatform.AC2.4**: `!start`, `!stop`, `!kill`, `!list`, `!restart`, `!stats`, `!rename`, `!skill`, `!tasks` text commands work
- **cc-bridge-multiplatform.AC2.5**: Slash commands work when Mattermost is configured with webhook URLs
- **cc-bridge-multiplatform.AC2.6**: Files attached to Mattermost messages are saved locally and paths relayed to Claude
- **cc-bridge-multiplatform.AC2.7**: `[[attach: /path]]` markers in Claude output trigger file upload to Mattermost thread

### AC3: One-at-a-Time Runtime
- **cc-bridge-multiplatform.AC3.1**: `BRIDGE_PLATFORM=discord` starts Discord backend only; Mattermost config is not required
- **cc-bridge-multiplatform.AC3.2**: `BRIDGE_PLATFORM=mattermost` starts Mattermost backend only; Discord config is not required
- **cc-bridge-multiplatform.AC3.3**: Missing or invalid `BRIDGE_PLATFORM` produces a clear error message

### AC4: Project Rename
- **cc-bridge-multiplatform.AC4.1**: Package installs as `cc-bridge` CLI command
- **cc-bridge-multiplatform.AC4.2**: Secrets load from `~/.config/cc-bridge/secrets.json` (with fallback to old path + deprecation warning)
- **cc-bridge-multiplatform.AC4.3**: State stored in `~/.local/state/cc-bridge/`
- **cc-bridge-multiplatform.AC4.4**: Hook scripts use `CC_BRIDGE_TASK_ID` env var (accept `CC_DISCORD_TASK_ID` as fallback)

### AC5: Discord Regression-Free
- **cc-bridge-multiplatform.AC5.1**: All existing test files pass (relocated but unchanged logic)
- **cc-bridge-multiplatform.AC5.2**: Discord bot connects, receives messages, streams responses, handles approvals — identical behaviour to pre-refactor
- **cc-bridge-multiplatform.AC5.3**: Slash commands sync and function correctly

### Failure Cases
- **cc-bridge-multiplatform.F1**: If Mattermost WebSocket disconnects mid-task, the task continues running and output is posted after reconnection
- **cc-bridge-multiplatform.F2**: If a file upload to Mattermost fails (too large, permission denied), the text portion of the response still posts with an error note
- **cc-bridge-multiplatform.F3**: If `BRIDGE_PLATFORM` is set to an unknown value, daemon exits with a clear error (not a stack trace)
- **cc-bridge-multiplatform.F4**: If approval emoji reaction times out (600s), the tool use is denied with a timeout reason

## Glossary

- **ChatPlatform**: Python Protocol class defining the minimal interface that Discord and Mattermost backends must implement (post, thread, file, reaction operations).
- **Backend**: A platform-specific module (`backends/discord/`, `backends/mattermost/`) implementing `ChatPlatform` plus platform-specific concerns like slash commands and rich formatting.
- **Hook**: Claude Code lifecycle event (SessionStart, PostToolUse, Stop, etc.) delivered via HTTP POST from a hook script running inside the Claude Code process to the bridge's `/v1/hook/event` endpoint.
- **Transcript JSONL**: Claude Code's internal session log at `~/.claude/projects/.../<session_id>.jsonl`. Contains structured entries (text, thinking, tool_use) that the bridge reads for output streaming.
- **Task**: A single Claude Code session running in a zellij tab, bound to a chat thread. Lifecycle: spawning → running → stopped/crashed/archived.
- **Thread ID**: Platform-agnostic string identifying a conversation thread. Discord: stringified thread integer. Mattermost: root post ID (26-char alphanumeric).
- **Approval**: PreToolUse round-trip where the bridge posts a permission prompt to the chat thread and waits for the user to approve/deny via emoji reaction or text reply.
- **Text command**: A message prefixed with `!` (e.g., `!start`, `!stop`) parsed by the bridge as a command. Available on all platforms without server-side configuration.
- **Slash command**: Platform-native command interface. Discord: `app_commands.CommandTree`. Mattermost: webhook-based slash commands requiring admin configuration.

---

## Architecture

### Hybrid Abstraction Model

The refactor introduces a thin `ChatPlatform` protocol for operations that are genuinely identical across platforms, with backend modules for platform-specific concerns.

```
                     ┌─────────────────────────────────────────┐
                     │              cc-bridge daemon            │
                     │                                         │
  ┌────────────┐     │  ┌─────────────────────────────────┐    │
  │ Claude Code │────▶│  │  Core (platform-agnostic)       │    │
  │  (hooks)    │     │  │  ├─ TaskRegistry                │    │
  │             │     │  │  ├─ ZellijManager               │    │
  └────────────┘     │  │  ├─ TranscriptReader             │    │
                     │  │  ├─ ApprovalRouter (text path)   │    │
  ┌────────────┐     │  │  ├─ CommandHandlers              │    │
  │   zellij    │◀──▶│  │  └─ State (SQLite)               │    │
  │  (panes)    │     │  └─────────────┬───────────────────┘    │
  └────────────┘     │                 │ ChatPlatform protocol  │
                     │       ┌─────────┴─────────┐              │
                     │       ▼                   ▼              │
                     │  ┌──────────┐      ┌─────────────┐      │
                     │  │ Discord  │      │ Mattermost  │      │
                     │  │ Backend  │      │  Backend    │      │
                     │  └──────────┘      └─────────────┘      │
                     │                                         │
                     │  ┌─────────────────────────────────┐    │
                     │  │  HTTP Server (aiohttp)           │    │
                     │  │  /v1/hook/event, /v1/notify, etc │    │
                     │  └─────────────────────────────────┘    │
                     └─────────────────────────────────────────┘
```

### ChatPlatform Protocol

Minimal protocol covering operations that are structurally identical across Discord and Mattermost:

```python
class ChatPlatform(Protocol):
    @property
    def is_ready(self) -> bool: ...

    async def start(self) -> None: ...
    async def close(self) -> None: ...

    async def post(self, message: str, thread_id: str | None = None) -> list[str]: ...
    async def post_with_attachments(
        self, file_paths: list[Path], thread_id: str | None = None, text: str | None = None
    ) -> list[str]: ...
    async def create_thread(self, name: str) -> str: ...
    async def archive_thread(self, thread_id: str) -> None: ...
    async def thread_alive(self, thread_id: str) -> bool: ...
    async def download_attachment(self, attachment_ref: Any, dest_dir: Path) -> Path: ...
    async def add_reactions(self, message_id: str, thread_id: str, emoji: list[str]) -> None: ...
    async def edit_message(self, thread_id: str, message_id: str, content: str | None = None) -> None: ...
```

Key design decisions:
- `thread_id` and `message_id` are `str` everywhere (Discord casts `int` → `str`)
- Return types are `list[str]` for message IDs (chunked posts return multiple)
- `attachment_ref` is `Any` — each backend defines its own attachment type
- `post()` handles chunking internally (platform-specific limits)

### What stays in backend modules

| Concern | Why platform-specific |
|---------|----------------------|
| Reaction event handling | Discord: gateway events. Mattermost: WebSocket `reaction_added` |
| Slash command registration | Discord: `app_commands.CommandTree`. Mattermost: webhook URLs |
| Text command parsing | Shared pattern but different trigger syntax |
| Rich formatting (embeds/cards) | Discord: `discord.Embed`. Mattermost: markdown tables or attachments |
| Connection lifecycle | Discord: intents, gateway. Mattermost: token auth, REST+WS |
| Thread naming/renaming | Discord: `rename_thread()`. Mattermost: no equivalent (root post can't be renamed) |

### Existing Patterns Preserved

The following patterns from cc-discord remain unchanged:

1. **Hook-driven output**: Claude Code hooks fire HTTP events to `/v1/hook/event`. The bridge reads transcript JSONL for structured output. No viewport scraping.
2. **Single asyncio event loop**: aiohttp server + chat platform client share one loop.
3. **Task lifecycle**: `spawning → running → stopped/crashed/archived` state machine persisted in SQLite.
4. **Pane-per-task model**: Each task gets one zellij tab. Input via `write-chars`/bracketed paste.
5. **Task-scoped hook settings**: Generated per-task JSON at `~/.local/state/cc-bridge/task-settings/<task_id>.json`.
6. **Approval round-trips**: PreToolUse hooks for `AskUserQuestion`/`ExitPlanMode` with timeout.
7. **Transcript streaming**: Walk JSONL entries since last user prompt, dedup by uuid, emit text+thinking blocks.

---

## Mattermost Backend Design

### Client Architecture

The Mattermost backend wraps the Mattermost REST API v4 and WebSocket API. Two implementation options:

1. **matteraio** (if viable): Pure async Mattermost client for Python 3.12+. Needs verification during implementation.
2. **Raw aiohttp** (fallback): Thin wrapper (~200 LOC) against REST API v4 + `websockets` library for real-time events. The API is simple REST — this is a viable and dependency-light approach.

The implementation plan should verify `matteraio` availability first. If unavailable or immature, use raw aiohttp. The Mattermost REST API is well-documented and stable.

### Thread Semantics

| Discord | Mattermost | Mapping |
|---------|------------|---------|
| `create_thread(name)` → thread ID (int) | `create_post(channel_id, message)` → post ID (str) | "Thread" = root post. Replies use `root_id`. |
| `thread_alive(thread_id)` → bool | `get_post(post_id)` → 200/404 | Same pattern, different API call |
| `archive_thread(thread_id)` | No-op | Mattermost threads don't archive |
| `rename_thread(thread_id, name)` | No equivalent | Skip or edit root post content |
| Thread has numeric ID | Post has 26-char alphanumeric ID | All IDs stored as `str` |

Thread creation flow for Mattermost:
1. `/start` (or `!start`) triggers `spawn_task()`
2. Backend posts a root message: `"🟢 cc-bridge task: {cwd_leaf} ({task_id[:8]})"`
3. Root post's `id` becomes the `thread_id`
4. All subsequent output posts use `root_id = thread_id`

### Reaction-Based Approvals

Mattermost WebSocket events include `reaction_added`:
```json
{
  "event": "reaction_added",
  "data": {
    "reaction": "{\"user_id\":\"...\",\"post_id\":\"...\",\"emoji_name\":\"white_check_mark\",...}"
  }
}
```

Mapping:
- Discord `✅` → Mattermost emoji_name `white_check_mark`
- Discord `❌` → Mattermost emoji_name `x`
- Discord `1️⃣`..`4️⃣` → Mattermost emoji_name `one`..`four`

The Mattermost backend's reaction handler parses WebSocket events and calls `approval_router.resolve_by_reaction()` with normalized emoji names.

### Command Interface

**Text commands** (parsed from message content):
- `!start <cwd> [prompt]` — spawn task
- `!stop [task_id]` — graceful stop
- `!kill [task_id]` — force kill
- `!list` — list active tasks
- `!restart [task_id]` — resume task
- `!stats [task_id]` — token/cost stats
- `!rename [name]` — rename (edits root post)
- `!skill <name> [args]` — invoke skill
- `!tasks [task_id]` — show task list

**Slash commands** (optional, requires Mattermost admin config):
- Mattermost slash commands POST to a configured URL (e.g., `/v1/mattermost/slash/start`)
- Bridge registers an HTTP handler for each command
- Response format: JSON `{"text": "...", "response_type": "ephemeral|in_channel"}`
- Admin must create the slash command in Mattermost pointing to the bridge URL

Both interfaces call the same shared command handlers.

### File Attachments

**Inbound (Mattermost → Claude):**
1. Mattermost posts with files have `file_ids` array in the post JSON
2. WebSocket `posted` event includes file metadata
3. Backend downloads via `GET /api/v4/files/{file_id}` with auth header
4. Saves to `~/.local/state/cc-bridge/attachments/<task_id>/`
5. Appends absolute paths to relayed pane input

**Outbound (Claude → Mattermost):**
1. Transcript streaming detects `[[attach: /path]]` markers (existing logic)
2. Backend uploads via `POST /api/v4/files` (multipart form)
3. Response includes `file_id`
4. Backend creates post with `file_ids` array

### Message Formatting

| Discord | Mattermost |
|---------|------------|
| `discord.Embed` (rich cards) | Markdown tables or `| props |` attachments |
| Max chunk: 1900 chars | Max chunk: 3500 chars (soft), 16383 (hard) |
| Code blocks: same markdown | Code blocks: same markdown |
| Thread link: `discord.com/channels/...` | Post link: `{server}/team/pl/{post_id}` |
| Ephemeral messages | No equivalent — post then delete, or DM |

For rich formatting (task lists, subagent blocks, stats), the Mattermost backend uses markdown tables instead of Discord embeds. The content is the same; the rendering differs.

---

## Project Rename

### Scope of Rename

| Artifact | Old | New |
|----------|-----|-----|
| Package name (pyproject.toml) | `claude-discord-bridge` | `claude-code-bridge` |
| CLI entrypoint | `claude-discord-bridge` | `cc-bridge` |
| Source package | `src/bridge/` | `src/bridge/` (unchanged) |
| Secrets dir | `~/.config/claude-discord-bridge/` | `~/.config/cc-bridge/` |
| State dir | `~/.local/state/claude-discord-bridge/` | `~/.local/state/cc-bridge/` |
| Zellij session | `meow` (configurable) | `cc-bridge-worker` (configurable) |
| Systemd unit | `claude-discord-bridge.service` | `cc-bridge.service` |
| LaunchAgent plist | `local.claude-discord-bridge.plist` | `local.cc-bridge.plist` |
| Env var prefix | `CC_DISCORD_*` | `CC_BRIDGE_*` |
| Hook env var | `CC_DISCORD_TASK_ID` | `CC_BRIDGE_TASK_ID` |

Migration notes:
- The `init` and `doctor` commands detect old paths and offer migration
- Backward-compatible secret loading: check new path first, fall back to old path with deprecation warning
- Hook env vars: accept both `CC_DISCORD_TASK_ID` and `CC_BRIDGE_TASK_ID` during transition

---

## Source Layout (Post-Refactor)

```
src/bridge/
  __init__.py
  cli.py                    # CLI entrypoint (platform selection)
  server.py                 # HTTP server (platform-agnostic dispatch)
  platform.py               # ChatPlatform protocol definition
  tasks.py                  # TaskRegistry (uses ChatPlatform protocol)
  threads.py                # ThreadRegistry (uses ChatPlatform protocol)
  approvals.py              # ApprovalRouter (text path generic, reaction path delegated)
  commands.py                # Shared command handlers (business logic only)
  state.py                  # SQLite schema (thread_id → TEXT)
  transcript.py             # Transcript JSONL reader (unchanged)
  zellij.py                 # Zellij CLI wrapper (unchanged)
  listener.py               # /v1/ask coalescing (unchanged)
  secrets.py                # Secrets I/O (path updated)

  backends/
    __init__.py
    discord/
      __init__.py
      bot.py                # DiscordBot(ChatPlatform) — wraps discord.py
      commands.py           # Discord slash command registration
      formatting.py         # Embed rendering for task lists, stats, etc.
    mattermost/
      __init__.py
      bot.py                # MattermostBot(ChatPlatform) — wraps REST+WS
      commands.py           # Text command parser + slash command HTTP handlers
      formatting.py         # Markdown table rendering for task lists, stats, etc.

hooks/
  event.py                  # Hook event POST (env var rename, otherwise unchanged)
  pretooluse-approve.py     # PreToolUse hook (unchanged)
  notify-stop.py            # Stop hook (unchanged)
  notify-notification.py    # Notification hook (unchanged)

skills/
  ask-discord/SKILL.md      # Keep for backward compat, add ask-bridge/ alias
```

---

## Configuration

### Platform Selection

```
BRIDGE_PLATFORM=discord|mattermost    # Required. Selects backend.
```

### Shared Config

```
BRIDGE_URL=http://127.0.0.1:8787     # HTTP server bind (existing)
BRIDGE_ZELLIJ_SESSION=cc-bridge-worker  # Zellij session name
BRIDGE_ATTACHMENT_TTL_SECS=604800     # Attachment cleanup TTL
BRIDGE_SECRETS_PATH=~/.config/cc-bridge/secrets.json  # Override secrets location
```

### Discord-Specific (only when BRIDGE_PLATFORM=discord)

```
# In secrets.json:
{
  "bot_token": "...",
  "channel_id": 123456789
}

# Environment:
BRIDGE_NOTIFY_USER_ID=...            # Discord user ID for @-mentions
```

### Mattermost-Specific (only when BRIDGE_PLATFORM=mattermost)

```
# In secrets.json:
{
  "bot_token": "...",
  "server_url": "https://mattermost.example.com",
  "channel_id": "abc123def456...",
  "allowed_user_ids": ["user1", "user2"]
}

# Environment:
BRIDGE_MATTERMOST_SCHEME=https       # Default: https
```

### CLI Changes

```bash
# Init now asks which platform
cc-bridge init --platform discord
cc-bridge init --platform mattermost

# Serve reads BRIDGE_PLATFORM
cc-bridge serve

# Doctor checks platform-specific config
cc-bridge doctor
```

---

## Testing Strategy

### Existing Tests (Must Pass)

All 20 existing test files continue to pass. Discord-specific tests move to `tests/backends/discord/` but their logic is unchanged.

### New Tests

| Test File | Purpose |
|-----------|---------|
| `tests/test_platform.py` | Protocol conformance: verify both backends satisfy `ChatPlatform` |
| `tests/backends/mattermost/test_bot.py` | MattermostBot: posting, chunking, thread creation, reactions, file upload |
| `tests/backends/mattermost/test_commands.py` | Text command parsing + slash command HTTP handlers |
| `tests/backends/mattermost/test_formatting.py` | Markdown table rendering for task lists, stats |
| `tests/test_tasks_multiplatform.py` | TaskRegistry with FakePlatform (generic, not Discord-specific) |
| `tests/test_approvals_multiplatform.py` | Approval flows with FakePlatform |

### Test Infrastructure

- `tests/fakes.py` gains `FakePlatform(ChatPlatform)` — in-memory implementation for testing core logic without any real backend
- Existing `FakeBot` becomes `FakeDiscordBot` in `tests/backends/discord/fakes.py`
- New `FakeMattermostBot` in `tests/backends/mattermost/fakes.py`

---

## Implementation Phases

### Phase 1: Project Rename
Rename package, CLI, paths, env vars. No functional changes. All existing tests pass with new names.

### Phase 2: ChatPlatform Protocol + Discord Extraction
Define `platform.py` with the `ChatPlatform` protocol. Extract Discord-specific code from `bot.py` into `backends/discord/bot.py`. Adapt `tasks.py`, `threads.py`, `approvals.py`, `server.py` to use the protocol instead of the concrete `Bot` class. Change `thread_id` from `int` to `str` throughout (including SQLite schema migration). All existing tests pass.

### Phase 3: Command Handler Extraction
Separate command business logic from Discord slash command registration. Create `commands.py` (shared handlers) and `backends/discord/commands.py` (slash command wiring). Existing command tests adapted.

### Phase 4: Mattermost Client
Implement `backends/mattermost/bot.py` with `MattermostBot(ChatPlatform)`. REST API wrapper for posting, threads, files, reactions. WebSocket client for real-time events (posted, reaction_added). Unit tests with mocked HTTP.

### Phase 5: Mattermost Commands + Formatting
Implement text command parser (`!start`, `!stop`, etc.) and optional slash command HTTP handlers. Implement markdown table formatting for task lists, stats, subagent blocks. Tests for parsing and rendering.

### Phase 6: Mattermost Approvals + Reactions
Wire Mattermost WebSocket `reaction_added` events to approval router. Map Mattermost emoji names to approval decisions. Text-based approval fallback (already generic). Tests for approval flows.

### Phase 7: CLI + Init + Doctor Updates
Update `cc-bridge init` to support `--platform mattermost` with Mattermost-specific setup wizard. Update `cc-bridge doctor` with Mattermost-specific health checks. Update `cc-bridge serve` to instantiate selected backend.

### Phase 8: Integration Testing + Documentation
End-to-end tests with FakePlatform. Update README, CLAUDE.md, deployment docs. Update systemd/launchd unit templates. Migration guide for existing cc-discord users.

---

## Additional Considerations

### Mattermost WebSocket Reliability

Mattermost's WebSocket connection can drop. The backend must:
- Reconnect automatically with backoff
- Re-subscribe to events after reconnection
- Not lose in-flight approval prompts (they have 600s timeout; reconnection is faster)

The existing `@mattermost/client` in claude-mattermost-zellij handles reconnection. The Python implementation should follow the same pattern.

### SQLite Schema Migration

Phase 2 changes `thread_id INTEGER` → `thread_id TEXT`. Migration strategy:
- Add new column `thread_id_text TEXT`
- Copy data: `UPDATE sessions SET thread_id_text = CAST(thread_id AS TEXT)`
- Drop old column, rename new (or just recreate table — SQLite doesn't support DROP COLUMN before 3.35)
- Version the schema with a `schema_version` table

### Backward Compatibility

During transition:
- Accept both old (`CC_DISCORD_TASK_ID`) and new (`CC_BRIDGE_TASK_ID`) env vars in hooks
- Check both old and new secrets/state paths
- Emit deprecation warnings for old paths
- Remove backward compat in a future release (not in scope for this design)

### Rate Limiting

Discord has aggressive rate limits (50 requests/second per route). Mattermost's are configurable server-side and generally more lenient. The Mattermost backend should still implement retry-with-backoff for 429 responses, but can use simpler logic than Discord's.

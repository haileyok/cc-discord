# cc-bridge Multiplatform — Phase 2: ChatPlatform Protocol + Discord Extraction

**Goal:** Define a `ChatPlatform` Protocol class and extract the Discord bot into `backends/discord/`, adapting all core modules to reference the protocol. Migrate `thread_id` from `int` to `str` throughout.

**Architecture:** Introduce `src/bridge/platform.py` with the `ChatPlatform` Protocol. Move `bot.py` to `backends/discord/bot.py` as `DiscordBot` implementing the protocol (wrapping discord.py). Core modules (`tasks.py`, `threads.py`, `approvals.py`, `server.py`) switch from concrete `Bot` import to `ChatPlatform` protocol. SQLite schema migrates `thread_id INTEGER` → `thread_id TEXT`. FakePlatform test double replaces FakeBot for core tests.

**Tech Stack:** Python 3.12, typing.Protocol, discord.py, aiosqlite

**Scope:** 8 phases from original design (phase 2 of 8)

**Codebase verified:** 2026-05-10

---

## Acceptance Criteria Coverage

This phase implements and tests:

### cc-bridge-multiplatform.AC1: Platform Abstraction Layer
- **cc-bridge-multiplatform.AC1.1 Success:** Core modules (`tasks.py`, `threads.py`, `approvals.py`, `server.py`) reference only `ChatPlatform` protocol, never `discord.*` or mattermost-specific types
- **cc-bridge-multiplatform.AC1.2 Success:** `thread_id` is `str` everywhere (code, SQLite schema, hook env vars)
- **cc-bridge-multiplatform.AC1.3 Success:** A `FakePlatform` implementation passes all core tests without any real backend

### cc-bridge-multiplatform.AC5: Discord Regression-Free (partial)
- **cc-bridge-multiplatform.AC5.1 Success:** All existing test files pass (relocated but unchanged logic)

---

<!-- START_SUBCOMPONENT_A (tasks 1-2) -->
<!-- START_TASK_1 -->
### Task 1: Create ChatPlatform protocol definition

**Files:**
- Create: `src/bridge/platform.py`

**Implementation:**

Define the `ChatPlatform` Protocol based on the intersection of bot methods used by core modules. Investigation confirmed these methods are called from tasks.py, threads.py, approvals.py, and server.py:

```python
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class ChatPlatform(Protocol):
    @property
    def is_ready(self) -> bool: ...

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def post(
        self, message: str, *, thread_id: str | None = None
    ) -> list[str]: ...

    async def post_with_attachments(
        self,
        file_paths: list[Path],
        *,
        thread_id: str | None = None,
        text: str | None = None,
    ) -> list[str]: ...

    async def create_thread(self, name: str) -> str: ...

    async def archive_thread(self, thread_id: str) -> None: ...

    async def rename_thread(self, thread_id: str, name: str) -> None: ...

    async def thread_alive(self, thread_id: str) -> bool: ...

    async def download_attachment(
        self, attachment_ref: Any, dest_dir: Path
    ) -> Path: ...

    async def add_reactions(
        self, message_id: str, thread_id: str, emoji: list[str]
    ) -> None: ...

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        *,
        content: str | None = None,
    ) -> None: ...

    async def fetch_messageable(self, thread_id: str) -> Any: ...
```

Key decisions:
- All IDs are `str` (Discord backend converts `int` ↔ `str` at its boundary)
- `post()` and `post_with_attachments()` return `list[str]` (message IDs from chunked posts)
- `edit_message()` drops the `embed` parameter — embeds are Discord-specific. `DiscordBot` keeps `embed` as an extra `**kwargs` parameter beyond the protocol, and `tasks.py` uses a platform-specific `post_rich()` callback (see below) for embed-like rendering. The protocol's `edit_message()` only handles text content updates.
- `fetch_messageable()` returns `Any` — the return type is platform-specific
- `download_attachment()` takes `Any` for the attachment reference — each backend defines its own attachment type

**Embed/rich rendering strategy:**

`tasks.py` currently calls `self._bot.post_embed(embed, ...)` and `self._bot.edit_message(..., embed=...)` for subagent blocks and task lists. To keep `tasks.py` backend-agnostic:

1. Define a `RichFormatter` callback protocol in `platform.py`:
   ```python
   class RichFormatter(Protocol):
       async def post_rich(self, thread_id: str, block_type: str, data: dict) -> str: ...
       async def edit_rich(self, thread_id: str, message_id: str, block_type: str, data: dict) -> None: ...
   ```
2. Each backend provides its own `RichFormatter` implementation:
   - Discord: renders `discord.Embed` objects
   - Mattermost: renders markdown tables/blocks
3. `TaskRegistry` receives a `RichFormatter` via `bind_formatter()` alongside `bind_bot()`
4. All embed calls in `tasks.py` switch to `self._formatter.post_rich(...)` / `self._formatter.edit_rich(...)`
5. `DiscordBot.edit_message()` only handles text. The Discord `RichFormatter` calls discord-specific `edit_message` with embed kwargs internally.

This approach is implemented in Phase 2 Task 6 (core module refactoring) and the backends provide their formatters in Phase 5 (Mattermost) and Phase 2 Task 5 (Discord).

**Verification:**

```bash
uv run python -c "from bridge.platform import ChatPlatform; print('OK')"
```

**Commit:** `feat: add ChatPlatform protocol definition`

<!-- END_TASK_1 -->

<!-- START_TASK_2 -->
### Task 2: Create FakePlatform test double

**Verifies:** cc-bridge-multiplatform.AC1.3

**Files:**
- Modify: `tests/fakes.py` (add FakePlatform alongside existing FakeBot)

**Implementation:**

Add a `FakePlatform` class that implements the `ChatPlatform` protocol using in-memory tracking, similar to the existing `FakeBot` pattern but with `str` IDs:

```python
@dataclass
class FakePlatform:
    is_ready: bool = True
    _post_calls: list[dict] = field(default_factory=list)
    _thread_calls: list[dict] = field(default_factory=list)
    _archive_calls: list[dict] = field(default_factory=list)
    _reaction_calls: list[dict] = field(default_factory=list)
    _edit_calls: list[dict] = field(default_factory=list)
    _download_calls: list[dict] = field(default_factory=list)
    _attachment_calls: list[dict] = field(default_factory=list)
    _thread_counter: int = 0
    _message_counter: int = 0

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def post(
        self, message: str, *, thread_id: str | None = None
    ) -> list[str]:
        self._message_counter += 1
        msg_id = str(1000 + self._message_counter)
        self._post_calls.append(
            {"content": message, "thread_id": thread_id, "msg_id": msg_id}
        )
        return [msg_id]

    async def post_with_attachments(
        self,
        file_paths: list[Path],
        *,
        thread_id: str | None = None,
        text: str | None = None,
    ) -> list[str]:
        self._message_counter += 1
        msg_id = str(1000 + self._message_counter)
        self._attachment_calls.append(
            {"file_paths": file_paths, "thread_id": thread_id, "text": text}
        )
        return [msg_id]

    async def create_thread(self, name: str) -> str:
        self._thread_counter += 1
        tid = str(2000 + self._thread_counter)
        self._thread_calls.append({"name": name, "thread_id": tid})
        return tid

    async def archive_thread(self, thread_id: str) -> None:
        self._archive_calls.append({"thread_id": thread_id})

    async def rename_thread(self, thread_id: str, name: str) -> None:
        pass

    async def thread_alive(self, thread_id: str) -> bool:
        return True

    async def download_attachment(
        self, attachment_ref: Any, dest_dir: Path
    ) -> Path:
        self._download_calls.append(
            {"ref": attachment_ref, "dest_dir": dest_dir}
        )
        return dest_dir / "fake_download.txt"

    async def add_reactions(
        self, message_id: str, thread_id: str, emoji: list[str]
    ) -> None:
        self._reaction_calls.append(
            {"message_id": message_id, "thread_id": thread_id, "emoji": emoji}
        )

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        *,
        content: str | None = None,
    ) -> None:
        self._edit_calls.append(
            {"thread_id": thread_id, "message_id": message_id, "content": content}
        )

    async def fetch_messageable(self, thread_id: str) -> Any:
        return FakeBotChannel()
```

Keep the existing `FakeBot` intact — Discord-specific tests still need it.

**Testing:**

Tests must verify:
- cc-bridge-multiplatform.AC1.3: FakePlatform satisfies `ChatPlatform` protocol at type-check time (runtime_checkable or structural check)

Create `tests/test_platform.py` with a protocol conformance test.

**Verification:**

```bash
uv run pytest tests/test_platform.py -v
```

**Commit:** `feat: add FakePlatform test double implementing ChatPlatform`

<!-- END_TASK_2 -->
<!-- END_SUBCOMPONENT_A -->

<!-- START_SUBCOMPONENT_B (tasks 3-4) -->
<!-- START_TASK_3 -->
### Task 3: SQLite schema migration — thread_id INTEGER → TEXT

**Verifies:** cc-bridge-multiplatform.AC1.2

**Files:**
- Modify: `src/bridge/state.py`

**Implementation:**

The current schema has `thread_id INTEGER` in both `sessions` and `tasks` tables. Migrate to `thread_id TEXT`.

Update the `init_schema` function (or wherever CREATE TABLE statements live) to use `TEXT` for thread_id:

```sql
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    cwd TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_activity INTEGER NOT NULL
)

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
```

Update the dataclass definitions:

```python
@dataclass(frozen=True)
class SessionRow:
    session_id: str
    cwd: str
    thread_id: str  # was int
    created_at: int
    last_activity: int

@dataclass(frozen=True)
class TaskRow:
    task_id: str
    thread_id: str  # was int
    zellij_pane_id: str | None
    cwd: str
    status: str
    current_claude_session_id: str | None
    current_transcript_path: str | None
    created_at: int
    last_activity: int
```

Add a schema migration function that handles existing databases with INTEGER thread_id. Since tests use in-memory databases (via `conftest.py in_memory_db` fixture calling `init_schema`), the migration only matters for production databases:

```python
async def _migrate_thread_id_to_text(conn: aiosqlite.Connection) -> None:
    """Migrate thread_id from INTEGER to TEXT if needed."""
    cursor = await conn.execute("PRAGMA table_info(sessions)")
    cols = await cursor.fetchall()
    for col in cols:
        if col[1] == "thread_id" and col[2].upper() == "INTEGER":
            # Recreate tables with TEXT thread_id
            await conn.executescript("""
                CREATE TABLE sessions_new (
                    session_id TEXT PRIMARY KEY,
                    cwd TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_activity INTEGER NOT NULL
                );
                INSERT INTO sessions_new
                    SELECT session_id, cwd, CAST(thread_id AS TEXT), created_at, last_activity
                    FROM sessions;
                DROP TABLE sessions;
                ALTER TABLE sessions_new RENAME TO sessions;

                CREATE TABLE tasks_new (
                    task_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    zellij_pane_id TEXT,
                    cwd TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_claude_session_id TEXT,
                    current_transcript_path TEXT,
                    created_at INTEGER NOT NULL,
                    last_activity INTEGER NOT NULL
                );
                INSERT INTO tasks_new
                    SELECT task_id, CAST(thread_id AS TEXT), zellij_pane_id, cwd, status,
                           current_claude_session_id, current_transcript_path, created_at, last_activity
                    FROM tasks;
                DROP TABLE tasks;
                ALTER TABLE tasks_new RENAME TO tasks;
                CREATE INDEX idx_tasks_thread_id ON tasks(thread_id);
            """)
            break
```

Call `_migrate_thread_id_to_text` from `open_db` or `init_schema` after creating tables.

**Note on schema versioning:** The design plan suggests a `schema_version` table. For this single migration, the ad-hoc PRAGMA check is sufficient (YAGNI). If future migrations are needed, add a `schema_version` table at that time.

Update all state helper functions (`upsert_session`, `upsert_task`, `get_task`, etc.) that reference `thread_id` — their type hints should change from `int` to `str`.

**Testing:**

Tests must verify:
- cc-bridge-multiplatform.AC1.2: thread_id stored as TEXT in SQLite schema
- Migration from INTEGER to TEXT preserves data

Test file: `tests/test_state.py` (extend existing)

**Verification:**

```bash
uv run pytest tests/test_state.py -v
```

**Commit:** `feat: migrate thread_id from INTEGER to TEXT in SQLite schema`

<!-- END_TASK_3 -->

<!-- START_TASK_4 -->
### Task 4: Update thread_id types across core modules

**Verifies:** cc-bridge-multiplatform.AC1.2

**Files:**
- Modify: `src/bridge/tasks.py` (~15+ locations where thread_id is typed as `int`)
- Modify: `src/bridge/threads.py` (thread_id parameter types)
- Modify: `src/bridge/approvals.py` (thread_id parameter types)
- Modify: `src/bridge/server.py` (thread_id handling in HTTP handlers)

**Implementation:**

This is a mechanical type change. In every core module:

1. Change `thread_id: int` → `thread_id: str` in all function signatures
2. Change `thread_id: int | None` → `thread_id: str | None`
3. Change return type `-> int` to `-> str` where thread_id is returned
4. Remove any `int()` casts on thread_id values
5. Update any integer comparisons or arithmetic on thread_id

In `tasks.py`:
- `Task` dataclass: `thread_id: int` → `thread_id: str`
- All method signatures referencing thread_id
- The `_pending_startup_notices` and `_flush_startup_notices` if they reference thread_id types

In `threads.py`:
- `ThreadRegistry.__init__` and all methods
- Any dict keys typed as `int` for thread_id

In `approvals.py`:
- `ApprovalRouter` and `PendingApproval` dataclass thread_id fields
- Method signatures

In `server.py`:
- HTTP handler thread_id extraction from request JSON
- Any int conversions on thread_id from request data

**Testing:**

Tests must verify:
- cc-bridge-multiplatform.AC1.2: All core module interfaces use `str` for thread_id

Existing tests should be updated to pass `str` thread IDs (e.g., `"12345"` instead of `12345`).

**Verification:**

```bash
uv run pytest -v
```

**Commit:** `refactor: change thread_id type from int to str across core modules`

<!-- END_TASK_4 -->
<!-- END_SUBCOMPONENT_B -->

<!-- START_SUBCOMPONENT_C (tasks 5-7) -->
<!-- START_TASK_5 -->
### Task 5: Extract Discord bot into backends/discord/

**Verifies:** cc-bridge-multiplatform.AC1.1

**Files:**
- Create: `src/bridge/backends/__init__.py`
- Create: `src/bridge/backends/discord/__init__.py`
- Move: `src/bridge/bot.py` → `src/bridge/backends/discord/bot.py`

**Implementation:**

Create the backends directory structure:

```bash
mkdir -p src/bridge/backends/discord
touch src/bridge/backends/__init__.py
touch src/bridge/backends/discord/__init__.py
```

Move `bot.py` to the new location:

```bash
git mv src/bridge/bot.py src/bridge/backends/discord/bot.py
```

Rename the class from `Bot` to `DiscordBot` in the moved file. The class must implement `ChatPlatform` by adapting its `int`-based discord.py interface to the `str`-based protocol:

- All `thread_id: int` parameters become `thread_id: str` with `int(thread_id)` conversion at the discord.py boundary
- All `-> int` return types become `-> str` with `str(result)` conversion
- `post()` returns `list[str]` (convert each `int` message ID to `str`)
- `create_thread()` returns `str` (convert the discord Thread ID)

Add `str` ↔ `int` conversion at the boundary:

```python
async def post(self, message: str, *, thread_id: str | None = None) -> list[str]:
    discord_thread_id = int(thread_id) if thread_id is not None else None
    # ... existing logic using discord_thread_id ...
    return [str(msg_id) for msg_id in result_ids]
```

Add a `download_attachment` method if not already present — the Discord implementation downloads via `discord.Attachment.read()`.

Keep Discord-specific methods that aren't part of the protocol (like `post_embed`) as additional methods on `DiscordBot` — the Discord backend module can call these directly.

Update imports in `src/bridge/backends/discord/__init__.py`:

```python
from bridge.backends.discord.bot import DiscordBot

__all__ = ["DiscordBot"]
```

**Verification:**

```bash
uv run python -c "from bridge.backends.discord import DiscordBot; print('OK')"
```

**Commit:** `refactor: extract Discord bot into backends/discord/`

<!-- END_TASK_5 -->

<!-- START_TASK_6 -->
### Task 6: Update core module imports to use ChatPlatform protocol

**Verifies:** cc-bridge-multiplatform.AC1.1

**Files:**
- Modify: `src/bridge/tasks.py` (replace `Bot` import with `ChatPlatform`)
- Modify: `src/bridge/threads.py` (replace `Bot` import with `ChatPlatform`)
- Modify: `src/bridge/approvals.py` (replace `Bot` import with `ChatPlatform`)
- Modify: `src/bridge/server.py` (replace `Bot` import with `ChatPlatform`, update construction)

**Implementation:**

In each core module, replace the concrete `Bot` type with the `ChatPlatform` protocol:

In `tasks.py`:
```python
# Remove: from bridge.bot import Bot (or TYPE_CHECKING equivalent)
# Add:
from bridge.platform import ChatPlatform

# Change: self._bot: "Bot | None" → self._bot: ChatPlatform | None
# Change: def bind_bot(self, bot: "Bot") → def bind_bot(self, bot: ChatPlatform)
```

In `threads.py`:
```python
from bridge.platform import ChatPlatform
# Change: bot parameter type to ChatPlatform
```

In `approvals.py`:
```python
from bridge.platform import ChatPlatform
# Change: self._bot: Bot | None → self._bot: ChatPlatform | None
# Change: def bind_bot(self, bot: Bot) → def bind_bot(self, bot: ChatPlatform)
```

In `server.py`:
```python
from bridge.platform import ChatPlatform
from bridge.backends.discord import DiscordBot  # for construction only

# Change: BOT_KEY type from web.AppKey[Bot] → web.AppKey[ChatPlatform]
# Change: build_app(bot: Bot) → build_app(platform: ChatPlatform)
# Change: serve() to construct DiscordBot and pass as ChatPlatform
```

Remove `import discord` from any core module that only used it for type annotations of bot-related types. If a core module uses `discord.Embed` or `discord.RawReactionActionEvent`, those usages need to be refactored — `discord.Embed` moves to the Discord backend, and reaction events are handled through the backend's event handler.

**Embed/rich rendering refactor:** `tasks.py` currently calls `self._bot.post_embed(embed, ...)` and `self._bot.edit_message(..., embed=...)` for subagent blocks and rich formatting. Replace all these calls with the `RichFormatter` protocol defined in Task 1:

1. Add `self._formatter: RichFormatter | None = None` and `bind_formatter()` to `TaskRegistry`
2. Replace `self._bot.post_embed(embed, thread_id=...)` → `self._formatter.post_rich(thread_id, "subagent_block", {...data...})`
3. Replace `self._bot.edit_message(..., embed=...)` → `self._formatter.edit_rich(thread_id, msg_id, "subagent_block", {...data...})`
4. Create `DiscordRichFormatter` in `backends/discord/formatting.py` that receives the raw `DiscordBot` and calls `post_embed`/`edit_message(embed=...)` internally
5. Wire `DiscordRichFormatter` into `TaskRegistry.bind_formatter()` in `server.py`

This ensures `tasks.py` never imports `discord.*` and all rich rendering is delegated through the formatter protocol.

**Testing:**

Tests must verify:
- cc-bridge-multiplatform.AC1.1: No `discord.*` imports in tasks.py, threads.py, approvals.py after extraction

Verify with grep:
```bash
grep -n "import discord" src/bridge/tasks.py src/bridge/threads.py src/bridge/approvals.py
```
Expected: No matches.

**Verification:**

```bash
uv run pytest -v
```

**Commit:** `refactor: core modules reference ChatPlatform protocol, not concrete Bot`

<!-- END_TASK_6 -->

<!-- START_TASK_7 -->
### Task 7: Update test infrastructure and existing tests

**Verifies:** cc-bridge-multiplatform.AC1.3, cc-bridge-multiplatform.AC5.1

**Files:**
- Modify: `tests/fakes.py` (keep FakeBot for Discord tests, FakePlatform for core tests)
- Modify: Test files that test core logic (update thread_id from int to str, use FakePlatform where appropriate)
- Modify: Test files that test Discord-specific logic (update imports from `bridge.bot` to `bridge.backends.discord.bot`)

**Implementation:**

Update test imports:
- Tests that import `from bridge.bot import Bot` → `from bridge.backends.discord.bot import DiscordBot`
- Tests that use `FakeBot` for core logic testing should switch to `FakePlatform`
- Tests that specifically test Discord behaviour keep `FakeBot`

Update all thread_id values in tests from integers to strings:
- `thread_id=12345` → `thread_id="12345"`
- `create_thread(...)` assertions: expect `str` return, not `int`
- `FakeBot` in `tests/fakes.py`: update to return `str` IDs (or keep returning `int` for Discord-specific tests and add `FakePlatform` for protocol tests)

The existing `FakeBot` should be updated to match `DiscordBot`'s new `str` interface, since `DiscordBot` now presents `str` IDs externally.

**Testing:**

Tests must verify:
- cc-bridge-multiplatform.AC1.3: Core tests pass with FakePlatform (no real backend)
- cc-bridge-multiplatform.AC5.1: All existing test files pass after relocation

**Verification:**

```bash
uv run pytest -v
```

Expected: All tests pass. Zero failures.

**Commit:** `test: update test infrastructure for ChatPlatform protocol`

<!-- END_TASK_7 -->

<!-- START_TASK_8 -->
### Task 8: Relocate Discord-specific tests to backends/discord/

**Files:**
- Create: `tests/backends/discord/__init__.py`
- Create: `tests/backends/discord/fakes.py`
- Move: `tests/test_bot.py` → `tests/backends/discord/test_bot.py`
- Move: `tests/test_commands.py` → `tests/backends/discord/test_commands.py` (will be further modified in Phase 3)

**Implementation:**

Create the Discord test directory:

```bash
mkdir -p tests/backends/discord
touch tests/backends/discord/__init__.py
```

Move Discord-specific test files:

```bash
git mv tests/test_bot.py tests/backends/discord/test_bot.py
```

Note: `tests/test_commands.py` will be moved in Phase 3 when the commands module moves to `backends/discord/commands.py`.

Create `tests/backends/discord/fakes.py`:
- Move `FakeBot` from `tests/fakes.py` and rename to `FakeDiscordBot`
- `FakeDiscordBot` mirrors `DiscordBot`'s `str`-based interface (after Phase 2 migration)
- Update `tests/fakes.py` to keep `FakePlatform` only (remove `FakeBot`)
- Update any Discord-specific tests that import `FakeBot` from `tests/fakes.py` to import `FakeDiscordBot` from the new location

**Verification:**

```bash
uv run pytest -v
```

**Commit:** `refactor: relocate Discord-specific tests to backends/discord/`

<!-- END_TASK_8 -->
<!-- END_SUBCOMPONENT_C -->

<!-- START_TASK_9 -->
### Task 9: Full regression verification

**Files:**
- None (verification only)

**Verification:**

```bash
# Verify no discord imports in core modules
grep -rn "import discord" src/bridge/tasks.py src/bridge/threads.py src/bridge/approvals.py

# Verify thread_id is str everywhere in core
grep -rn "thread_id: int" src/bridge/tasks.py src/bridge/threads.py src/bridge/approvals.py src/bridge/state.py src/bridge/server.py

# Full test suite
uv run pytest -v
```

Expected:
- First grep: no matches
- Second grep: no matches
- All tests pass

<!-- END_TASK_9 -->

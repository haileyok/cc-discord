"""Tests for Task and TaskRegistry."""

import asyncio
import os
from dataclasses import dataclass, field

import pytest

from bridge.state import TaskRow, upsert_task
from bridge.tasks import Task, TaskRegistry, TaskSpawnError, _ToolSummaryAggregator
from bridge.zellij import ZellijError, ZellijManager
from tests.backends.discord.fakes import FakeDiscordBot
from tests.fakes import FakeZellij


@pytest.fixture
async def fake_bot():
    return FakeDiscordBot()


@pytest.fixture
async def fake_zellij(monkeypatch):
    """Create a ZellijManager with mocked _run method."""
    mgr = ZellijManager()

    async def mock_run(*argv, env=None, timeout=10.0):
        """Mock _run to always return success."""
        return (0, "", "")

    monkeypatch.setattr(mgr, "_run", mock_run)
    return mgr


@pytest.mark.asyncio
class TestTask:
    """Tests for Task dataclass."""

    async def test_task_from_row(self) -> None:
        """Task.from_row converts TaskRow to Task."""
        row = TaskRow(
            task_id="task-123",
            thread_id="999",
            zellij_pane_id="terminal_1",
            cwd="/tmp/test",
            status="running",
            current_claude_session_id="sess-abc",
            current_transcript_path="/path/transcript",
            created_at=1000,
            last_activity=2000,
        )
        task = Task.from_row(row)
        assert task.task_id == "task-123"
        assert task.thread_id == "999"
        assert task.zellij_pane_id == "terminal_1"
        assert task.cwd == "/tmp/test"
        assert task.status == "running"
        assert task.current_claude_session_id == "sess-abc"
        assert task.current_transcript_path == "/path/transcript"
        assert task.created_at == 1000
        assert task.last_activity == 2000


@pytest.mark.asyncio
class TestTaskRegistry:
    """Tests for TaskRegistry."""

    async def test_load_from_db(self, fake_bot, fake_zellij, in_memory_db) -> None:
        """load_from_db populates all three maps."""
        now = 1000
        # Insert some tasks
        await upsert_task(
            in_memory_db,
            "task-1",
            1001,
            "/a",
            "running",
            current_claude_session_id="sess-1",
            now=now,
        )
        await upsert_task(
            in_memory_db,
            "task-2",
            1002,
            "/b",
            "spawning",
            current_claude_session_id="sess-2",
            now=now,
        )
        # Stopped task should not be loaded
        await upsert_task(
            in_memory_db, "task-3", 1003, "/c", "stopped", now=now
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Should have loaded 2 tasks
        assert registry.get_by_task_id("task-1") is not None
        assert registry.get_by_task_id("task-2") is not None
        assert registry.get_by_task_id("task-3") is None  # Stopped task not loaded

        # By thread_id
        assert registry.get_by_thread_id("1001") is not None
        assert registry.get_by_thread_id("1002") is not None
        assert registry.get_by_thread_id("1003") is None

        # By session_id
        assert registry.get_by_session_id("sess-1") is not None
        assert registry.get_by_session_id("sess-2") is not None

    async def test_get_by_task_id(self, fake_bot, fake_zellij, in_memory_db) -> None:
        """get_by_task_id returns task or None."""
        now = 1000
        await upsert_task(
            in_memory_db, "task-123", 999, "/tmp", "running", now=now
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.task_id == "task-123"

        assert registry.get_by_task_id("unknown") is None

    async def test_get_by_thread_id(self, fake_bot, fake_zellij, in_memory_db) -> None:
        """get_by_thread_id returns task or None."""
        now = 1000
        await upsert_task(
            in_memory_db, "task-123", 999, "/tmp", "running", now=now
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        task = registry.get_by_thread_id(999)
        assert task is not None
        assert task.task_id == "task-123"

        assert registry.get_by_thread_id(888) is None

    async def test_get_by_session_id(self, fake_bot, fake_zellij, in_memory_db) -> None:
        """get_by_session_id returns task or None."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        task = registry.get_by_session_id("sess-abc")
        assert task is not None
        assert task.task_id == "task-123"

        assert registry.get_by_session_id("unknown") is None

    async def test_handle_event_session_start_with_task(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """handle_event('SessionStart') with matching task_id updates and posts."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "spawning",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Handle SessionStart with matching task_id
        body = {
            "hook_event_name": "SessionStart",
            "session_id": "sess-abc",
            "cwd": "/tmp",
            "transcript_path": "/path/to/transcript",
            "env_passthrough": {"CC_DISCORD_TASK_ID": "task-123"},
        }
        await registry.handle_event("SessionStart", body)

        # Task should be updated
        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.current_claude_session_id == "sess-abc"
        assert task.current_transcript_path == "/path/to/transcript"
        assert task.status == "running"

        # Bot should have posted
        posts = fake_bot.get_post_calls()
        assert len(posts) == 1
        assert posts[0]["thread_id"] == 999
        assert "🟢 Task started" in posts[0]["content"]
        assert "sess-abc" in posts[0]["content"]

    async def test_handle_event_session_start_rotates_session_id(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """handle_event('SessionStart') rotates session_id and invalidates old mapping."""
        now = 1000
        # Seed task with initial session_id
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-A",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Verify initial state
        assert registry.get_by_session_id("sess-A") is not None
        assert registry.get_by_session_id("sess-B") is None

        # Rotate session_id to sess-B (e.g., on /clear or /compact)
        body = {
            "hook_event_name": "SessionStart",
            "session_id": "sess-B",
            "cwd": "/tmp",
            "transcript_path": "/path/to/transcript",
            "env_passthrough": {"CC_DISCORD_TASK_ID": "task-123"},
        }
        await registry.handle_event("SessionStart", body)

        # Task should be updated with new session_id
        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.current_claude_session_id == "sess-B"

        # Old session_id mapping should be invalidated
        assert registry.get_by_session_id("sess-A") is None
        # New session_id mapping should be valid
        assert registry.get_by_session_id("sess-B") is not None

    async def test_handle_event_session_start_missing_session_id(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """handle_event('SessionStart') without session_id is dropped (no state mutation)."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "spawning",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # SessionStart with task_id but missing session_id — should be dropped
        body = {
            "hook_event_name": "SessionStart",
            "cwd": "/tmp",
            "transcript_path": "/path/to/transcript",
            "env_passthrough": {"CC_DISCORD_TASK_ID": "task-123"},
        }
        await registry.handle_event("SessionStart", body)

        # Task should remain unchanged
        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.current_claude_session_id is None
        assert task.status == "spawning"  # Status stays spawning
        assert task.current_transcript_path is None

        # No post should happen
        posts = fake_bot.get_post_calls()
        assert len(posts) == 0

    async def test_handle_event_session_start_without_task(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """handle_event('SessionStart') without task_id is silent no-op."""
        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        body = {
            "hook_event_name": "SessionStart",
            "session_id": "sess-abc",
            "cwd": "/tmp",
            "transcript_path": "/path/to/transcript",
        }
        await registry.handle_event("SessionStart", body)

        # No post should happen
        posts = fake_bot.get_post_calls()
        assert len(posts) == 0

    async def test_handle_event_unknown_event(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """handle_event with unknown event name returns without raising."""
        now = 1000
        await upsert_task(
            in_memory_db, "task-123", 999, "/tmp", "running", now=now
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Should not raise
        body = {"hook_event_name": "Banana", "some": "data"}
        await registry.handle_event("Banana", body)

        # No post
        posts = fake_bot.get_post_calls()
        assert len(posts) == 0

    async def test_handlers_dict_has_all_event_names(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """_HANDLERS dict contains all expected event names."""
        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)

        expected_events = {
            "SessionStart",
            "UserPromptSubmit",
            "PostToolUse",
            "PostToolUseFailure",
            "Stop",
            "Notification",
            "SessionEnd",
            "SubagentStop",
            "PreCompact",
        }
        assert set(registry._HANDLERS.keys()) == expected_events

    async def test_spawn_task_nonexistent_cwd(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """spawn_task with nonexistent cwd raises TaskSpawnError."""
        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)

        with pytest.raises(TaskSpawnError, match="cwd does not exist"):
            await registry.spawn_task("/nonexistent/path")

    async def test_spawn_task_success(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch
    ) -> None:
        """spawn_task succeeds: creates thread, persists row, indexes task."""
        # Mock zellij.spawn_task to return a pane_id
        pane_id = "terminal_1"

        async def mock_spawn_task(
            cwd: str, env: dict[str, str], pane_name: str, extra_argv: list[str] | None = None
        ) -> str:
            return pane_id

        monkeypatch.setattr(fake_zellij, "spawn_task", mock_spawn_task)

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)

        cwd = "/tmp"
        task = await registry.spawn_task(cwd)

        # Verify thread was created with correct name format
        thread_calls = fake_bot.get_thread_calls()
        assert len(thread_calls) == 1
        assert thread_calls[0]["name"].startswith("cc · tmp · ")
        assert len(thread_calls[0]["name"]) > len("cc · tmp · ")  # has task_id suffix

        # Verify task fields
        assert task.task_id is not None
        assert task.thread_id == 2000
        assert task.zellij_pane_id == pane_id
        assert task.cwd == cwd
        assert task.status == "spawning"
        assert task.current_claude_session_id is None
        assert task.current_transcript_path is None
        assert task.created_at > 0
        assert task.last_activity > 0

        # Verify task is indexed by task_id
        indexed = registry.get_by_task_id(task.task_id)
        assert indexed is not None
        assert indexed.task_id == task.task_id

        # Verify task is indexed by thread_id
        indexed = registry.get_by_thread_id(task.thread_id)
        assert indexed is not None
        assert indexed.task_id == task.task_id

        # Verify task is persisted to DB
        from bridge.state import get_task
        db_row = await get_task(in_memory_db, task.task_id)
        assert db_row is not None
        assert db_row.status == "spawning"
        assert db_row.zellij_pane_id == pane_id

    async def test_spawn_task_env_vars(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch
    ) -> None:
        """spawn_task injects CC_DISCORD_TASK_ID and BRIDGE_URL into env."""
        captured_env = {}

        async def mock_spawn_task(cwd: str, env: dict[str, str], pane_name: str, extra_argv: list[str] | None = None) -> str:
            captured_env.update(env)
            return "terminal_1"

        monkeypatch.setattr(fake_zellij, "spawn_task", mock_spawn_task)

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)

        # Clear BRIDGE_URL from os.environ for this test to use default
        old_bridge_url = os.environ.pop("BRIDGE_URL", None)
        try:
            task = await registry.spawn_task("/tmp")

            # Verify CC_DISCORD_TASK_ID is in env
            assert captured_env["CC_DISCORD_TASK_ID"] == task.task_id

            # Verify BRIDGE_URL defaults to localhost
            assert captured_env["BRIDGE_URL"] == "http://127.0.0.1:8787"
        finally:
            if old_bridge_url is not None:
                os.environ["BRIDGE_URL"] = old_bridge_url

    async def test_spawn_task_env_bridge_url_from_env(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch
    ) -> None:
        """spawn_task preserves BRIDGE_URL from os.environ if set."""
        captured_env = {}

        async def mock_spawn_task(cwd: str, env: dict[str, str], pane_name: str, extra_argv: list[str] | None = None) -> str:
            captured_env.update(env)
            return "terminal_1"

        monkeypatch.setattr(fake_zellij, "spawn_task", mock_spawn_task)

        old_bridge_url = os.environ.get("BRIDGE_URL")
        try:
            os.environ["BRIDGE_URL"] = "http://example.com:9999"

            registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
            await registry.spawn_task("/tmp")

            # Verify BRIDGE_URL is preserved from env
            assert captured_env["BRIDGE_URL"] == "http://example.com:9999"
        finally:
            if old_bridge_url is not None:
                os.environ["BRIDGE_URL"] = old_bridge_url
            else:
                os.environ.pop("BRIDGE_URL", None)

    async def test_on_session_start_no_cc_discord_task_id(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """_on_session_start without CC_DISCORD_TASK_ID is silently dropped."""
        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)

        body = {
            "hook_event_name": "SessionStart",
            "session_id": "sess-abc",
            "transcript_path": "/path/to/transcript",
            "env_passthrough": {},
        }
        await registry.handle_event("SessionStart", body)

        # No posts should happen
        posts = fake_bot.get_post_calls()
        assert len(posts) == 0

    async def test_on_session_start_updates_status_to_running(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """_on_session_start flips status from spawning to running."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "spawning",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        body = {
            "hook_event_name": "SessionStart",
            "session_id": "sess-abc",
            "transcript_path": "/path/to/transcript",
            "env_passthrough": {"CC_DISCORD_TASK_ID": "task-123"},
        }
        await registry.handle_event("SessionStart", body)

        # Task should be updated to running
        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.status == "running"
        assert task.current_claude_session_id == "sess-abc"
        assert task.current_transcript_path == "/path/to/transcript"
        assert task.last_activity > now

    async def test_on_session_start_bind_notice(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """_on_session_start posts a bind notice with session_id."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "spawning",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        body = {
            "hook_event_name": "SessionStart",
            "session_id": "sess-abc123",
            "transcript_path": "/path/to/transcript",
            "env_passthrough": {"CC_DISCORD_TASK_ID": "task-123"},
        }
        await registry.handle_event("SessionStart", body)

        # Verify bind notice was posted
        posts = fake_bot.get_post_calls()
        assert len(posts) == 1
        assert posts[0]["thread_id"] == 999
        assert "🟢 Task started" in posts[0]["content"]
        assert "sess-abc" in posts[0]["content"]

    async def test_on_session_start_unknown_task_id(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """_on_session_start with unknown task_id logs warning and does nothing."""
        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        body = {
            "hook_event_name": "SessionStart",
            "session_id": "sess-abc",
            "transcript_path": "/path/to/transcript",
            "env_passthrough": {"CC_DISCORD_TASK_ID": "unknown-task"},
        }
        await registry.handle_event("SessionStart", body)

        # No posts should happen
        posts = fake_bot.get_post_calls()
        assert len(posts) == 0

    async def test_handle_event_session_start_missing_transcript_path(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """handle_event('SessionStart') without transcript_path is dropped (no state mutation)."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "spawning",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # SessionStart with task_id but missing transcript_path — should be dropped
        body = {
            "hook_event_name": "SessionStart",
            "session_id": "sess-abc",
            "cwd": "/tmp",
            "env_passthrough": {"CC_DISCORD_TASK_ID": "task-123"},
        }
        await registry.handle_event("SessionStart", body)

        # Task should remain unchanged
        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.current_claude_session_id is None
        assert task.status == "spawning"  # Status stays spawning
        assert task.current_transcript_path is None

        # No post should happen
        posts = fake_bot.get_post_calls()
        assert len(posts) == 0

    async def test_spawn_task_zellij_failure(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch
    ) -> None:
        """spawn_task on zellij failure marks task as crashed and re-raises."""
        from bridge.zellij import ZellijSpawnError

        async def mock_spawn_task(cwd: str, env: dict[str, str], pane_name: str, extra_argv: list[str] | None = None) -> str:
            raise ZellijSpawnError("Could not resolve pane id")

        monkeypatch.setattr(fake_zellij, "spawn_task", mock_spawn_task)

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)

        with pytest.raises(ZellijSpawnError):
            await registry.spawn_task("/tmp")

        # Verify that a task was created and marked as crashed
        # We need to find the created task. Let's check the thread calls
        thread_calls = fake_bot.get_thread_calls()
        assert len(thread_calls) == 1
        # The exception was raised, which is what we're testing

    async def test_on_session_start_matcher_startup(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """_on_session_start with matcher='startup' posts 🟢 notice."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "spawning",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        body = {
            "session_id": "sess-abc123",
            "transcript_path": "/path/to/transcript",
            "matcher": "startup",
            "env_passthrough": {"CC_DISCORD_TASK_ID": "task-123"},
        }
        await registry._on_session_start(body)

        posts = fake_bot.get_post_calls()
        assert len(posts) == 1
        assert "🟢 Task started" in posts[0]["content"]

    async def test_on_session_start_matcher_clear(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """_on_session_start with matcher='clear' posts 🧹 notice and rebinds."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-old",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Verify old session id is indexed
        assert registry.get_by_session_id("sess-old") is not None

        body = {
            "session_id": "sess-new123",
            "transcript_path": "/path/to/transcript2",
            "matcher": "clear",
            "env_passthrough": {"CC_DISCORD_TASK_ID": "task-123"},
        }
        await registry._on_session_start(body)

        # Verify rebind
        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.current_claude_session_id == "sess-new123"
        assert task.current_transcript_path == "/path/to/transcript2"
        assert task.status == "running"  # Status unchanged

        # Verify session_id index updated
        assert registry.get_by_session_id("sess-old") is None
        assert registry.get_by_session_id("sess-new123") is not None

        # Verify notice
        posts = fake_bot.get_post_calls()
        assert len(posts) == 1
        assert "🧹 Context cleared" in posts[0]["content"]
        assert "sess-new" in posts[0]["content"]

    async def test_on_session_start_matcher_compact(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """_on_session_start with matcher='compact' posts 🧰 notice and rebinds."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-old",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        body = {
            "session_id": "sess-new456",
            "transcript_path": "/path/to/transcript3",
            "matcher": "compact",
            "env_passthrough": {"CC_DISCORD_TASK_ID": "task-123"},
        }
        await registry._on_session_start(body)

        # Verify rebind
        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.current_claude_session_id == "sess-new456"

        # Verify notice
        posts = fake_bot.get_post_calls()
        assert len(posts) == 1
        assert "🧰 Context compacted" in posts[0]["content"]

    async def test_on_session_start_matcher_resume(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """_on_session_start with matcher='resume' rebinds without posting notice."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-old",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        body = {
            "session_id": "sess-resumed789",
            "transcript_path": "/path/to/transcript4",
            "matcher": "resume",
            "env_passthrough": {"CC_DISCORD_TASK_ID": "task-123"},
        }
        await registry._on_session_start(body)

        # Verify rebind
        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.current_claude_session_id == "sess-resumed789"

        # Verify NO notice
        posts = fake_bot.get_post_calls()
        assert len(posts) == 0

    async def test_on_session_start_matcher_unknown(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """_on_session_start with unknown matcher posts fallback notice."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "spawning",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        body = {
            "session_id": "sess-unknown",
            "transcript_path": "/path/to/transcript",
            "matcher": "weird_matcher",
            "env_passthrough": {"CC_DISCORD_TASK_ID": "task-123"},
        }
        await registry._on_session_start(body)

        posts = fake_bot.get_post_calls()
        assert len(posts) == 1
        assert "Bound to session" in posts[0]["content"]

    async def test_on_session_end_abnormal_exit_error(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """_on_session_end with exit_reason='error' flips status to crashed and posts 💥."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-test",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        body = {
            "session_id": "sess-test",
            "exit_reason": "error",
        }
        await registry._on_session_end(body)

        # Verify status flipped to crashed
        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.status == "crashed"

        # Verify 💥 notice posted
        posts = fake_bot.get_post_calls()
        assert len(posts) == 1
        assert "💥 Claude process exited" in posts[0]["content"]

        # Verify thread archived
        archives = fake_bot.get_archive_calls()
        assert len(archives) == 1

    async def test_on_session_end_abnormal_exit_sigint(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """_on_session_end with exit_reason='sigint' flips status to crashed."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-test",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        body = {
            "session_id": "sess-test",
            "exit_reason": "sigint",
        }
        await registry._on_session_end(body)

        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.status == "crashed"

    async def test_on_session_end_normal_exit(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """_on_session_end with exit_reason='exit' flips status to stopped (graceful)."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-test",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        body = {
            "session_id": "sess-test",
            "exit_reason": "exit",
        }
        await registry._on_session_end(body)

        # Verify status flipped to stopped (not crashed)
        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.status == "stopped"

        # Verify NO 💥 notice
        posts = fake_bot.get_post_calls()
        assert len(posts) == 0

        # But thread still archived
        archives = fake_bot.get_archive_calls()
        assert len(archives) == 1

    async def test_on_session_end_no_exit_reason(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """_on_session_end with no exit_reason (None) treats as normal exit."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-test",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        body = {
            "session_id": "sess-test",
            # No exit_reason field
        }
        await registry._on_session_end(body)

        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.status == "stopped"  # Normal exit

    async def test_on_session_end_idempotent_already_stopped(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """_on_session_end when task already stopped is idempotent."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "stopped",
            current_claude_session_id="sess-test",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        # Manually load the stopped task since load_from_db skips stopped tasks
        task = Task(
            task_id="task-123",
            thread_id="999",
            zellij_pane_id=None,
            cwd="/tmp",
            status="stopped",
            current_claude_session_id="sess-test",
            current_transcript_path=None,
            created_at=now,
            last_activity=now,
        )
        await registry._index(task)

        body = {
            "session_id": "sess-test",
            "exit_reason": "error",
        }
        await registry._on_session_end(body)

        # Verify status stays stopped (not flipped to crashed)
        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.status == "stopped"

        # Verify NO new notice (was already stopped)
        posts = fake_bot.get_post_calls()
        assert len(posts) == 0

    async def test_on_session_end_unknown_session_is_noop(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """_on_session_end for unknown session_id is a no-op."""
        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)

        body = {
            "session_id": "sess-unknown",
            "exit_reason": "error",
        }
        await registry._on_session_end(body)

        # Should be silent no-op
        posts = fake_bot.get_post_calls()
        assert len(posts) == 0

    async def test_load_from_db_pane_alive_posts_bridge_restart_notice(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch
    ) -> None:
        """load_from_db with live pane: task stays running, posts bridge-restart notice."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            zellij_pane_id="terminal_3",
            current_claude_session_id="sess-abc",
            now=now,
        )

        async def mock_list_panes():
            return [{"id": "terminal_3", "exited": False}]

        monkeypatch.setattr(fake_zellij, "list_panes", mock_list_panes)

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db(reconcile_with_zellij=True)

        # Notices are deferred until bot is ready; flush them now.
        assert fake_bot.get_post_calls() == []
        await registry.flush_startup_notices()

        # Task should be loaded and in memory
        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.status == "running"

        # Should post bridge-restart notice to live recovered task
        posts = fake_bot.get_post_calls()
        assert len(posts) == 1
        assert posts[0]["thread_id"] == 999
        assert "Bridge restarted" in posts[0]["content"]
        assert "hook level" in posts[0]["content"]

    async def test_load_from_db_pane_missing_marks_crashed(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch
    ) -> None:
        """load_from_db with missing pane: task marked crashed, posts 💥, thread archived."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            zellij_pane_id="terminal_3",
            current_claude_session_id="sess-abc",
            now=now,
        )

        async def mock_list_panes():
            return [{"id": "terminal_4", "exited": False}]  # Different pane

        monkeypatch.setattr(fake_zellij, "list_panes", mock_list_panes)

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db(reconcile_with_zellij=True)

        # Notices and archive are deferred until bot is ready; flush them now.
        assert fake_bot.get_post_calls() == []
        assert fake_bot.get_archive_calls() == []
        await registry.flush_startup_notices()

        # Task should be loaded but marked crashed
        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.status == "crashed"

        # Post recovery crash notice
        posts = fake_bot.get_post_calls()
        assert any("💥 Bridge restarted" in p["content"] for p in posts)
        assert any("🛡 ❌ Any pending approval" in p["content"] for p in posts)

        # Thread archived
        archives = fake_bot.get_archive_calls()
        assert len(archives) == 1

    async def test_load_from_db_pane_exited_marks_crashed(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch
    ) -> None:
        """load_from_db with exited pane: task marked crashed."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            zellij_pane_id="terminal_3",
            current_claude_session_id="sess-abc",
            now=now,
        )

        async def mock_list_panes():
            return [{"id": "terminal_3", "exited": True}]  # Pane exited

        monkeypatch.setattr(fake_zellij, "list_panes", mock_list_panes)

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db(reconcile_with_zellij=True)

        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.status == "crashed"

    async def test_load_from_db_stopped_task_not_loaded(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch
    ) -> None:
        """load_from_db skips stopped/crashed tasks (original behavior)."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-stopped",
            1001,
            "/tmp",
            "stopped",
            now=now,
        )

        async def mock_list_panes():
            return []

        monkeypatch.setattr(fake_zellij, "list_panes", mock_list_panes)

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Stopped task should not be in memory
        task = registry.get_by_task_id("task-stopped")
        assert task is None

    async def test_load_from_db_zellij_fails_assumes_alive(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch
    ) -> None:
        """load_from_db when list_panes fails: assumes all panes alive (defensive)."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            zellij_pane_id="terminal_3",
            current_claude_session_id="sess-abc",
            now=now,
        )

        async def mock_list_panes():
            raise ZellijError("zellij command failed")

        monkeypatch.setattr(fake_zellij, "list_panes", mock_list_panes)

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Task should be kept as-is (defensive assumption)
        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.status == "running"  # Not crashed

    async def test_load_from_db_does_not_touch_bot_when_unset(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch
    ) -> None:
        """Reconciliation must not call self._bot during load_from_db — at server
        startup the registry is constructed with bot=None and only wired up after
        the bot logs in. flush_startup_notices() drains the queue once bot is set.
        """
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-live",
            900,
            "/tmp",
            "running",
            zellij_pane_id="terminal_1",
            current_claude_session_id="sess-live",
            now=now,
        )
        await upsert_task(
            in_memory_db,
            "task-dead",
            901,
            "/tmp",
            "running",
            zellij_pane_id="terminal_dead",
            current_claude_session_id="sess-dead",
            now=now,
        )

        async def mock_list_panes():
            return [{"id": "terminal_1", "exited": False}]

        monkeypatch.setattr(fake_zellij, "list_panes", mock_list_panes)

        # bot=None mirrors server.serve()'s startup ordering.
        registry = TaskRegistry(in_memory_db, None, fake_zellij)  # type: ignore[arg-type]
        await registry.load_from_db(reconcile_with_zellij=True)  # must not raise

        # State changes still applied: dead task flipped to crashed.
        assert registry.get_by_task_id("task-dead").status == "crashed"
        assert registry.get_by_task_id("task-live").status == "running"

        # Now wire up the bot and flush; deferred posts/archives land.
        registry._bot = fake_bot
        await registry.flush_startup_notices()

        posts = fake_bot.get_post_calls()
        assert any("💥 Bridge restarted" in p["content"] and p["thread_id"] == 901 for p in posts)
        assert any("hook level" in p["content"] and p["thread_id"] == 900 for p in posts)
        assert len(fake_bot.get_archive_calls()) == 1

        # Second flush is a no-op (idempotent drain).
        await registry.flush_startup_notices()
        assert len(fake_bot.get_post_calls()) == len(posts)


@dataclass
class FakeChannel:
    """Minimal fake channel for maybe_route_message tests."""
    id: int


@dataclass
class FakeAuthor:
    """Minimal fake author for maybe_route_message tests."""
    id: int
    bot: bool = False


@dataclass
class FakeAttachment:
    """Minimal fake attachment for maybe_route_message tests."""
    url: str = "http://example.com/image.png"


@dataclass
class FakeMsgLike:
    """Fake message matching MessageLike protocol for routing tests."""
    channel: FakeChannel
    content: str = ""
    attachments: list[FakeAttachment] = field(default_factory=list)
    author: FakeAuthor = field(default_factory=lambda: FakeAuthor(id=123))
    created_at: object = field(default_factory=lambda: __import__('datetime').datetime.now(__import__('datetime').timezone.utc))


@pytest.mark.asyncio
class TestMaybeRouteMessage:
    """Tests for TaskRegistry.maybe_route_message."""

    async def test_maybe_route_message_no_task_returns_false(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """maybe_route_message returns False when no task is bound to the thread."""
        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        msg = FakeMsgLike(channel=FakeChannel(id=999))
        result = await registry.maybe_route_message(msg)
        assert result is False

    async def test_maybe_route_message_no_pane_id_returns_false(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch
    ) -> None:
        """maybe_route_message returns False when task.zellij_pane_id is None."""
        # Create a task with no pane_id
        task_id = "task-xyz"
        thread_id = 5000
        now = int(__import__('time').time())
        await upsert_task(
            in_memory_db, task_id, thread_id, "/tmp", "spawning",
            zellij_pane_id=None,
            current_claude_session_id=None,
            current_transcript_path=None,
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        msg = FakeMsgLike(channel=FakeChannel(id=thread_id))
        result = await registry.maybe_route_message(msg)
        assert result is False

    async def test_maybe_route_message_writes_to_pane(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch
    ) -> None:
        """maybe_route_message calls zellij.write_to_pane and returns True for a bound task."""
        task_id = "task-abc"
        thread_id = 6000
        pane_id = "pane_1"
        now = int(__import__('time').time())
        await upsert_task(
            in_memory_db, task_id, thread_id, "/tmp", "running",
            zellij_pane_id=pane_id,
            current_claude_session_id="sess-123",
            current_transcript_path="/path/transcript",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Track write_to_pane calls
        write_calls = []

        async def mock_write_to_pane(pane_id: str, text: str) -> None:
            write_calls.append({"pane_id": pane_id, "text": text})

        monkeypatch.setattr(fake_zellij, "write_to_pane", mock_write_to_pane)

        msg = FakeMsgLike(channel=FakeChannel(id=thread_id), content="hello world")
        result = await registry.maybe_route_message(msg)

        assert result is True
        assert len(write_calls) == 1
        assert write_calls[0]["pane_id"] == pane_id
        assert write_calls[0]["text"] == "hello world\n"

    async def test_maybe_route_message_empty_content_no_attachments_returns_true(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch
    ) -> None:
        """maybe_route_message returns True silently for empty message with no attachments."""
        task_id = "task-def"
        thread_id = 7000
        pane_id = "pane_2"
        now = int(__import__('time').time())
        await upsert_task(
            in_memory_db, task_id, thread_id, "/tmp", "running",
            zellij_pane_id=pane_id,
            current_claude_session_id="sess-456",
            current_transcript_path="/path/transcript",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        write_calls = []

        async def mock_write_to_pane(pane_id: str, text: str) -> None:
            write_calls.append({"pane_id": pane_id, "text": text})

        monkeypatch.setattr(fake_zellij, "write_to_pane", mock_write_to_pane)

        # Empty message, no attachments
        msg = FakeMsgLike(channel=FakeChannel(id=thread_id), content="")
        result = await registry.maybe_route_message(msg)

        assert result is True
        assert len(write_calls) == 0  # No write to pane

    async def test_maybe_route_message_image_placeholder(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch
    ) -> None:
        """maybe_route_message writes placeholder for message with attachments but no content."""
        task_id = "task-ghi"
        thread_id = 8000
        pane_id = "pane_3"
        now = int(__import__('time').time())
        await upsert_task(
            in_memory_db, task_id, thread_id, "/tmp", "running",
            zellij_pane_id=pane_id,
            current_claude_session_id="sess-789",
            current_transcript_path="/path/transcript",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        write_calls = []

        async def mock_write_to_pane(pane_id: str, text: str) -> None:
            write_calls.append({"pane_id": pane_id, "text": text})

        monkeypatch.setattr(fake_zellij, "write_to_pane", mock_write_to_pane)

        # Empty content but with attachment
        msg = FakeMsgLike(
            channel=FakeChannel(id=thread_id),
            content="",
            attachments=[FakeAttachment(url="http://example.com/image.png")]
        )
        result = await registry.maybe_route_message(msg)

        assert result is True
        assert len(write_calls) == 1
        assert "(image attached — image relay not yet supported)" in write_calls[0]["text"]


@pytest.mark.asyncio
class TestTaskRegistryPhase3:
    """Tests for Phase 3 task lifecycle methods: list_tasks, stop_task, kill_task, restart_task."""

    async def test_list_tasks_returns_active_ordered_by_last_activity(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """list_tasks returns active tasks ordered by last_activity DESC; filters out stopped/crashed."""
        now = 1000
        # Insert active task
        await upsert_task(
            in_memory_db,
            "task-1",
            1001,
            "/a",
            "running",
            current_claude_session_id="sess-1",
            now=now,
        )
        # Insert another active task with more recent last_activity
        await upsert_task(
            in_memory_db,
            "task-2",
            1002,
            "/b",
            "running",
            current_claude_session_id="sess-2",
            now=now + 100,
        )
        # Insert stopped task (should be filtered out)
        await upsert_task(
            in_memory_db,
            "task-3",
            1003,
            "/c",
            "stopped",
            now=now,
        )
        # Insert crashed task (should be filtered out)
        await upsert_task(
            in_memory_db,
            "task-4",
            1004,
            "/d",
            "crashed",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        tasks = await registry.list_tasks()

        # Should return 2 active tasks, with task-2 first (higher last_activity)
        assert len(tasks) == 2
        assert tasks[0].task_id == "task-2"
        assert tasks[1].task_id == "task-1"

    async def test_stop_task_with_pane_alive_sends_exit_and_waits_for_session_end(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch
    ) -> None:
        """stop_task sends /exit and waits for SessionEnd to resolve; returns True on success."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            zellij_pane_id="terminal_1",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        write_calls = []

        async def mock_write_to_pane(pane_id: str, text: str) -> None:
            write_calls.append({"pane_id": pane_id, "text": text})

        monkeypatch.setattr(fake_zellij, "write_to_pane", mock_write_to_pane)

        # Spawn stop_task (will be waiting for SessionEnd)
        import asyncio

        stop_task_handle = asyncio.create_task(registry.stop_task("task-123", timeout=5.0))

        # Give it a moment to send /exit
        await asyncio.sleep(0.1)

        # Verify /exit was sent
        assert len(write_calls) == 1
        assert write_calls[0]["text"] == "/exit\n"

        # Now simulate SessionEnd event to resolve the future
        await registry.handle_event("SessionEnd", {"session_id": "sess-abc"})

        # Wait for stop_task to complete
        stopped = await stop_task_handle

        # Should return True (stopped cleanly)
        assert stopped is True

        # Task should be marked as stopped
        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.status == "stopped"

        # Thread should be archived. Note: both stop_task (_mark_stopped) and
        # SessionEnd handler (_on_session_end) may archive; archive_thread is idempotent.
        archive_calls = fake_bot.get_archive_calls()
        assert len(archive_calls) >= 1
        assert archive_calls[0]["thread_id"] == 999

    async def test_stop_task_timeout_returns_false_and_marks_stopped(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch
    ) -> None:
        """stop_task with timeout returns False if SessionEnd doesn't arrive; still marks stopped."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            zellij_pane_id="terminal_1",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        async def mock_write_to_pane(pane_id: str, text: str) -> None:
            pass

        monkeypatch.setattr(fake_zellij, "write_to_pane", mock_write_to_pane)

        # stop_task with very short timeout
        stopped = await registry.stop_task("task-123", timeout=0.1)

        # Should return False (timed out)
        assert stopped is False

        # Task should still be marked as stopped
        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.status == "stopped"

    async def test_stop_task_with_no_pane_marks_stopped(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """stop_task when pane_id is None (mid-spawn) immediately marks stopped."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "spawning",
            zellij_pane_id=None,
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        stopped = await registry.stop_task("task-123")

        # Should return True (fail-safe stop)
        assert stopped is True

        # Task should be marked as stopped
        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.status == "stopped"

    async def test_stop_task_unknown_task_raises(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """stop_task with unknown task_id raises TaskNotFound."""
        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)

        from bridge.tasks import TaskNotFound

        with pytest.raises(TaskNotFound):
            await registry.stop_task("unknown")

    async def test_kill_task_closes_pane_and_marks_crashed(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch
    ) -> None:
        """kill_task closes the pane and marks status='crashed'."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            zellij_pane_id="terminal_1",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        close_calls = []

        async def mock_close_pane(pane_id: str) -> None:
            close_calls.append({"pane_id": pane_id})

        monkeypatch.setattr(fake_zellij, "close_pane", mock_close_pane)

        await registry.kill_task("task-123")

        # Should have called close_pane
        assert len(close_calls) == 1
        assert close_calls[0]["pane_id"] == "terminal_1"

        # Task should be marked as crashed
        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.status == "crashed"

    async def test_kill_task_unknown_task_raises(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """kill_task with unknown task_id raises TaskNotFound."""
        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)

        from bridge.tasks import TaskNotFound

        with pytest.raises(TaskNotFound):
            await registry.kill_task("unknown")

    async def test_stop_task_removes_from_thread_and_session_indexes(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch
    ) -> None:
        """After stop_task, get_by_thread_id and get_by_session_id return None."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            zellij_pane_id="terminal_1",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Verify task is in indexes before stop
        assert registry.get_by_thread_id(999) is not None
        assert registry.get_by_session_id("sess-abc") is not None

        # Mock SessionEnd to complete the stop
        loop = asyncio.get_running_loop()
        async def mock_write_to_pane(pane_id: str, text: str) -> None:
            # Trigger SessionEnd immediately
            loop.call_soon(lambda: asyncio.create_task(registry._on_session_end({"session_id": "sess-abc"})))

        monkeypatch.setattr(fake_zellij, "write_to_pane", mock_write_to_pane)

        await registry.stop_task("task-123")

        # After stop, task should not be in indexes
        assert registry.get_by_thread_id(999) is None
        assert registry.get_by_session_id("sess-abc") is None
        # But still findable by task_id
        assert registry.get_by_task_id("task-123") is not None

    async def test_kill_task_removes_from_thread_and_session_indexes(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """After kill_task, get_by_thread_id and get_by_session_id return None."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            zellij_pane_id="terminal_1",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Verify task is in indexes before kill
        assert registry.get_by_thread_id(999) is not None
        assert registry.get_by_session_id("sess-abc") is not None

        await registry.kill_task("task-123")

        # After kill, task should not be in indexes
        assert registry.get_by_thread_id(999) is None
        assert registry.get_by_session_id("sess-abc") is None
        # But still findable by task_id
        assert registry.get_by_task_id("task-123") is not None

    async def test_stop_task_cleans_up_typing_task_and_aggregator(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch
    ) -> None:
        """stop_task removes typing task and flushes aggregator."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            zellij_pane_id="terminal_1",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Start typing and add a tool summary
        await registry._on_user_prompt_submit({"session_id": "sess-abc"})
        await asyncio.sleep(0)

        # Verify typing task exists
        assert "task-123" in registry._typing_tasks

        # Add a tool summary
        line = "✓ Bash: echo hello"
        registry._agg_for(registry.get_by_task_id("task-123")).append(line)
        assert "task-123" in registry._aggregators

        # Mock SessionEnd to complete the stop
        loop = asyncio.get_running_loop()

        async def mock_write_to_pane(pane_id: str, text: str) -> None:
            loop.call_soon(lambda: asyncio.create_task(registry._on_session_end({"session_id": "sess-abc"})))

        monkeypatch.setattr(fake_zellij, "write_to_pane", mock_write_to_pane)

        await registry.stop_task("task-123")

        # Verify typing task was cleaned up
        assert "task-123" not in registry._typing_tasks
        # Verify aggregator was cleaned up (and flushed)
        assert "task-123" not in registry._aggregators
        # Verify the tool summary was posted
        posts = fake_bot.get_post_calls()
        assert any(line in post["content"] for post in posts)

    async def test_kill_task_cleans_up_typing_task_and_aggregator(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """kill_task removes typing task and drops aggregator without flushing."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            zellij_pane_id="terminal_1",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Start typing and add a tool summary
        await registry._on_user_prompt_submit({"session_id": "sess-abc"})
        await asyncio.sleep(0)

        # Verify typing task exists
        assert "task-123" in registry._typing_tasks

        # Add a tool summary
        line = "✗ Bash: false (exit 1)"
        registry._agg_for(registry.get_by_task_id("task-123")).append(line)
        assert "task-123" in registry._aggregators

        # Record initial post count
        initial_posts = len(fake_bot.get_post_calls())

        await registry.kill_task("task-123")

        # Verify typing task was cleaned up
        assert "task-123" not in registry._typing_tasks
        # Verify aggregator was cleaned up (dropped without flushing)
        assert "task-123" not in registry._aggregators
        # Verify no new posts (aggregator was dropped, not flushed)
        assert len(fake_bot.get_post_calls()) == initial_posts

    async def test_restart_task_with_live_pane_writes_resume_command(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch
    ) -> None:
        """restart_task with live pane writes claude --resume command."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            zellij_pane_id="terminal_1",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        write_calls = []
        pane_list = [{"id": "terminal_1", "title": "", "pwd": "/tmp", "terminal_command": "claude", "exited": False}]

        async def mock_write_to_pane(pane_id: str, text: str) -> None:
            write_calls.append({"pane_id": pane_id, "text": text})

        async def mock_list_panes() -> list:
            return pane_list

        monkeypatch.setattr(fake_zellij, "write_to_pane", mock_write_to_pane)
        monkeypatch.setattr(fake_zellij, "list_panes", mock_list_panes)

        task = await registry.restart_task("task-123")

        # Should have written the resume command
        assert len(write_calls) == 1
        assert "claude --resume sess-abc" in write_calls[0]["text"]

        # Task should still be running
        assert task.status == "running"

    async def test_restart_task_with_dead_pane_spawns_new_pane(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch
    ) -> None:
        """restart_task with dead pane spawns a new pane with extra_argv."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            zellij_pane_id="terminal_1",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        pane_list = []  # Empty — no panes alive

        async def mock_list_panes() -> list:
            return pane_list

        spawn_calls = []

        async def mock_spawn_task(
            cwd: str, env: dict, pane_name: str, extra_argv: list | None = None
        ) -> str:
            spawn_calls.append(
                {"cwd": cwd, "env": env, "pane_name": pane_name, "extra_argv": extra_argv}
            )
            return "terminal_2"

        monkeypatch.setattr(fake_zellij, "list_panes", mock_list_panes)
        monkeypatch.setattr(fake_zellij, "spawn_task", mock_spawn_task)

        task = await registry.restart_task("task-123")

        # Should have spawned new pane with extra_argv
        assert len(spawn_calls) == 1
        assert spawn_calls[0]["cwd"] == "/tmp"
        # extra_argv now includes both --settings and --resume
        assert "--settings" in spawn_calls[0]["extra_argv"]
        assert "--resume" in spawn_calls[0]["extra_argv"]
        assert "sess-abc" in spawn_calls[0]["extra_argv"]

        # Task pane should be updated
        assert task.zellij_pane_id == "terminal_2"

    async def test_restart_task_no_session_id_raises(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """restart_task with no claude session_id raises TaskRestartError."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            zellij_pane_id="terminal_1",
            current_claude_session_id=None,
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        from bridge.tasks import TaskRestartError

        with pytest.raises(TaskRestartError):
            await registry.restart_task("task-123")

    async def test_restart_task_unknown_task_raises(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """restart_task with unknown task_id raises TaskNotFound."""
        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)

        from bridge.tasks import TaskNotFound

        with pytest.raises(TaskNotFound):
            await registry.restart_task("unknown")

    async def test_on_session_end_resolves_stop_future(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch
    ) -> None:
        """_on_session_end resolves a pending stop_future when task matches."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            zellij_pane_id="terminal_1",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Manually set up a pending stop_future
        import asyncio

        fut = asyncio.get_running_loop().create_future()
        registry._stop_futures["task-123"] = fut

        # Trigger SessionEnd
        await registry._on_session_end({"session_id": "sess-abc"})

        # Future should be resolved
        assert fut.done()

    async def test_write_initial_prompt_happy_path(
        self, in_memory_db
    ) -> None:
        """write_initial_prompt writes text to pane and bumps last_activity."""
        # Avoid fixture reuse issues with inline instantiation
        # FakeBot and FakeZellij already imported at top

        fake_zellij = FakeZellij()
        fake_bot = FakeDiscordBot()

        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "spawning",
            zellij_pane_id="terminal_1",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        task = registry.get_by_task_id("task-123")
        assert task is not None
        old_activity = task.last_activity

        # Write prompt
        await registry.write_initial_prompt("task-123", "hello world")

        # Verify write_to_pane was called
        assert len(fake_zellij._write_calls) == 1
        call = fake_zellij._write_calls[0]
        assert call["pane_id"] == "terminal_1"
        assert "hello world\n" in call["text"]

        # Verify last_activity was bumped
        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.last_activity > old_activity

    async def test_write_initial_prompt_missing_task(
        self, in_memory_db
    ) -> None:
        """write_initial_prompt logs warning if task is missing."""
        # FakeBot and FakeZellij already imported at top

        registry = TaskRegistry(in_memory_db, FakeDiscordBot(), FakeZellij())

        # Attempt to write to non-existent task
        await registry.write_initial_prompt("nonexistent", "hello")

        # Should not raise, just log

    async def test_stop_task_already_stopped_is_idempotent(
        self, in_memory_db
    ) -> None:
        """stop_task returns True immediately if task is already stopped."""
        # FakeBot and FakeZellij already imported at top

        fake_zellij = FakeZellij()
        now = 1000

        # Create a running task first
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            zellij_pane_id="terminal_1",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, FakeDiscordBot(), fake_zellij)
        await registry.load_from_db()

        # Manually set status to stopped
        task = registry.get_by_task_id("task-123")
        assert task is not None
        task.status = "stopped"

        # Stop should return True (idempotent, no-op)
        result = await registry.stop_task("task-123")
        assert result is True

        # Verify no write_to_pane was called
        assert len(fake_zellij._write_calls) == 0

    async def test_kill_task_already_crashed_is_idempotent(
        self, in_memory_db
    ) -> None:
        """kill_task returns None immediately if task is already crashed."""
        # FakeBot and FakeZellij already imported at top

        fake_zellij = FakeZellij()
        now = 1000

        # Create a running task first
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            zellij_pane_id="terminal_1",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, FakeDiscordBot(), fake_zellij)
        await registry.load_from_db()

        # Manually set status to crashed
        task = registry.get_by_task_id("task-123")
        assert task is not None
        task.status = "crashed"

        # Kill should return None (idempotent, no-op)
        await registry.kill_task("task-123")

        # Verify no close_pane was called
        assert len(fake_zellij._close_calls) == 0

    async def test_restart_task_bumps_activity_when_live_pane(
        self, in_memory_db
    ) -> None:
        """restart_task with live pane updates last_activity."""
        # Import fixtures inline to avoid import issues
        # FakeBot and FakeZellij already imported at top

        fake_zellij = FakeZellij()
        fake_bot = FakeDiscordBot()

        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            zellij_pane_id="terminal_1",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        task = registry.get_by_task_id("task-123")
        assert task is not None
        old_activity = task.last_activity

        # Mock list_panes to return pane as alive
        async def mock_list_panes():
            return [{"id": "terminal_1", "exited": False, "title": "test"}]

        fake_zellij.list_panes = mock_list_panes

        # Restart
        await registry.restart_task("task-123")

        # Verify last_activity was bumped
        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.last_activity > old_activity


    async def test_on_user_prompt_submit_starts_typing(self, in_memory_db) -> None:
        """_on_user_prompt_submit starts typing indicator."""
        fake_bot = FakeDiscordBot()
        fake_zellij = FakeZellij()
        now = 1000

        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Dispatch UserPromptSubmit
        await registry._on_user_prompt_submit({"session_id": "sess-abc"})

        # Verify typing task was created
        assert "task-123" in registry._typing_tasks
        task_obj = registry._typing_tasks["task-123"]
        assert not task_obj.done()

        # Clean up
        await registry._stop_typing("task-123")
        await asyncio.sleep(0.01)

    async def test_on_stop_cancels_typing(self, in_memory_db) -> None:
        """_on_stop cancels the typing indicator."""
        fake_bot = FakeDiscordBot()
        fake_zellij = FakeZellij()
        now = 1000

        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Start typing
        await registry._on_user_prompt_submit({"session_id": "sess-abc"})
        assert "task-123" in registry._typing_tasks
        assert not registry._typing_tasks["task-123"].done()

        # Stop
        await registry._on_stop({"session_id": "sess-abc"})
        await asyncio.sleep(0.01)

        # Verify typing was cancelled
        assert "task-123" not in registry._typing_tasks

    async def test_on_notification_cancels_typing(self, in_memory_db) -> None:
        """_on_notification cancels the typing indicator."""
        fake_bot = FakeDiscordBot()
        fake_zellij = FakeZellij()
        now = 1000

        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Start typing
        await registry._on_user_prompt_submit({"session_id": "sess-abc"})
        assert "task-123" in registry._typing_tasks

        # Notification
        await registry._on_notification({"session_id": "sess-abc"})
        await asyncio.sleep(0.01)

        # Verify typing was cancelled
        assert "task-123" not in registry._typing_tasks

    async def test_on_session_end_cancels_typing(self, in_memory_db) -> None:
        """_on_session_end cancels the typing indicator."""
        fake_bot = FakeDiscordBot()
        fake_zellij = FakeZellij()
        now = 1000

        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Start typing
        await registry._on_user_prompt_submit({"session_id": "sess-abc"})
        assert "task-123" in registry._typing_tasks

        # SessionEnd
        await registry._on_session_end({"session_id": "sess-abc"})
        await asyncio.sleep(0.01)

        # Verify typing was cancelled
        assert "task-123" not in registry._typing_tasks

    async def test_typing_context_manager_is_entered_and_exited(self, in_memory_db) -> None:
        """Verify that channel.typing() context manager is actually entered and exited."""
        fake_bot = FakeDiscordBot()
        fake_zellij = FakeZellij()
        now = 1000

        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Start typing
        await registry._on_user_prompt_submit({"session_id": "sess-abc"})
        # Yield to let _run_typing reach the async with line
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Verify typing context was entered
        thread_id = 999
        typing_context = fake_bot._fake_channels[thread_id].typing_context
        assert typing_context.entered is True
        assert typing_context.exited is False

        # Stop typing
        await registry._stop_typing("task-123")
        await asyncio.sleep(0)

        # Verify typing context was exited
        assert typing_context.exited is True

    async def test_on_user_prompt_submit_unknown_session_is_noop(self, in_memory_db) -> None:
        """_on_user_prompt_submit silently drops unknown session IDs."""
        fake_bot = FakeDiscordBot()
        fake_zellij = FakeZellij()

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Dispatch for unknown session
        await registry._on_user_prompt_submit({"session_id": "unknown"})

        # No crash, no typing task created
        assert len(registry._typing_tasks) == 0

    async def test_on_user_prompt_submit_twice_replaces_typing_task(
        self, in_memory_db
    ) -> None:
        """Calling _on_user_prompt_submit twice cancels the first typing task."""
        fake_bot = FakeDiscordBot()
        fake_zellij = FakeZellij()
        now = 1000

        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # First submit
        await registry._on_user_prompt_submit({"session_id": "sess-abc"})
        first_task = registry._typing_tasks["task-123"]
        assert not first_task.done()

        # Second submit (should cancel first)
        await registry._on_user_prompt_submit({"session_id": "sess-abc"})
        await asyncio.sleep(0.01)

        # First task should be done/cancelled
        assert first_task.done()

        # New task should exist
        second_task = registry._typing_tasks["task-123"]
        assert not second_task.done()
        assert second_task is not first_task

        # Clean up
        await registry._stop_typing("task-123")

    async def test_on_post_tool_use_appends_to_aggregator(self, in_memory_db) -> None:
        """_on_post_tool_use appends formatted summary to aggregator."""
        fake_bot = FakeDiscordBot()
        fake_zellij = FakeZellij()
        now = 1000

        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Dispatch PostToolUse
        await registry._on_post_tool_use({
            "session_id": "sess-abc",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -q"},
            "tool_response": {"exit_code": 0},
        })

        # Verify aggregator has the line
        agg = registry._aggregators.get("task-123")
        assert agg is not None
        assert len(agg._lines) == 1
        assert "Bash" in agg._lines[0]
        assert "pytest" in agg._lines[0]

    async def test_on_post_tool_use_failure(self, in_memory_db) -> None:
        """_on_post_tool_use_failure appends failure summary and updates task."""
        fake_bot = FakeDiscordBot()
        fake_zellij = FakeZellij()
        now = 1000

        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        task = registry.get_by_task_id("task-123")
        old_activity = task.last_activity

        # Dispatch PostToolUseFailure (forces failure regardless of tool_response.is_error)
        await registry._on_post_tool_use_failure({
            "session_id": "sess-abc",
            "tool_name": "Bash",
            "tool_input": {"command": "exit 0"},
            "tool_response": {"exit_code": 0},  # exit_code says success, but we force failure
        })

        # Verify aggregator has the failure line
        agg = registry._aggregators.get("task-123")
        assert agg is not None
        assert len(agg._lines) == 1
        # Should have the failure emoji
        assert "✗" in agg._lines[0]
        assert "Bash" in agg._lines[0]

        # Verify task's last_activity was updated
        task_refreshed = registry.get_by_task_id("task-123")
        assert task_refreshed.last_activity > old_activity

    async def test_aggregator_coalesces_within_window(self, in_memory_db, monkeypatch) -> None:
        """Multiple PostToolUse events within 1s window produce one post."""
        fake_bot = FakeDiscordBot()
        fake_zellij = FakeZellij()
        now = 1000

        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        # Override the aggregator flush window to be very short for testing
        monkeypatch.setattr(_ToolSummaryAggregator, "FLUSH_WINDOW", 0.05)
        await registry.load_from_db()

        # Dispatch two PostToolUse within the window
        await registry._on_post_tool_use({
            "session_id": "sess-abc",
            "tool_name": "Bash",
            "tool_input": {"command": "cmd1"},
            "tool_response": {"exit_code": 0},
        })
        await registry._on_post_tool_use({
            "session_id": "sess-abc",
            "tool_name": "Bash",
            "tool_input": {"command": "cmd2"},
            "tool_response": {"exit_code": 0},
        })

        # Wait for flush window to pass
        await asyncio.sleep(0.1)

        # Verify only one post was made (two lines in it)
        posts = fake_bot.get_post_calls()
        assert len(posts) == 1
        post_body = posts[0]["content"]
        assert "cmd1" in post_body
        assert "cmd2" in post_body
        # Should have two lines separated by newline
        lines = post_body.split("\n")
        assert len(lines) >= 2

    async def test_on_stop_flushes_aggregator(self, in_memory_db, monkeypatch) -> None:
        """_on_stop immediately flushes the aggregator."""
        fake_bot = FakeDiscordBot()
        fake_zellij = FakeZellij()
        now = 1000

        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        monkeypatch.setattr(_ToolSummaryAggregator, "FLUSH_WINDOW", 10.0)  # Very long window
        await registry.load_from_db()

        # Add a tool summary
        await registry._on_post_tool_use({
            "session_id": "sess-abc",
            "tool_name": "Bash",
            "tool_input": {"command": "test"},
            "tool_response": {"exit_code": 0},
        })

        # At this point, the flush should be pending (hasn't fired yet)
        agg = registry._aggregators["task-123"]
        assert len(agg._lines) == 1

        # Now call Stop, which should flush immediately
        await registry._on_stop({"session_id": "sess-abc"})

        # Verify the post was made immediately
        posts = fake_bot.get_post_calls()
        assert len(posts) == 1
        assert "test" in posts[0]["content"]

    async def test_aggregator_preserves_lines_on_cancellation(self, in_memory_db) -> None:
        """_flush_after_window preserves lines if cancelled during bot.post."""
        fake_bot = FakeDiscordBot()

        agg = _ToolSummaryAggregator(fake_bot, 999)
        agg._lines = ["✓ Bash: echo test"]

        # Mock bot.post to be slow so we can cancel during it
        post_started = asyncio.Event()
        post_should_finish = asyncio.Event()

        async def slow_post(content: str, *, thread_id: int | None = None) -> list[int]:
            post_started.set()
            try:
                await post_should_finish.wait()
            except asyncio.CancelledError:
                raise
            return [1001]

        fake_bot.post = slow_post

        # Start the flush and let it reach the post
        flush_handle = asyncio.create_task(agg._flush_after_window())
        await asyncio.sleep(0)  # Let it start sleeping

        # Manually trigger the flush by skipping the sleep
        # (In real usage, we'd wait for FLUSH_WINDOW to pass)
        # Instead, create a new flush task that doesn't sleep
        async def flush_without_sleep():
            if not agg._lines:
                return
            local_lines = list(agg._lines)
            agg._lines.clear()
            body = "\n".join(local_lines)
            try:
                await agg._bot.post(body, thread_id=agg._thread_id)
            except asyncio.CancelledError:
                agg._lines[:0] = local_lines
                raise
            except Exception:
                pass

        flush_handle2 = asyncio.create_task(flush_without_sleep())
        await post_started.wait()

        # Now cancel (simulating flush_now during post)
        flush_handle2.cancel()
        try:
            await flush_handle2
        except asyncio.CancelledError:
            pass

        # Verify lines were restored
        assert len(agg._lines) == 1
        assert "echo test" in agg._lines[0]

        # Cleanup
        post_should_finish.set()
        flush_handle.cancel()
        try:
            await flush_handle
        except asyncio.CancelledError:
            pass

    async def test_on_stop_posts_final_assistant_turn(self, in_memory_db, tmp_path) -> None:
        """_on_stop streams every assistant text block from the current turn."""
        import json

        fake_bot = FakeDiscordBot()
        fake_zellij = FakeZellij()
        now = 1000

        # Create a temporary transcript file. Each assistant entry needs a
        # uuid because the streamer dedupes on it.
        transcript_path = tmp_path / "transcript.jsonl"
        entries = [
            {
                "type": "user",
                "uuid": "u-prompt",
                "message": {"role": "user", "content": "hi"},
                "isSidechain": False,
                "isMeta": False,
            },
            {
                "type": "assistant",
                "uuid": "a-1",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Hello there"},
                        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
                    ],
                },
                "isSidechain": False,
                "isMeta": False,
            },
            {
                "type": "assistant",
                "uuid": "a-2",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "All done"}],
                },
                "isSidechain": False,
                "isMeta": False,
            },
        ]
        with open(transcript_path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-abc",
            current_transcript_path=str(transcript_path),
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        # Skip the file-stabilization wait for the test's static transcript.
        registry._STOP_TRANSCRIPT_RETRY_SECS = 0.0
        await registry.load_from_db()

        # Call Stop with transcript_path in body
        await registry._on_stop({
            "session_id": "sess-abc",
            "transcript_path": str(transcript_path),
        })

        # Each text block becomes its own Discord post (live-streaming
        # behavior); tool_use blocks are not posted here.
        posts = fake_bot.get_post_calls()
        assert [p["content"] for p in posts] == ["Hello there", "All done"]

    async def test_on_stop_does_not_post_empty_transcript(self, in_memory_db, tmp_path) -> None:
        """_on_stop does not post when transcript has no assistant text."""
        import json

        fake_bot = FakeDiscordBot()
        fake_zellij = FakeZellij()
        now = 1000

        # Create a transcript with only a user prompt (no assistant response)
        transcript_path = tmp_path / "transcript.jsonl"
        entries = [
            {
                "type": "user",
                "message": {"role": "user", "content": "hi"},
                "isSidechain": False,
                "isMeta": False,
            },
        ]
        with open(transcript_path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        # Disable the Stop transcript-flush retry — this test verifies the
        # empty-text branch and shouldn't pay the production retry budget.
        registry._STOP_TRANSCRIPT_RETRY_SECS = 0.0
        await registry.load_from_db()

        # Call Stop with transcript_path
        await registry._on_stop({
            "session_id": "sess-abc",
            "transcript_path": str(transcript_path),
        })

        # Verify no post was made (only empty content is skipped)
        posts = fake_bot.get_post_calls()
        assert len(posts) == 0

    async def test_on_stop_flushes_before_final_post(self, in_memory_db, tmp_path, monkeypatch) -> None:
        """_on_stop flushes tool summaries before posting the final turn."""
        import json

        fake_bot = FakeDiscordBot()
        fake_zellij = FakeZellij()
        now = 1000

        # Create a transcript with final assistant text. uuid is required so
        # the streamer dedupes properly.
        transcript_path = tmp_path / "transcript.jsonl"
        entries = [
            {
                "type": "user",
                "uuid": "u-prompt",
                "message": {"role": "user", "content": "run test"},
                "isSidechain": False,
                "isMeta": False,
            },
            {
                "type": "assistant",
                "uuid": "a-1",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Final response"}],
                },
                "isSidechain": False,
                "isMeta": False,
            },
        ]
        with open(transcript_path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-abc",
            current_transcript_path=str(transcript_path),
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        registry._STOP_TRANSCRIPT_RETRY_SECS = 0.0
        monkeypatch.setattr(_ToolSummaryAggregator, "FLUSH_WINDOW", 10.0)  # Very long window
        await registry.load_from_db()

        # Add a tool summary (with a long flush window, it won't auto-flush).
        # PostToolUse will also stream the assistant text written so far.
        await registry._on_post_tool_use({
            "session_id": "sess-abc",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest"},
            "tool_response": {"exit_code": 0},
        })

        agg = registry._aggregators["task-123"]
        assert len(agg._lines) == 1  # Pending

        # PostToolUse already streamed "Final response"; the aggregator hasn't
        # flushed yet (long window).
        assert [p["content"] for p in fake_bot.get_post_calls()] == ["Final response"]

        # Call Stop — flushes the aggregated tool summary; the assistant
        # entry is already posted, so streaming is a no-op.
        await registry._on_stop({
            "session_id": "sess-abc",
            "transcript_path": str(transcript_path),
        })

        posts = fake_bot.get_post_calls()
        assert len(posts) == 2
        assert posts[0]["content"] == "Final response"
        assert "pytest" in posts[1]["content"]

    async def test_on_stop_handles_missing_transcript_path(self, in_memory_db) -> None:
        """_on_stop does not crash if transcript_path is missing."""
        fake_bot = FakeDiscordBot()
        fake_zellij = FakeZellij()
        now = 1000

        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Call Stop without transcript_path
        await registry._on_stop({"session_id": "sess-abc"})

        # Should not crash and should not post
        posts = fake_bot.get_post_calls()
        assert len(posts) == 0

    async def test_on_stop_handles_nonexistent_transcript(self, in_memory_db) -> None:
        """_on_stop does not crash if the transcript file doesn't exist."""
        fake_bot = FakeDiscordBot()
        fake_zellij = FakeZellij()
        now = 1000

        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Call Stop with a nonexistent file path
        await registry._on_stop({
            "session_id": "sess-abc",
            "transcript_path": "/nonexistent/transcript.jsonl",
        })

        # Should not crash and should not post
        posts = fake_bot.get_post_calls()
        assert len(posts) == 0


@pytest.mark.asyncio
async def test_on_notification_ask_user_question(in_memory_db, tmp_path):
    """_on_notification spawns handler task; resolving the TUI future injects write_to_pane."""
    from bridge.approvals import ApprovalRouter
    import json

    fake_bot = FakeDiscordBot()
    fake_zellij = FakeZellij()
    approval_router = ApprovalRouter(fake_bot, in_memory_db, tui_timeout=10.0)

    transcript_path = tmp_path / "transcript.jsonl"
    entries = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_ask",
                        "name": "AskUserQuestion",
                        "input": {
                            "questions": [
                                {
                                    "question": "Which option?",
                                    "options": [
                                        {"label": "A", "description": "Option A"},
                                        {"label": "B", "description": "Option B"},
                                    ],
                                }
                            ]
                        },
                    }
                ],
            },
            "isSidechain": False,
            "isMeta": False,
        }
    ]
    with open(transcript_path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    await upsert_task(
        in_memory_db,
        "task-tui-1",
        3001,
        "/tmp",
        "running",
        zellij_pane_id="pane_1",
        current_claude_session_id="sess-tui-1",
        current_transcript_path=str(transcript_path),
    )

    registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij, approval_router)
    registry._PRE_PROMPT_FLUSH_SECS = 0.0
    await registry.load_from_db()

    # Trigger notification (returns immediately, spawns handler task)
    await registry._on_notification({
        "session_id": "sess-tui-1",
        "transcript_path": str(transcript_path),
    })

    # Brief wait for handler to register the pending TUI
    await asyncio.sleep(0.05)

    # Verify post was made
    posts = fake_bot.get_post_calls()
    assert len(posts) > 0
    assert "Which option?" in posts[0]["content"]

    # Now resolve the TUI by reaction (option 1 = emoji "1️⃣")
    message_id = approval_router._tui_by_message_id
    assert len(message_id) > 0
    msg_id = list(message_id.keys())[0]
    resolved = await approval_router.resolve_tui_by_reaction(msg_id, "1️⃣", user_is_bot=False)
    assert resolved is True

    # Grab and await the handler task
    handler_task = registry._tui_handler_tasks.get("task-tui-1")
    assert handler_task is not None
    await handler_task

    # Verify write_to_pane was called with "1\n"
    assert len(fake_zellij._write_calls) == 1
    assert fake_zellij._write_calls[0]["pane_id"] == "pane_1"
    assert fake_zellij._write_calls[0]["text"] == "1\n"


@pytest.mark.asyncio
async def test_on_notification_exit_plan_mode(in_memory_db, tmp_path):
    """_on_notification spawns handler task; resolving the TUI future injects write_to_pane."""
    from bridge.approvals import ApprovalRouter
    import json

    fake_bot = FakeDiscordBot()
    fake_zellij = FakeZellij()
    approval_router = ApprovalRouter(fake_bot, in_memory_db, tui_timeout=10.0)

    transcript_path = tmp_path / "transcript.jsonl"
    entries = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_exit",
                        "name": "ExitPlanMode",
                        "input": {
                            "plan": "## Step 1\n## Step 2",
                        },
                    }
                ],
            },
            "isSidechain": False,
            "isMeta": False,
        }
    ]
    with open(transcript_path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    await upsert_task(
        in_memory_db,
        "task-tui-2",
        3002,
        "/tmp",
        "running",
        zellij_pane_id="pane_2",
        current_claude_session_id="sess-tui-2",
        current_transcript_path=str(transcript_path),
    )

    registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij, approval_router)
    registry._PRE_PROMPT_FLUSH_SECS = 0.0
    await registry.load_from_db()

    # Trigger notification (returns immediately, spawns handler task)
    await registry._on_notification({
        "session_id": "sess-tui-2",
        "transcript_path": str(transcript_path),
    })

    # Brief wait for handler to register the pending TUI
    await asyncio.sleep(0.05)

    # Verify post contains plan body
    posts = fake_bot.get_post_calls()
    assert len(posts) > 0
    assert "Plan ready for review" in posts[0]["content"]

    # Resolve the TUI by reaction (approve = "✅" → "1")
    message_id = approval_router._tui_by_message_id
    assert len(message_id) > 0
    msg_id = list(message_id.keys())[0]
    resolved = await approval_router.resolve_tui_by_reaction(msg_id, "✅", user_is_bot=False)
    assert resolved is True

    # Grab and await the handler task
    handler_task = registry._tui_handler_tasks.get("task-tui-2")
    assert handler_task is not None
    await handler_task

    # Verify write_to_pane was called with "1\n"
    assert len(fake_zellij._write_calls) == 1
    assert fake_zellij._write_calls[0]["pane_id"] == "pane_2"
    assert fake_zellij._write_calls[0]["text"] == "1\n"


@pytest.mark.asyncio
async def test_on_notification_free_text_stall(in_memory_db, tmp_path):
    """_on_notification spawns handler task for free-text stall; resolving injects text."""
    from bridge.approvals import ApprovalRouter
    import json

    fake_bot = FakeDiscordBot()
    fake_zellij = FakeZellij()
    approval_router = ApprovalRouter(fake_bot, in_memory_db, tui_timeout=10.0)

    transcript_path = tmp_path / "transcript.jsonl"
    entries = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Waiting..."}
                ],
            },
            "isSidechain": False,
            "isMeta": False,
        }
    ]
    with open(transcript_path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    await upsert_task(
        in_memory_db,
        "task-tui-3",
        3003,
        "/tmp",
        "running",
        zellij_pane_id="pane_3",
        current_claude_session_id="sess-tui-3",
        current_transcript_path=str(transcript_path),
    )

    registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij, approval_router)
    await registry.load_from_db()

    # Trigger notification (returns immediately, spawns handler task)
    await registry._on_notification({
        "session_id": "sess-tui-3",
        "transcript_path": str(transcript_path),
    })

    # Brief wait for handler to register the pending TUI
    await asyncio.sleep(0.05)

    # Verify generic waiting notice was posted
    posts = fake_bot.get_post_calls()
    assert len(posts) > 0
    assert "waiting for input" in posts[0]["content"]

    # Resolve by text reply
    resolved = await approval_router.resolve_tui_by_text(3003, "my answer", author_is_bot=False)
    assert resolved is True

    # Grab and await the handler task
    handler_task = registry._tui_handler_tasks.get("task-tui-3")
    assert handler_task is not None
    await handler_task

    # Verify write_to_pane was called with the text
    assert len(fake_zellij._write_calls) == 1
    assert fake_zellij._write_calls[0]["pane_id"] == "pane_3"
    assert fake_zellij._write_calls[0]["text"] == "my answer\n"


@pytest.mark.asyncio
async def test_on_user_prompt_submit_cancels_tui(in_memory_db, tmp_path):
    """_on_user_prompt_submit cancels pending TUI prompts via sentinel."""
    from bridge.approvals import ApprovalRouter

    fake_bot = FakeDiscordBot()
    fake_zellij = FakeZellij()
    approval_router = ApprovalRouter(fake_bot, in_memory_db, tui_timeout=10.0)

    await upsert_task(
        in_memory_db,
        "task-tui-4",
        3004,
        "/tmp",
        "running",
        zellij_pane_id="pane_4",
        current_claude_session_id="sess-tui-4",
    )

    registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij, approval_router)
    await registry.load_from_db()

    # Start a pending TUI request
    async def create_tui_request():
        answer, source = await approval_router.request_tui_answer(
            request_id="req-tui-test",
            task_id="task-tui-4",
            thread_id=3004,
            pane_id="pane_4",
            kind="free_text",
            prompt_body="Waiting...",
            timeout=10.0,
        )
        return answer, source

    tui_task = asyncio.create_task(create_tui_request())

    # Let it register
    await asyncio.sleep(0.05)

    # Call _on_user_prompt_submit to cancel
    await registry._on_user_prompt_submit({
        "session_id": "sess-tui-4",
    })

    # The TUI request should resolve with ("", "cancelled") via sentinel
    result = await tui_task
    assert result == ("", "cancelled")

    # Verify no write_to_pane was called (cancelled means user answered in zellij, not Discord)
    assert len(fake_zellij._write_calls) == 0


@pytest.mark.asyncio
async def test_on_notification_returns_immediately_with_pending_tui(in_memory_db, tmp_path):
    """_on_notification returns immediately; handler tasks run in background."""
    from bridge.approvals import ApprovalRouter
    import json
    import time

    fake_bot = FakeDiscordBot()
    fake_zellij = FakeZellij()
    approval_router = ApprovalRouter(fake_bot, in_memory_db, tui_timeout=10.0)

    transcript_path = tmp_path / "transcript.jsonl"
    entries = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_ask",
                        "name": "AskUserQuestion",
                        "input": {
                            "questions": [
                                {
                                    "question": "Which?",
                                    "options": [
                                        {"label": "A", "description": ""},
                                    ],
                                }
                            ]
                        },
                    }
                ],
            },
            "isSidechain": False,
            "isMeta": False,
        }
    ]
    with open(transcript_path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    await upsert_task(
        in_memory_db,
        "task-async-1",
        4001,
        "/tmp",
        "running",
        zellij_pane_id="pane_async",
        current_claude_session_id="sess-async-1",
        current_transcript_path=str(transcript_path),
    )

    registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij, approval_router)
    await registry.load_from_db()

    # Record timing: _on_notification should return fast, even though the handler task
    # may be running concurrently.
    start = time.time()
    await registry._on_notification({
        "session_id": "sess-async-1",
        "transcript_path": str(transcript_path),
    })
    elapsed = time.time() - start

    # _on_notification should return promptly (< 50ms) since it just spawns a task
    assert elapsed < 0.05, f"_on_notification took {elapsed}s; expected < 0.05s"

    # Handler task should be registered
    handler_task = registry._tui_handler_tasks.get("task-async-1")
    assert handler_task is not None
    assert not handler_task.done()

    # Resolve and await to verify it completes
    await asyncio.sleep(0.05)
    message_id = approval_router._tui_by_message_id
    assert len(message_id) > 0
    msg_id = list(message_id.keys())[0]
    await approval_router.resolve_tui_by_reaction(msg_id, "1️⃣", user_is_bot=False)
    await handler_task


@pytest.mark.asyncio
async def test_kill_task_cancels_pending_tui_handler(in_memory_db, tmp_path):
    """kill_task cancels any pending TUI handler task."""
    from bridge.approvals import ApprovalRouter
    import json

    fake_bot = FakeDiscordBot()
    fake_zellij = FakeZellij()
    approval_router = ApprovalRouter(fake_bot, in_memory_db, tui_timeout=10.0)

    transcript_path = tmp_path / "transcript.jsonl"
    entries = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_ask",
                        "name": "AskUserQuestion",
                        "input": {
                            "questions": [
                                {
                                    "question": "Which?",
                                    "options": [
                                        {"label": "A", "description": ""},
                                    ],
                                }
                            ]
                        },
                    }
                ],
            },
            "isSidechain": False,
            "isMeta": False,
        }
    ]
    with open(transcript_path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    await upsert_task(
        in_memory_db,
        "task-kill-1",
        4002,
        "/tmp",
        "running",
        zellij_pane_id="pane_kill",
        current_claude_session_id="sess-kill-1",
        current_transcript_path=str(transcript_path),
    )

    registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij, approval_router)
    await registry.load_from_db()

    # Spawn a TUI handler via _on_notification
    await registry._on_notification({
        "session_id": "sess-kill-1",
        "transcript_path": str(transcript_path),
    })

    # Let handler register
    await asyncio.sleep(0.05)

    # Get the handler task before we kill
    handler_task = registry._tui_handler_tasks.get("task-kill-1")
    assert handler_task is not None
    assert not handler_task.done()

    # Kill the task
    await registry.kill_task("task-kill-1")

    # Handler task should be cancelled
    assert handler_task.done()
    assert handler_task.cancelled()

    # TUI handler tasks dict should be cleared
    assert "task-kill-1" not in registry._tui_handler_tasks


@pytest.mark.asyncio
async def test_race_cancel_before_request_registers(in_memory_db):
    """Race condition: cancel_thread_tui fires before request_tui_answer registers pending."""
    from bridge.approvals import ApprovalRouter

    fake_bot = FakeDiscordBot()
    approval_router = ApprovalRouter(fake_bot, in_memory_db, tui_timeout=10.0)

    thread_id = 5001

    # Simulate: cancel fires before request_tui_answer registers
    await approval_router.cancel_thread_tui(thread_id)

    # Now the request arrives
    answer, source = await approval_router.request_tui_answer(
        request_id="req-race-test",
        task_id="task-race-1",
        thread_id=thread_id,
        pane_id="pane_race",
        kind="free_text",
        prompt_body="Too late, already cancelled",
        timeout=10.0,
    )

    # Should resolve immediately with cancel sentinel
    assert answer == ""
    assert source == "cancelled"


@pytest.mark.asyncio
class TestTaskSettingsIntegration:
    """Tests for task-scoped settings file integration with spawn/kill/restart."""

    async def test_spawn_task_passes_settings_file_via_extra_argv(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch, tmp_path
    ) -> None:
        """spawn_task passes settings file to zellij via extra_argv."""
        from bridge import tasks as tasks_module
        
        # Replace both functions to use a test directory
        settings_dir = tmp_path / "settings"
        
        original_write = tasks_module._write_task_settings
        original_cleanup = tasks_module._cleanup_task_settings
        
        def patched_write(task_id: str, *, settings_dir_param=None, hooks_dir=None):
            return original_write(
                task_id,
                settings_dir=settings_dir,
                hooks_dir=hooks_dir or tasks_module.HOOKS_DIR
            )
        
        def patched_cleanup(task_id: str, *, settings_dir_param=None):
            return original_cleanup(task_id, settings_dir=settings_dir)
        
        monkeypatch.setattr(tasks_module, "_write_task_settings", patched_write)
        monkeypatch.setattr(tasks_module, "_cleanup_task_settings", patched_cleanup)

        captured_args = {}

        async def mock_spawn_task(
            cwd: str,
            env: dict[str, str],
            pane_name: str,
            extra_argv: list[str] | None = None,
        ) -> str:
            captured_args["extra_argv"] = extra_argv
            return "terminal_1"

        monkeypatch.setattr(fake_zellij, "spawn_task", mock_spawn_task)

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        task = await registry.spawn_task("/tmp")

        # Verify extra_argv includes --settings
        assert captured_args["extra_argv"] is not None
        assert len(captured_args["extra_argv"]) >= 2
        assert captured_args["extra_argv"][0] == "--settings"
        assert captured_args["extra_argv"][1].endswith(f"{task.task_id}.json")

    async def test_kill_task_cleans_up_settings_file(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch, tmp_path
    ) -> None:
        """kill_task removes the task-scoped settings file."""
        from bridge import tasks as tasks_module
        
        settings_dir = tmp_path / "settings"
        
        original_write = tasks_module._write_task_settings
        original_cleanup = tasks_module._cleanup_task_settings
        
        def patched_write(task_id: str, *, settings_dir_param=None, hooks_dir=None):
            return original_write(
                task_id,
                settings_dir=settings_dir,
                hooks_dir=hooks_dir or tasks_module.HOOKS_DIR
            )
        
        def patched_cleanup(task_id: str, *, settings_dir_param=None):
            return original_cleanup(task_id, settings_dir=settings_dir)
        
        monkeypatch.setattr(tasks_module, "_write_task_settings", patched_write)
        monkeypatch.setattr(tasks_module, "_cleanup_task_settings", patched_cleanup)

        async def mock_spawn_task(
            cwd: str,
            env: dict[str, str],
            pane_name: str,
            extra_argv: list[str] | None = None,
        ) -> str:
            return "terminal_1"

        monkeypatch.setattr(fake_zellij, "spawn_task", mock_spawn_task)

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        task = await registry.spawn_task("/tmp")

        # Verify settings file was created
        settings_file = settings_dir / f"{task.task_id}.json"
        assert settings_file.exists()

        # Kill the task
        await registry.kill_task(task.task_id)

        # Verify settings file was removed
        assert not settings_file.exists()

    async def test_mark_stopped_cleans_up_settings_file(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch, tmp_path
    ) -> None:
        """_mark_stopped removes the task-scoped settings file."""
        from bridge import tasks as tasks_module
        
        settings_dir = tmp_path / "settings"
        
        original_write = tasks_module._write_task_settings
        original_cleanup = tasks_module._cleanup_task_settings
        
        def patched_write(task_id: str, *, settings_dir_param=None, hooks_dir=None):
            return original_write(
                task_id,
                settings_dir=settings_dir,
                hooks_dir=hooks_dir or tasks_module.HOOKS_DIR
            )
        
        def patched_cleanup(task_id: str, *, settings_dir_param=None):
            return original_cleanup(task_id, settings_dir=settings_dir)
        
        monkeypatch.setattr(tasks_module, "_write_task_settings", patched_write)
        monkeypatch.setattr(tasks_module, "_cleanup_task_settings", patched_cleanup)

        async def mock_spawn_task(
            cwd: str,
            env: dict[str, str],
            pane_name: str,
            extra_argv: list[str] | None = None,
        ) -> str:
            return "terminal_1"

        monkeypatch.setattr(fake_zellij, "spawn_task", mock_spawn_task)

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        task = await registry.spawn_task("/tmp")

        # Verify settings file was created
        settings_file = settings_dir / f"{task.task_id}.json"
        assert settings_file.exists()

        # Mark as stopped (via _mark_stopped, which is called by stop_task)
        await registry._mark_stopped(task)

        # Verify settings file was removed
        assert not settings_file.exists()

    async def test_on_session_end_cleans_up_settings_file(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch, tmp_path
    ) -> None:
        """_on_session_end removes the task-scoped settings file."""
        from bridge import tasks as tasks_module
        
        settings_dir = tmp_path / "settings"
        
        original_write = tasks_module._write_task_settings
        original_cleanup = tasks_module._cleanup_task_settings
        
        def patched_write(task_id: str, *, settings_dir_param=None, hooks_dir=None):
            return original_write(
                task_id,
                settings_dir=settings_dir,
                hooks_dir=hooks_dir or tasks_module.HOOKS_DIR
            )
        
        def patched_cleanup(task_id: str, *, settings_dir_param=None):
            return original_cleanup(task_id, settings_dir=settings_dir)
        
        monkeypatch.setattr(tasks_module, "_write_task_settings", patched_write)
        monkeypatch.setattr(tasks_module, "_cleanup_task_settings", patched_cleanup)

        async def mock_spawn_task(
            cwd: str,
            env: dict[str, str],
            pane_name: str,
            extra_argv: list[str] | None = None,
        ) -> str:
            return "terminal_1"

        monkeypatch.setattr(fake_zellij, "spawn_task", mock_spawn_task)

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        task = await registry.spawn_task("/tmp")

        # Simulate SessionStart to set session_id
        await registry._on_session_start({
            "session_id": "sess-test-123",
            "transcript_path": "/tmp/transcript.md",
            "env_passthrough": {"CC_DISCORD_TASK_ID": task.task_id},
        })

        # Verify settings file was created
        settings_file = settings_dir / f"{task.task_id}.json"
        assert settings_file.exists()

        # Trigger SessionEnd event
        await registry._on_session_end({"session_id": "sess-test-123"})

        # Verify settings file was removed
        assert not settings_file.exists()

    async def test_spawn_task_cleanup_on_zellij_failure(
        self, fake_bot, fake_zellij, in_memory_db, monkeypatch, tmp_path
    ) -> None:
        """spawn_task cleans up settings file when zellij.spawn_task raises ZellijError."""
        from bridge import tasks as tasks_module
        from bridge.zellij import ZellijError

        settings_dir = tmp_path / "settings"

        original_write = tasks_module._write_task_settings
        original_cleanup = tasks_module._cleanup_task_settings

        def patched_write(task_id: str, *, settings_dir_param=None, hooks_dir=None):
            return original_write(
                task_id,
                settings_dir=settings_dir,
                hooks_dir=hooks_dir or tasks_module.HOOKS_DIR
            )

        def patched_cleanup(task_id: str, *, settings_dir_param=None):
            return original_cleanup(task_id, settings_dir=settings_dir)

        monkeypatch.setattr(tasks_module, "_write_task_settings", patched_write)
        monkeypatch.setattr(tasks_module, "_cleanup_task_settings", patched_cleanup)

        # Mock spawn_task to raise ZellijError
        async def mock_spawn_task(
            cwd: str,
            env: dict[str, str],
            pane_name: str,
            extra_argv: list[str] | None = None,
        ) -> str:
            raise ZellijError("spawn failed")

        monkeypatch.setattr(fake_zellij, "spawn_task", mock_spawn_task)

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)

        # Try to spawn, expect ZellijError (re-raised after cleanup)
        with pytest.raises(ZellijError):
            await registry.spawn_task("/tmp")

        # After the spawn failure, there should be no settings files left
        # (since the file was cleaned up)
        assert not list(settings_dir.glob("*.json")) or all(
            not f.exists() for f in settings_dir.glob("*.json")
        )

    async def test_on_session_end_abnormal_exit_removes_from_indexes(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """_on_session_end with abnormal exit removes task from _by_thread_id and _by_session_id."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "running",
            current_claude_session_id="sess-abc",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Verify task is indexed
        assert registry.get_by_thread_id(999) is not None
        assert registry.get_by_session_id("sess-abc") is not None

        # Trigger abnormal SessionEnd
        await registry._on_session_end({
            "session_id": "sess-abc",
            "exit_reason": "Error: process crashed",
        })

        # Verify task was removed from indexes
        assert registry.get_by_thread_id(999) is None
        assert registry.get_by_session_id("sess-abc") is None

    async def test_on_session_end_normal_exit_removes_from_indexes(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """_on_session_end with normal exit removes task from _by_thread_id and _by_session_id."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-456",
            888,
            "/tmp",
            "running",
            current_claude_session_id="sess-def",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Verify task is indexed
        assert registry.get_by_thread_id(888) is not None
        assert registry.get_by_session_id("sess-def") is not None

        # Trigger normal SessionEnd (graceful exit)
        await registry._on_session_end({
            "session_id": "sess-def",
            "exit_reason": "exit",
        })

        # Verify task was removed from indexes
        assert registry.get_by_thread_id(888) is None
        assert registry.get_by_session_id("sess-def") is None

    async def test_tui_handler_task_auto_cleanup_on_completion(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """_track_tui_handler_task removes entry when handler task completes."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-789",
            777,
            "/tmp",
            "running",
            current_claude_session_id="sess-ghi",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        task = registry.get_by_task_id("task-789")
        assert task is not None

        # Create a simple coroutine that completes immediately
        async def quick_handler():
            return "done"

        # Create and track a handler task
        handler_task = asyncio.create_task(quick_handler())
        registry._track_tui_handler_task(task.task_id, handler_task)

        # Verify task is tracked
        assert task.task_id in registry._tui_handler_tasks

        # Wait for the handler task to complete
        await handler_task

        # Yield control to allow the done_callback to fire
        await asyncio.sleep(0)

        # Verify task was auto-removed from tracking
        assert task.task_id not in registry._tui_handler_tasks


@pytest.mark.asyncio
class TestEnvVarBackwardCompatibility:
    """Test backward-compatible env var support for CC_BRIDGE_TASK_ID."""

    async def test_build_spawn_env_includes_both_task_id_vars(
        self, fake_bot, fake_zellij, in_memory_db
    ) -> None:
        """_build_spawn_env includes both CC_BRIDGE_TASK_ID and CC_DISCORD_TASK_ID."""
        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)

        env = registry._build_spawn_env("test-task-id")

        assert env["CC_BRIDGE_TASK_ID"] == "test-task-id"
        assert env["CC_DISCORD_TASK_ID"] == "test-task-id"
        assert "BRIDGE_URL" in env

    async def test_session_start_with_new_env_var(
        self, fake_bot, fake_zellij, in_memory_db, tmp_path
    ) -> None:
        """SessionStart event can use CC_BRIDGE_TASK_ID env var."""
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text("")

        # Pre-create the task
        await upsert_task(
            in_memory_db,
            "task-new-var",
            5001,
            "/tmp",
            "spawning",
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Use a valid UUID format for session_id
        session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeee0001"

        # Simulate SessionStart with new env var name
        await registry._on_session_start({
            "session_id": session_id,
            "transcript_path": str(transcript_path),
            "env_passthrough": {
                "CC_BRIDGE_TASK_ID": "task-new-var",  # New var name
                "CLAUDE_PROJECT_DIR": "/tmp",
            },
            "matcher": "startup",
        })

        # Task should be bound to the session
        task = registry.get_by_task_id("task-new-var")
        assert task is not None
        assert task.current_claude_session_id == session_id

    async def test_session_start_with_old_env_var(
        self, fake_bot, fake_zellij, in_memory_db, tmp_path
    ) -> None:
        """SessionStart event still works with CC_DISCORD_TASK_ID env var (backward compat)."""
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text("")

        # Pre-create the task
        await upsert_task(
            in_memory_db,
            "task-old-var",
            5002,
            "/tmp",
            "spawning",
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Use a valid UUID format for session_id
        session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeee0002"

        # Simulate SessionStart with old env var name
        await registry._on_session_start({
            "session_id": session_id,
            "transcript_path": str(transcript_path),
            "env_passthrough": {
                "CC_DISCORD_TASK_ID": "task-old-var",  # Old var name
                "CLAUDE_PROJECT_DIR": "/tmp",
            },
            "matcher": "startup",
        })

        # Task should still be bound to the session
        task = registry.get_by_task_id("task-old-var")
        assert task is not None
        assert task.current_claude_session_id == session_id

    async def test_session_start_prefers_new_over_old_env_var(
        self, fake_bot, fake_zellij, in_memory_db, tmp_path
    ) -> None:
        """SessionStart prefers CC_BRIDGE_TASK_ID when both vars are present."""
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text("")

        # Pre-create the task
        await upsert_task(
            in_memory_db,
            "task-new-var",
            5003,
            "/tmp",
            "spawning",
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Use a valid UUID format for session_id
        session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeee0003"

        # Simulate SessionStart with both vars present
        await registry._on_session_start({
            "session_id": session_id,
            "transcript_path": str(transcript_path),
            "env_passthrough": {
                "CC_BRIDGE_TASK_ID": "task-new-var",  # New var (preferred)
                "CC_DISCORD_TASK_ID": "task-wrong-var",  # Old var (ignored)
                "CLAUDE_PROJECT_DIR": "/tmp",
            },
            "matcher": "startup",
        })

        # Task should use the new var's value
        task = registry.get_by_task_id("task-new-var")
        assert task is not None
        assert task.current_claude_session_id == session_id

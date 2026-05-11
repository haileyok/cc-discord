"""Tests for slash commands."""

from __future__ import annotations

import asyncio
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from bridge.tasks import TaskRegistry
from tests.backends.discord.fakes import FakeDiscordBot
from tests.fakes import FakeZellij


@dataclass
class FakeResponse:
    """Fake discord.Interaction.response."""

    _deferred: bool = False
    _sends: list[dict] = field(default_factory=list)

    async def defer(self, *, ephemeral: bool = False) -> None:
        """Record deferred call."""
        self._deferred = True

    async def send_message(self, content: str, *, ephemeral: bool = False) -> Any:
        """Record send_message call, return a fake message."""
        self._sends.append({"content": content, "ephemeral": ephemeral})
        return None


@dataclass
class FakeFollowup:
    """Fake discord.Interaction.followup."""

    _sends: list[dict] = field(default_factory=list)

    async def send(self, content: str, *, ephemeral: bool = False) -> Any:
        """Record send call, return a fake message."""
        self._sends.append({"content": content, "ephemeral": ephemeral})
        return None


@dataclass
class FakeInteraction:
    """Fake discord.Interaction for testing command handlers."""

    channel_id: int
    guild_id: int
    response: FakeResponse = field(default_factory=FakeResponse)
    followup: FakeFollowup = field(default_factory=FakeFollowup)

    def __post_init__(self) -> None:
        self.response = FakeResponse()
        self.followup = FakeFollowup()


@pytest.fixture
def fake_bot() -> FakeDiscordBot:
    return FakeDiscordBot()


@pytest.fixture
def fake_zellij() -> FakeZellij:
    return FakeZellij()


@pytest.mark.asyncio
class TestCommands:
    """Tests for slash command handlers and utilities."""

    async def test_humanize_age_seconds(self) -> None:
        """_humanize_age formats seconds correctly."""
        from bridge.command_handlers import _humanize_age

        now = int(time.time())

        # 30 seconds ago
        result = _humanize_age(now - 30)
        assert "s ago" in result

        # 5 minutes ago
        result = _humanize_age(now - 300)
        assert "m ago" in result

        # 2 hours ago
        result = _humanize_age(now - 7200)
        assert "h ago" in result

        # 3 days ago
        result = _humanize_age(now - 259200)
        assert "d ago" in result

    async def test_wait_for_session_bind_polls_until_ready(
        self, in_memory_db, fake_bot, fake_zellij
    ) -> None:
        """_wait_for_session_bind polls until session_id is set or timeout."""
        from bridge.command_handlers import _wait_for_session_bind
        from bridge.state import upsert_task

        now = int(time.time())
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

        async def simulate_bind() -> None:
            """Simulate SessionStart binding after a short delay."""
            await asyncio.sleep(0.2)
            # Manually update the task's session_id
            task = registry.get_by_task_id("task-123")
            if task:
                task.current_claude_session_id = "sess-abc"
                await registry._index(task)

        # Start the bind simulation
        asyncio.create_task(simulate_bind())

        # Wait for session bind with 2s timeout
        await _wait_for_session_bind(registry, "task-123", timeout=2.0)

        # Task should now have session_id
        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.current_claude_session_id == "sess-abc"

    async def test_wait_for_session_bind_timeout(
        self, in_memory_db, fake_bot, fake_zellij
    ) -> None:
        """_wait_for_session_bind raises asyncio.TimeoutError if session_id doesn't arrive."""
        from bridge.command_handlers import _wait_for_session_bind
        from bridge.state import upsert_task

        now = int(time.time())
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

        # Wait with very short timeout (will timeout before bind)
        with pytest.raises(asyncio.TimeoutError):
            await _wait_for_session_bind(registry, "task-123", timeout=0.05)

    async def test_start_happy_path_no_prompt(
        self, in_memory_db, fake_bot, fake_zellij
    ) -> None:
        """start command without prompt spawns task and replies with thread URL."""
        from bridge.backends.discord.commands import build_tree

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        tree = build_tree(fake_bot, registry)

        # Get the start command callback
        start_cmd = tree.get_command("start")
        assert start_cmd is not None

        # Create a temp directory for cwd
        with tempfile.TemporaryDirectory() as tmpdir:
            interaction = FakeInteraction(channel_id=100, guild_id=1)

            # Invoke the handler
            await start_cmd.callback(interaction, cwd=tmpdir, prompt=None)

            # Verify defer was called
            assert interaction.response._deferred

            # Verify followup contains thread URL
            assert len(interaction.followup._sends) >= 1
            reply = interaction.followup._sends[0]
            assert "✅" in reply["content"]
            assert "<#" in reply["content"]  # thread URL format
            assert reply["ephemeral"]

            # Verify spawn_task was called
            assert len(fake_zellij._spawn_calls) == 1

    async def test_start_task_spawn_error(
        self, in_memory_db, fake_bot, fake_zellij
    ) -> None:
        """start command replies with ❌ when spawn_task raises."""
        from bridge.backends.discord.commands import build_tree

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        tree = build_tree(fake_bot, registry)

        start_cmd = tree.get_command("start")
        assert start_cmd is not None

        interaction = FakeInteraction(channel_id=100, guild_id=1)

        # Invoke with non-existent cwd (will fail spawn_task)
        await start_cmd.callback(interaction, cwd="/nonexistent/path", prompt=None)

        # Verify defer was called
        assert interaction.response._deferred

        # Verify error reply
        assert len(interaction.followup._sends) >= 1
        reply = interaction.followup._sends[0]
        assert "❌" in reply["content"]
        assert reply["ephemeral"]


    async def test_list_empty(self, in_memory_db, fake_bot, fake_zellij) -> None:
        """list command with no tasks replies 'No active tasks.'"""
        from bridge.backends.discord.commands import build_tree

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        tree = build_tree(fake_bot, registry)

        list_cmd = tree.get_command("list")
        assert list_cmd is not None

        interaction = FakeInteraction(channel_id=100, guild_id=1)
        await list_cmd.callback(interaction)

        # Check either response.send_message (no defer) or followup.send (with defer)
        sends = interaction.response._sends + interaction.followup._sends
        assert len(sends) >= 1
        reply = sends[0]
        assert "No active tasks" in reply["content"]
        assert reply["ephemeral"]

    async def test_list_multi(self, in_memory_db, fake_bot, fake_zellij) -> None:
        """list command shows all active tasks."""
        from bridge.backends.discord.commands import build_tree
        from bridge.state import upsert_task

        now = int(time.time())
        for i in range(3):
            await upsert_task(
                in_memory_db,
                f"task-{i}",
                2000 + i,
                f"/tmp/dir{i}",
                "running",
                zellij_pane_id=f"pane_{i}",
                current_claude_session_id=f"sess-{i}",
                now=now,
            )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()
        tree = build_tree(fake_bot, registry)

        list_cmd = tree.get_command("list")
        assert list_cmd is not None

        interaction = FakeInteraction(channel_id=100, guild_id=1)
        await list_cmd.callback(interaction)

        sends = interaction.response._sends + interaction.followup._sends
        assert len(sends) >= 1
        reply = sends[0]
        assert "**Active tasks:**" in reply["content"]
        # Should contain entries for all 3 tasks
        assert reply["content"].count("<#") >= 3

    async def test_stop_outside_task_thread(
        self, in_memory_db, fake_bot, fake_zellij
    ) -> None:
        """stop command outside a task thread replies with ❌."""
        from bridge.backends.discord.commands import build_tree

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        tree = build_tree(fake_bot, registry)

        stop_cmd = tree.get_command("stop")
        assert stop_cmd is not None

        # Interaction in non-task channel
        interaction = FakeInteraction(channel_id=100, guild_id=1)
        await stop_cmd.callback(interaction, thread=None)

        assert interaction.response._deferred
        assert len(interaction.followup._sends) >= 1
        reply = interaction.followup._sends[0]
        assert "❌" in reply["content"]

    async def test_stop_cleanly_stopped(self, in_memory_db, fake_bot, fake_zellij) -> None:
        """stop command replies ✅ when stop_task returns True."""
        from bridge.backends.discord.commands import build_tree
        from bridge.state import upsert_task

        now = int(time.time())
        await upsert_task(
            in_memory_db,
            "task-xyz",
            2000,
            "/tmp",
            "running",
            zellij_pane_id="pane_1",
            current_claude_session_id="sess-xyz",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()
        tree = build_tree(fake_bot, registry)

        stop_cmd = tree.get_command("stop")
        assert stop_cmd is not None

        interaction = FakeInteraction(channel_id=2000, guild_id=1)

        # Run stop command
        async def simulate_stop() -> None:
            cmd_task = asyncio.create_task(stop_cmd.callback(interaction, thread=None))

            # Trigger SessionEnd to signal graceful stop
            await asyncio.sleep(0.05)
            await registry._on_session_end({"session_id": "sess-xyz"})

            await cmd_task

        await simulate_stop()

        assert interaction.response._deferred
        assert len(interaction.followup._sends) >= 1
        reply = interaction.followup._sends[0]
        assert "✅ Stopped" in reply["content"]

    async def test_kill_happy_path(self, in_memory_db, fake_bot, fake_zellij) -> None:
        """kill command replies 💥 after closing pane."""
        from bridge.backends.discord.commands import build_tree
        from bridge.state import upsert_task

        now = int(time.time())
        await upsert_task(
            in_memory_db,
            "task-kill",
            2001,
            "/tmp",
            "running",
            zellij_pane_id="pane_kill",
            current_claude_session_id="sess-kill",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()
        tree = build_tree(fake_bot, registry)

        kill_cmd = tree.get_command("kill")
        assert kill_cmd is not None

        interaction = FakeInteraction(channel_id=2001, guild_id=1)
        await kill_cmd.callback(interaction, thread=None)

        assert interaction.response._deferred
        assert len(interaction.followup._sends) >= 1
        reply = interaction.followup._sends[0]
        assert "💥 Killed" in reply["content"]

        # Verify close_pane was called
        assert len(fake_zellij._close_calls) >= 1

    async def test_restart_happy_path(self, in_memory_db, fake_bot, fake_zellij) -> None:
        """restart command replies 🔄."""
        from bridge.backends.discord.commands import build_tree
        from bridge.state import upsert_task

        now = int(time.time())
        await upsert_task(
            in_memory_db,
            "task-restart",
            2002,
            "/tmp",
            "running",
            zellij_pane_id="pane_restart",
            current_claude_session_id="sess-restart",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()
        tree = build_tree(fake_bot, registry)

        restart_cmd = tree.get_command("restart")
        assert restart_cmd is not None

        interaction = FakeInteraction(channel_id=2002, guild_id=1)
        await restart_cmd.callback(interaction, thread=None)

        assert interaction.response._deferred
        assert len(interaction.followup._sends) >= 1
        reply = interaction.followup._sends[0]
        assert "🔄 Restarted" in reply["content"]


@pytest.mark.asyncio
class TestSharedCommandHandlers:
    """Tests for platform-agnostic command handlers."""

    async def test_command_result_dataclass_exists(self) -> None:
        """CommandResult dataclass is importable and has expected fields."""
        from bridge.command_handlers import CommandResult

        result = CommandResult(success=True, message="test")
        assert result.success is True
        assert result.message == "test"
        assert result.task is None
        assert result.tasks is None
        assert result.embed_data is None

    async def test_handle_start_returns_command_result(
        self, in_memory_db, fake_bot, fake_zellij
    ) -> None:
        """handle_start returns CommandResult with task data."""
        from bridge.command_handlers import handle_start

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)

        with tempfile.TemporaryDirectory() as tmpdir:
            result = await handle_start(registry, cwd=tmpdir)

            # Should return CommandResult
            assert hasattr(result, "success")
            assert hasattr(result, "message")
            assert hasattr(result, "task")
            # Successful start should have task data
            assert result.success is True
            assert result.task is not None

    async def test_handle_list_returns_command_result(
        self, in_memory_db, fake_bot, fake_zellij
    ) -> None:
        """handle_list returns CommandResult with tasks list."""
        from bridge.command_handlers import handle_list

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)

        result = await handle_list(registry)

        assert hasattr(result, "success")
        assert hasattr(result, "message")
        assert hasattr(result, "tasks")
        assert result.success is True
        assert isinstance(result.tasks, list)

    async def test_humanize_age_in_handlers(self) -> None:
        """_humanize_age is available in command_handlers module."""
        from bridge.command_handlers import _humanize_age

        now = int(time.time())
        result = _humanize_age(now - 30)
        assert "s ago" in result

    async def test_wait_for_session_bind_in_handlers(
        self, in_memory_db, fake_bot, fake_zellij
    ) -> None:
        """_wait_for_session_bind is available in command_handlers module."""
        from bridge.command_handlers import _wait_for_session_bind
        from bridge.state import upsert_task

        now = int(time.time())
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

        async def simulate_bind() -> None:
            await asyncio.sleep(0.1)
            task = registry.get_by_task_id("task-123")
            if task:
                task.current_claude_session_id = "sess-abc"
                await registry._index(task)

        asyncio.create_task(simulate_bind())

        # Should complete without error
        await _wait_for_session_bind(registry, "task-123", timeout=2.0)

    async def test_no_discord_imports_in_handlers(self) -> None:
        """command_handlers module does not import discord."""
        import inspect

        import bridge.command_handlers as handlers_module

        # Check the module source
        source = inspect.getsource(handlers_module)
        assert "import discord" not in source, "command_handlers should not import discord"
        assert "from discord" not in source, "command_handlers should not import from discord"

    async def test_handle_stop_happy_path(
        self, in_memory_db, fake_bot, fake_zellij
    ) -> None:
        """handle_stop returns success when stop_task succeeds."""
        from bridge.command_handlers import handle_stop
        from bridge.state import upsert_task

        now = int(time.time())
        await upsert_task(
            in_memory_db,
            "task-stop",
            2010,
            "/tmp",
            "running",
            zellij_pane_id="pane_stop",
            current_claude_session_id="sess-stop",
            now=now,
        )

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)
        await registry.load_from_db()

        # Simulate session end to trigger stop
        async def simulate_stop():
            result_task = asyncio.create_task(
                handle_stop(registry, thread_id="2010", task_id=None)
            )
            await asyncio.sleep(0.05)
            await registry._on_session_end({"session_id": "sess-stop"})
            return await result_task

        result = await simulate_stop()
        assert result.success is True
        assert "✅ Stopped" in result.message

    async def test_handle_list_empty(self, in_memory_db, fake_bot, fake_zellij) -> None:
        """handle_list returns empty tasks list when no tasks exist."""
        from bridge.command_handlers import handle_list

        registry = TaskRegistry(in_memory_db, fake_bot, fake_zellij)

        result = await handle_list(registry)

        assert result.success is True
        assert "No active tasks" in result.message
        assert result.tasks == []

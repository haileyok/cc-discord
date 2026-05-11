"""Tests for slash commands."""

from __future__ import annotations

import asyncio
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
        from bridge.commands import _humanize_age
        import time

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
        from bridge.commands import _wait_for_session_bind
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
        from bridge.commands import _wait_for_session_bind
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
        from bridge.commands import build_tree
        import tempfile

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
        from bridge.commands import build_tree

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
        from bridge.commands import build_tree

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
        from bridge.commands import build_tree
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
        from bridge.commands import build_tree

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
        from bridge.commands import build_tree
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
        from bridge.commands import build_tree
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
        from bridge.commands import build_tree
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

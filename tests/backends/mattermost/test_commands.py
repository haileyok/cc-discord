"""Tests for Mattermost text command parsing and dispatching."""

from __future__ import annotations

from unittest import mock

import pytest

from bridge.command_handlers import CommandResult


class TestParseTextCommand:
    """Tests for parse_text_command function."""

    def test_parse_command_no_prefix_returns_none(self):
        """Test message without ! prefix returns None."""
        from bridge.backends.mattermost.commands import parse_text_command

        result = parse_text_command("hello world")
        assert result is None

    def test_parse_simple_command(self):
        """Test parsing a simple command."""
        from bridge.backends.mattermost.commands import parse_text_command

        result = parse_text_command("!start /tmp")
        assert result == ("start", ["/tmp"])

    def test_parse_command_with_multiple_args(self):
        """Test parsing command with multiple arguments."""
        from bridge.backends.mattermost.commands import parse_text_command

        result = parse_text_command("!start /tmp my prompt text")
        assert result == ("start", ["/tmp", "my", "prompt", "text"])

    def test_parse_command_with_quoted_args(self):
        """Test parsing command with quoted arguments."""
        from bridge.backends.mattermost.commands import parse_text_command

        result = parse_text_command('!start /tmp "my prompt"')
        assert result == ("start", ["/tmp", "my prompt"])

    def test_parse_command_no_args(self):
        """Test parsing command with no arguments."""
        from bridge.backends.mattermost.commands import parse_text_command

        result = parse_text_command("!list")
        assert result == ("list", [])

    def test_parse_command_case_insensitive(self):
        """Test command name is lowercased."""
        from bridge.backends.mattermost.commands import parse_text_command

        result = parse_text_command("!START /tmp")
        assert result == ("start", ["/tmp"])

    def test_parse_command_empty_string(self):
        """Test empty command (just prefix)."""
        from bridge.backends.mattermost.commands import parse_text_command

        result = parse_text_command("!")
        assert result is None

    def test_parse_command_unmatched_quote(self):
        """Test unmatched quote falls back to split."""
        from bridge.backends.mattermost.commands import parse_text_command

        result = parse_text_command('!start /tmp "unmatched')
        # Should fall back to split() on error
        assert result is not None
        assert result[0] == "start"


class TestDispatchTextCommand:
    """Tests for dispatch_text_command function."""

    @pytest.mark.asyncio
    async def test_dispatch_start_command(self):
        """Test dispatching start command."""
        from bridge.backends.mattermost.commands import dispatch_text_command

        registry = mock.AsyncMock()
        task = mock.MagicMock()
        task.task_id = "test123abc"
        registry.spawn_task.return_value = task

        result = await dispatch_text_command("start", ["/tmp"], registry, None)

        assert result.success is True
        assert "test123abc" in result.message or "Started" in result.message
        registry.spawn_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_start_command_with_prompt(self):
        """Test dispatching start command with prompt."""
        from bridge.backends.mattermost.commands import dispatch_text_command

        registry = mock.MagicMock()
        task = mock.MagicMock()
        task.task_id = "test123abc"
        task.current_claude_session_id = "session123"
        registry.spawn_task = mock.AsyncMock(return_value=task)
        registry.get_by_task_id = mock.MagicMock(return_value=task)
        registry.write_initial_prompt = mock.AsyncMock(return_value=None)

        result = await dispatch_text_command(
            "start", ["/tmp", "my", "prompt"], registry, None
        )

        assert result.success is True
        registry.spawn_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_start_command_no_args(self):
        """Test start command without required cwd fails."""
        from bridge.backends.mattermost.commands import dispatch_text_command

        registry = mock.AsyncMock()

        result = await dispatch_text_command("start", [], registry, None)

        assert result.success is False
        assert "Usage" in result.message

    @pytest.mark.asyncio
    async def test_dispatch_stop_command(self):
        """Test dispatching stop command."""
        from bridge.backends.mattermost.commands import dispatch_text_command

        registry = mock.MagicMock()
        registry.stop_task = mock.AsyncMock(return_value=True)

        result = await dispatch_text_command("stop", ["task123"], registry, None)

        assert result.success is True
        registry.stop_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_kill_command(self):
        """Test dispatching kill command."""
        from bridge.backends.mattermost.commands import dispatch_text_command

        registry = mock.MagicMock()
        registry.kill_task = mock.AsyncMock(return_value=None)

        result = await dispatch_text_command("kill", ["task123"], registry, None)

        assert result.success is True
        registry.kill_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_list_command(self):
        """Test dispatching list command."""
        from bridge.backends.mattermost.commands import dispatch_text_command

        registry = mock.MagicMock()
        registry.list_tasks = mock.AsyncMock(return_value=[])

        result = await dispatch_text_command("list", [], registry, None)

        assert result.success is True
        registry.list_tasks.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_restart_command(self):
        """Test dispatching restart command."""
        from bridge.backends.mattermost.commands import dispatch_text_command

        registry = mock.MagicMock()
        registry.restart_task = mock.AsyncMock(return_value=None)

        result = await dispatch_text_command("restart", ["task123"], registry, None)

        assert result.success is True
        registry.restart_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_stats_command(self):
        """Test dispatching stats command."""
        from bridge.backends.mattermost.commands import dispatch_text_command

        registry = mock.MagicMock()
        task = mock.MagicMock()
        task.task_id = "task123"
        task.current_transcript_path = None
        registry.get_by_task_id = mock.MagicMock(return_value=task)

        result = await dispatch_text_command("stats", ["task123"], registry, None)

        # Should fail because task has no transcript path yet
        assert result.success is False
        assert "transcript" in result.message.lower()

    @pytest.mark.asyncio
    async def test_dispatch_rename_command(self):
        """Test dispatching rename command."""
        from bridge.backends.mattermost.commands import dispatch_text_command

        registry = mock.MagicMock()
        task = mock.MagicMock()
        task.task_id = "task123"
        registry.get_by_thread_id = mock.MagicMock(return_value=task)
        registry.generate_thread_name = mock.AsyncMock(return_value="new name")

        result = await dispatch_text_command(
            "rename", ["new", "name"], registry, "thread123"
        )

        assert result.success is True

    @pytest.mark.asyncio
    async def test_dispatch_skill_command(self):
        """Test dispatching skill command."""
        from bridge.backends.mattermost.commands import dispatch_text_command

        registry = mock.MagicMock()
        task = mock.MagicMock()
        task.task_id = "task123"
        registry.get_by_thread_id = mock.MagicMock(return_value=task)
        registry.invoke_skill = mock.AsyncMock(return_value=None)

        result = await dispatch_text_command(
            "skill", ["my-skill", "arg1"], registry, "thread123"
        )

        assert result.success is True
        registry.invoke_skill.assert_called_once()

    @pytest.mark.asyncio
    async def test_dispatch_skill_command_no_thread(self):
        """Test skill command without thread context fails."""
        from bridge.backends.mattermost.commands import dispatch_text_command

        registry = mock.AsyncMock()

        result = await dispatch_text_command("skill", ["my-skill"], registry, None)

        assert result.success is False
        assert "thread" in result.message.lower()

    @pytest.mark.asyncio
    async def test_dispatch_tasks_command(self):
        """Test dispatching tasks command."""
        from bridge.backends.mattermost.commands import dispatch_text_command

        registry = mock.MagicMock()
        task = mock.MagicMock()
        task.task_list_state = {"tasks": []}
        registry.get_by_task_id = mock.MagicMock(return_value=task)

        result = await dispatch_text_command("tasks", ["task123"], registry, None)

        assert result.success is True

    @pytest.mark.asyncio
    async def test_dispatch_unknown_command(self):
        """Test unknown command returns error."""
        from bridge.backends.mattermost.commands import dispatch_text_command

        registry = mock.AsyncMock()

        result = await dispatch_text_command("unknown", [], registry, None)

        assert result.success is False
        assert "Unknown" in result.message

"""Tests for Mattermost text command parsing and dispatching."""

from __future__ import annotations

import json
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


class TestSlashCommandHandlers:
    """Tests for HTTP slash command handlers."""

    def _make_request(self, post_data):
        """Build a mock aiohttp Request with form data."""
        from aiohttp import web

        request = mock.AsyncMock(spec=web.Request)
        request.post = mock.AsyncMock(return_value=post_data)
        return request

    @pytest.mark.asyncio
    async def test_slash_start_dispatches(self):
        """Test /start with cwd arg dispatches correctly."""
        from bridge.backends.mattermost.commands import handle_slash_request

        request = self._make_request({"text": "/tmp", "channel_id": "ch123"})

        registry = mock.MagicMock()
        task = mock.MagicMock()
        task.task_id = "task123abc"
        registry.spawn_task = mock.AsyncMock(return_value=task)
        registry.get_by_task_id = mock.MagicMock(return_value=task)

        response = await handle_slash_request(request, "start", registry)

        assert response.status == 200
        data = json.loads(response.body.decode())
        assert data["response_type"] == "in_channel"
        assert "task123" in data["text"] or "Started" in data["text"]

    @pytest.mark.asyncio
    async def test_slash_start_error_returns_ephemeral(self):
        """Test /start without args returns ephemeral error."""
        from bridge.backends.mattermost.commands import handle_slash_request

        request = self._make_request({"text": "", "channel_id": "ch123"})
        registry = mock.MagicMock()

        response = await handle_slash_request(request, "start", registry)

        assert response.status == 200
        data = json.loads(response.body.decode())
        assert data["response_type"] == "ephemeral"

    @pytest.mark.asyncio
    async def test_slash_stop_with_task_id(self):
        """Test /stop with explicit task_id passes it through."""
        from bridge.backends.mattermost.commands import handle_slash_request

        request = self._make_request({"text": "task123", "channel_id": "ch123"})

        registry = mock.MagicMock()
        registry.stop_task = mock.AsyncMock(return_value=True)

        response = await handle_slash_request(request, "stop", registry)

        assert response.status == 200
        registry.stop_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_slash_stop_uses_channel_id_as_thread_id(self):
        """Test /stop without args uses channel_id to resolve task."""
        from bridge.backends.mattermost.commands import handle_slash_request

        request = self._make_request({"text": "", "channel_id": "ch_abc"})

        task = mock.MagicMock()
        task.task_id = "resolved_task"
        registry = mock.MagicMock()
        registry.get_by_thread_id = mock.MagicMock(return_value=task)
        registry.stop_task = mock.AsyncMock(return_value=True)

        response = await handle_slash_request(request, "stop", registry)

        assert response.status == 200
        registry.get_by_thread_id.assert_called_with("ch_abc")

    @pytest.mark.asyncio
    async def test_slash_list_dispatches(self):
        """Test /list returns task list."""
        from bridge.backends.mattermost.commands import handle_slash_request

        request = self._make_request({"text": "", "channel_id": "ch123"})
        registry = mock.MagicMock()
        registry.list_tasks = mock.AsyncMock(return_value=[])

        response = await handle_slash_request(request, "list", registry)

        assert response.status == 200
        data = json.loads(response.body.decode())
        assert data["response_type"] == "in_channel"

    @pytest.mark.asyncio
    async def test_slash_token_validation_pass(self):
        """Test valid token passes through to command dispatch."""
        from bridge.backends.mattermost.commands import handle_slash_request

        request = self._make_request({
            "text": "",
            "channel_id": "ch123",
            "token": "secret123",
        })
        registry = mock.MagicMock()
        registry.list_tasks = mock.AsyncMock(return_value=[])

        response = await handle_slash_request(
            request, "list", registry, slash_token="secret123",
        )

        assert response.status == 200
        data = json.loads(response.body.decode())
        assert data["response_type"] == "in_channel"

    @pytest.mark.asyncio
    async def test_slash_token_validation_fail(self):
        """Test invalid token returns unauthorized."""
        from bridge.backends.mattermost.commands import handle_slash_request

        request = self._make_request({
            "text": "",
            "channel_id": "ch123",
            "token": "wrong",
        })
        registry = mock.MagicMock()

        response = await handle_slash_request(
            request, "list", registry, slash_token="secret123",
        )

        assert response.status == 200
        data = json.loads(response.body.decode())
        assert data["response_type"] == "ephemeral"
        assert "Unauthorized" in data["text"]

    @pytest.mark.asyncio
    async def test_slash_token_skipped_when_unconfigured(self):
        """Test no configured token skips validation."""
        from bridge.backends.mattermost.commands import handle_slash_request

        request = self._make_request({"text": "", "channel_id": "ch123"})
        registry = mock.MagicMock()
        registry.list_tasks = mock.AsyncMock(return_value=[])

        response = await handle_slash_request(
            request, "list", registry, slash_token=None,
        )

        assert response.status == 200
        data = json.loads(response.body.decode())
        assert data["response_type"] == "in_channel"

    @pytest.mark.asyncio
    async def test_slash_unknown_command(self):
        """Test unknown command returns error."""
        from bridge.backends.mattermost.commands import handle_slash_request

        request = self._make_request({"text": "", "channel_id": "ch123"})
        registry = mock.AsyncMock()

        response = await handle_slash_request(request, "nope", registry)

        assert response.status == 200
        data = json.loads(response.body.decode())
        assert data["response_type"] == "ephemeral"
        assert "Unknown" in data["text"]

    @pytest.mark.asyncio
    async def test_slash_parses_quoted_args(self):
        """Test slash handler handles quoted text args."""
        from bridge.backends.mattermost.commands import handle_slash_request

        request = self._make_request({
            "text": '/tmp "my prompt"',
            "channel_id": "ch123",
        })

        registry = mock.MagicMock()
        task = mock.MagicMock()
        task.task_id = "task123abc"
        registry.spawn_task = mock.AsyncMock(return_value=task)
        registry.get_by_task_id = mock.MagicMock(return_value=task)

        response = await handle_slash_request(request, "start", registry)

        assert response.status == 200
        registry.spawn_task.assert_called_once()
        call_kwargs = registry.spawn_task.call_args
        assert call_kwargs[1].get("prompt") == "my prompt" or \
            (call_kwargs[1].get("prompt") and "my prompt" in call_kwargs[1]["prompt"])

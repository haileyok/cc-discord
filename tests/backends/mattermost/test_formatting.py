"""Tests for Mattermost formatting and rich content rendering."""

from __future__ import annotations

from unittest import mock

import pytest


class TestFormatTaskList:
    """Tests for format_task_list function."""

    def test_format_empty_task_list(self):
        """Test empty task list returns 'No active tasks.'"""
        from bridge.backends.mattermost.formatting import format_task_list

        result = format_task_list([])
        assert result == "No active tasks."

    def test_format_single_task(self):
        """Test formatting a single task."""
        from bridge.backends.mattermost.formatting import format_task_list

        task = mock.MagicMock()
        task.task_id = "abc12345def"
        task.status = "running"
        task.cwd_leaf = "src"
        task.age = "2m"

        result = format_task_list([task])

        assert "Status" in result  # header
        assert "abc12345" in result  # truncated task ID
        assert "src" in result  # cwd_leaf
        assert "2m" in result  # age
        assert "▶️" in result  # running status emoji

    def test_format_multiple_tasks_with_different_statuses(self):
        """Test formatting multiple tasks with different statuses."""
        from bridge.backends.mattermost.formatting import format_task_list

        tasks = []
        for status in ["running", "stopped", "crashed", "archived", "spawning"]:
            task = mock.MagicMock()
            task.task_id = f"task_{status}"
            task.status = status
            task.cwd_leaf = "/tmp"
            task.age = "1m"
            tasks.append(task)

        result = format_task_list(tasks)

        # Should contain all status emojis
        assert "▶️" in result  # running
        assert "⏹" in result  # stopped
        assert "💥" in result  # crashed
        assert "📦" in result  # archived
        assert "🔄" in result  # spawning
        # Should have table structure
        assert "|" in result
        assert "---" in result

    def test_format_task_list_is_markdown_table(self):
        """Test that output is valid markdown table."""
        from bridge.backends.mattermost.formatting import format_task_list

        task = mock.MagicMock()
        task.task_id = "abc12345def"
        task.status = "running"
        task.cwd_leaf = "src"
        task.age = "2m"

        result = format_task_list([task])

        lines = result.strip().split("\n")
        # First line is header
        assert "|" in lines[0]
        # Second line is separator
        assert "---" in lines[1]
        # Data lines have pipe separators
        for line in lines[2:]:
            assert line.count("|") >= 4  # at least 5 columns (4 separators)


class TestFormatSubagentBlock:
    """Tests for format_subagent_block function."""

    def test_format_subagent_block_running(self):
        """Test formatting a running subagent block."""
        from bridge.backends.mattermost.formatting import format_subagent_block

        result = format_subagent_block(
            attribution="researcher",
            last_actions=["action1", "action2"],
            total_actions=2,
            finished=False,
            duration_str="30s",
        )

        assert "researcher" in result
        assert "action1" in result
        assert "action2" in result
        assert "running" in result
        assert "2 actions" in result
        assert "30s" in result
        assert "🟡" in result  # yellow status emoji

    def test_format_subagent_block_finished(self):
        """Test formatting a finished subagent block."""
        from bridge.backends.mattermost.formatting import format_subagent_block

        result = format_subagent_block(
            attribution="researcher",
            last_actions=["action1"],
            total_actions=5,
            finished=True,
            duration_str="2m30s",
        )

        assert "researcher" in result
        assert "finished" in result
        assert "🟢" in result  # green status emoji
        assert "5 actions" in result
        assert "2m30s" in result

    def test_format_subagent_block_many_actions(self):
        """Test formatting with many actions shows last 5."""
        from bridge.backends.mattermost.formatting import format_subagent_block

        actions = [f"action{i}" for i in range(100)]

        result = format_subagent_block(
            attribution="researcher",
            last_actions=actions,
            total_actions=100,
            finished=False,
            duration_str="10s",
        )

        # Should only include last 5 actions
        assert "action99" in result
        assert "action95" in result
        # Should NOT include early actions
        assert "action0" not in result
        assert "100 actions" in result

    def test_format_subagent_block_truncates_long_actions(self):
        """Test that very long action lists are truncated."""
        from bridge.backends.mattermost.formatting import format_subagent_block

        long_actions = [
            "a" * 1000 for _ in range(10)
        ]  # 10 actions of 1000 chars each

        result = format_subagent_block(
            attribution="researcher",
            last_actions=long_actions,
            total_actions=10,
            finished=False,
            duration_str="10s",
        )

        # Should be truncated to ~3500 chars (with ellipsis)
        assert len(result) < 4000
        assert "…(truncated)" in result or len(result) < 4000


class TestFormatToolDiff:
    """Tests for format_tool_diff function."""

    def test_format_tool_diff_returns_diff_text(self):
        """Test that tool diff returns the input markdown."""
        from bridge.backends.mattermost.formatting import format_tool_diff

        diff_text = "```diff\n-old line\n+new line\n```"
        result = format_tool_diff("Edit", diff_text)

        assert result == diff_text


class TestFormatTaskTodos:
    """Tests for format_task_todos function."""

    def test_format_empty_todos(self):
        """Test formatting empty todo list."""
        from bridge.backends.mattermost.formatting import format_task_todos

        result = format_task_todos([])
        assert result == ""

    def test_format_todos_all_statuses(self):
        """Test formatting todos with all status types."""
        from bridge.backends.mattermost.formatting import format_task_todos

        todos = [
            {"status": "completed", "content": "done task"},
            {"status": "in_progress", "content": "active task"},
            {"status": "pending", "content": "waiting task"},
            {"status": "deleted", "content": "removed task"},
        ]

        result = format_task_todos(todos)

        assert "✅" in result  # completed
        assert "▶️" in result  # in_progress
        assert "⬜" in result  # pending
        assert "🗑" in result  # deleted
        assert "done task" in result
        assert "active task" in result
        assert "waiting task" in result
        assert "removed task" in result

    def test_format_todos_unknown_status(self):
        """Test formatting todos with unknown status defaults to empty square."""
        from bridge.backends.mattermost.formatting import format_task_todos

        todos = [{"status": "unknown", "content": "task"}]

        result = format_task_todos(todos)

        assert "⬜" in result
        assert "task" in result


class TestMattermostRichFormatter:
    """Tests for MattermostRichFormatter class."""

    @pytest.mark.asyncio
    async def test_post_rich_subagent_block(self):
        """Test posting a subagent block."""
        from bridge.backends.mattermost.rich_formatter import MattermostRichFormatter

        bot = mock.AsyncMock()
        bot.post = mock.AsyncMock(return_value=["msg123"])

        formatter = MattermostRichFormatter(bot)

        result = await formatter.post_rich(
            "thread123",
            "subagent_block",
            {
                "attribution": "researcher",
                "actions": ["action1", "action2"],
                "total_actions": 2,
                "finished": False,
                "duration": "30s",
            },
        )

        assert result == "msg123"
        bot.post.assert_called_once()
        call_args = bot.post.call_args
        assert call_args[0][0]  # text argument should be non-empty
        assert "researcher" in call_args[0][0]
        assert call_args[1]["thread_id"] == "thread123"

    @pytest.mark.asyncio
    async def test_post_rich_task_list(self):
        """Test posting a task list."""
        from bridge.backends.mattermost.rich_formatter import MattermostRichFormatter

        bot = mock.AsyncMock()
        bot.post = mock.AsyncMock(return_value=["msg456"])

        formatter = MattermostRichFormatter(bot)

        task = mock.MagicMock()
        task.task_id = "abc123"
        task.status = "running"
        task.cwd_leaf = "src"
        task.age = "2m"

        result = await formatter.post_rich(
            "thread123", "task_list", {"tasks": [task]}
        )

        assert result == "msg456"
        bot.post.assert_called_once()
        call_args = bot.post.call_args
        assert "abc123" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_post_rich_todo_list(self):
        """Test posting a todo list."""
        from bridge.backends.mattermost.rich_formatter import MattermostRichFormatter

        bot = mock.AsyncMock()
        bot.post = mock.AsyncMock(return_value=["msg789"])

        formatter = MattermostRichFormatter(bot)

        todos = [
            {"status": "completed", "content": "done"},
            {"status": "pending", "content": "todo"},
        ]

        result = await formatter.post_rich("thread123", "todo_list", {"todos": todos})

        assert result == "msg789"
        bot.post.assert_called_once()
        call_args = bot.post.call_args
        assert "✅" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_edit_rich_subagent_block(self):
        """Test editing a subagent block."""
        from bridge.backends.mattermost.rich_formatter import MattermostRichFormatter

        bot = mock.AsyncMock()
        bot.edit_message = mock.AsyncMock()

        formatter = MattermostRichFormatter(bot)

        await formatter.edit_rich(
            "thread123",
            "msg123",
            "subagent_block",
            {
                "attribution": "researcher",
                "actions": ["action1"],
                "total_actions": 3,
                "finished": True,
                "duration": "1m",
            },
        )

        bot.edit_message.assert_called_once()
        call_args = bot.edit_message.call_args
        assert call_args[0] == ("thread123", "msg123")
        assert "researcher" in call_args[1]["content"]
        assert "finished" in call_args[1]["content"]

    @pytest.mark.asyncio
    async def test_post_rich_unknown_block_type(self):
        """Test posting unknown block type returns string representation."""
        from bridge.backends.mattermost.rich_formatter import MattermostRichFormatter

        bot = mock.AsyncMock()
        bot.post = mock.AsyncMock(return_value=["msg999"])

        formatter = MattermostRichFormatter(bot)

        result = await formatter.post_rich(
            "thread123", "unknown_type", {"foo": "bar"}
        )

        assert result == "msg999"
        bot.post.assert_called_once()
        call_args = bot.post.call_args
        # Should convert data to string
        assert call_args[0][0]

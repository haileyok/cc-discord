"""Tests for DiscordRichFormatter - builds Discord Embeds from data dicts."""

from __future__ import annotations

from unittest import mock

import pytest


class TestDiscordRichFormatterPostRich:
    """Tests for DiscordRichFormatter.post_rich building embeds from data dicts."""

    @pytest.mark.asyncio
    async def test_post_rich_task_list_returns_message_id(self):
        """post_rich task_list should build an embed and post it."""
        from bridge.backends.discord.formatting import DiscordRichFormatter

        bot = mock.AsyncMock()
        bot.post_embed = mock.AsyncMock(return_value="msg-123")

        formatter = DiscordRichFormatter(bot)

        result = await formatter.post_rich(
            "thread-1",
            "task_list",
            {
                "entries": [
                    {"id": "1", "status": "completed", "subject": "write tests"},
                    {"id": "2", "status": "in_progress", "subject": "refactor"},
                    {"id": "3", "status": "pending", "subject": "deploy"},
                ],
                "done": 1,
                "total": 3,
                "in_progress": 1,
            },
        )

        assert result == "msg-123"
        bot.post_embed.assert_called_once()
        call_kwargs = bot.post_embed.call_args[1]
        assert call_kwargs["thread_id"] == "thread-1"

        import discord
        embed = bot.post_embed.call_args[0][0]
        assert isinstance(embed, discord.Embed)
        assert embed.title == "📋 Tasks"
        assert "write tests" in embed.description
        assert "#1" in embed.description
        assert embed.footer.text == "1/3 done"

    @pytest.mark.asyncio
    async def test_post_rich_task_list_done_color_green(self):
        """All tasks done → green embed color."""
        from bridge.backends.discord.formatting import DiscordRichFormatter

        bot = mock.AsyncMock()
        bot.post_embed = mock.AsyncMock(return_value="msg-done")

        formatter = DiscordRichFormatter(bot)
        await formatter.post_rich(
            "t1",
            "task_list",
            {
                "entries": [{"id": "1", "status": "completed", "subject": "done"}],
                "done": 1,
                "total": 1,
                "in_progress": 0,
            },
        )

        import discord
        embed = bot.post_embed.call_args[0][0]
        assert isinstance(embed, discord.Embed)
        assert embed.color.value == 0x57F287

    @pytest.mark.asyncio
    async def test_post_rich_task_list_in_progress_color_yellow(self):
        """In-progress tasks → yellow embed color."""
        from bridge.backends.discord.formatting import DiscordRichFormatter

        bot = mock.AsyncMock()
        bot.post_embed = mock.AsyncMock(return_value="msg-wip")

        formatter = DiscordRichFormatter(bot)
        await formatter.post_rich(
            "t1",
            "task_list",
            {
                "entries": [{"id": "1", "status": "in_progress", "subject": "wip"}],
                "done": 0,
                "total": 1,
                "in_progress": 1,
            },
        )

        import discord
        embed = bot.post_embed.call_args[0][0]
        assert embed.color.value == 0xFEE75C

    @pytest.mark.asyncio
    async def test_post_rich_subagent_block_returns_message_id(self):
        """post_rich subagent_block should build a subagent embed and post it."""
        from bridge.backends.discord.formatting import DiscordRichFormatter

        bot = mock.AsyncMock()
        bot.post_embed = mock.AsyncMock(return_value="msg-sub")

        formatter = DiscordRichFormatter(bot)

        result = await formatter.post_rich(
            "thread-2",
            "subagent_block",
            {
                "attribution": "researcher",
                "actions": ["Read file.py", "Searched for X"],
                "total_actions": 5,
                "finished": False,
                "duration": "30s",
            },
        )

        assert result == "msg-sub"
        bot.post_embed.assert_called_once()

        import discord
        embed = bot.post_embed.call_args[0][0]
        assert isinstance(embed, discord.Embed)
        assert "researcher" in embed.title
        assert "Read file.py" in embed.description
        assert "running" in embed.footer.text
        assert "5 actions" in embed.footer.text
        assert "30s" in embed.footer.text

    @pytest.mark.asyncio
    async def test_post_rich_subagent_block_finished_green(self):
        """Finished subagent → green embed color."""
        from bridge.backends.discord.formatting import DiscordRichFormatter

        bot = mock.AsyncMock()
        bot.post_embed = mock.AsyncMock(return_value="msg-done")

        formatter = DiscordRichFormatter(bot)
        await formatter.post_rich(
            "t1",
            "subagent_block",
            {
                "attribution": "writer",
                "actions": ["Wrote report"],
                "total_actions": 10,
                "finished": True,
                "duration": "2.5m",
            },
        )

        import discord
        embed = bot.post_embed.call_args[0][0]
        assert embed.color.value == 0x57F287
        assert "finished" in embed.footer.text

    @pytest.mark.asyncio
    async def test_post_rich_unknown_block_type_raises(self):
        """Unknown block type should raise NotImplementedError."""
        from bridge.backends.discord.formatting import DiscordRichFormatter

        bot = mock.AsyncMock()
        formatter = DiscordRichFormatter(bot)

        with pytest.raises(NotImplementedError):
            await formatter.post_rich("t1", "unknown_type", {"foo": "bar"})


class TestDiscordRichFormatterEditRich:
    """Tests for DiscordRichFormatter.edit_rich building embeds from data dicts."""

    @pytest.mark.asyncio
    async def test_edit_rich_task_list(self):
        """edit_rich task_list should build embed and call edit_message."""
        from bridge.backends.discord.formatting import DiscordRichFormatter

        bot = mock.AsyncMock()
        bot.edit_message = mock.AsyncMock()

        formatter = DiscordRichFormatter(bot)

        await formatter.edit_rich(
            "thread-1",
            "msg-old",
            "task_list",
            {
                "entries": [
                    {"id": "1", "status": "completed", "subject": "done"},
                    {"id": "2", "status": "deleted", "subject": "removed"},
                ],
                "done": 1,
                "total": 2,
                "in_progress": 0,
            },
        )

        bot.edit_message.assert_called_once_with("thread-1", "msg-old", embed=mock.ANY)
        import discord
        embed = bot.edit_message.call_args[1]["embed"]
        assert isinstance(embed, discord.Embed)
        assert "✅ #1" in embed.description
        assert "🗑 #2" in embed.description
        assert embed.footer.text == "1/2 done"

    @pytest.mark.asyncio
    async def test_edit_rich_subagent_block(self):
        """edit_rich subagent_block should build embed and call edit_message."""
        from bridge.backends.discord.formatting import DiscordRichFormatter

        bot = mock.AsyncMock()
        bot.edit_message = mock.AsyncMock()

        formatter = DiscordRichFormatter(bot)

        await formatter.edit_rich(
            "thread-2",
            "msg-sub",
            "subagent_block",
            {
                "attribution": "planner",
                "actions": ["Planned X", "Planned Y"],
                "total_actions": 8,
                "finished": True,
                "duration": "1.2m",
            },
        )

        bot.edit_message.assert_called_once_with("thread-2", "msg-sub", embed=mock.ANY)
        import discord
        embed = bot.edit_message.call_args[1]["embed"]
        assert isinstance(embed, discord.Embed)
        assert "planner" in embed.title
        assert "finished" in embed.footer.text
        assert "8 actions" in embed.footer.text

    @pytest.mark.asyncio
    async def test_edit_rich_unknown_block_type_raises(self):
        """Unknown block type should raise NotImplementedError."""
        from bridge.backends.discord.formatting import DiscordRichFormatter

        bot = mock.AsyncMock()
        formatter = DiscordRichFormatter(bot)

        with pytest.raises(NotImplementedError):
            await formatter.edit_rich("t1", "msg1", "unknown_type", {"foo": "bar"})


class TestBuildTaskListEmbed:
    """Unit tests for _build_task_list_embed helper."""

    def test_empty_entries_shows_no_tasks(self):
        """Empty entries should render as '_(no tasks)_'."""
        from bridge.backends.discord.formatting import _build_task_list_embed

        embed = _build_task_list_embed({"entries": [], "done": 0, "total": 0, "in_progress": 0})
        assert "_(no tasks)_" in embed.description

    def test_marks_by_status(self):
        """Correct marks for each status."""
        from bridge.backends.discord.formatting import _build_task_list_embed

        embed = _build_task_list_embed({
            "entries": [
                {"id": "1", "status": "completed", "subject": "a"},
                {"id": "2", "status": "in_progress", "subject": "b"},
                {"id": "3", "status": "deleted", "subject": "c"},
                {"id": "4", "status": "pending", "subject": "d"},
            ],
            "done": 1,
            "total": 4,
            "in_progress": 1,
        })
        assert "✅ #1 a" in embed.description
        assert "▶️ #2 b" in embed.description
        assert "🗑 #3 c" in embed.description
        assert "⬜ #4 d" in embed.description

    def test_no_subject_omits_subject(self):
        """Entry with empty subject should not add trailing space."""
        from bridge.backends.discord.formatting import _build_task_list_embed

        embed = _build_task_list_embed({
            "entries": [{"id": "1", "status": "pending", "subject": ""}],
            "done": 0,
            "total": 1,
            "in_progress": 0,
        })
        assert "⬜ #1\n" in embed.description or embed.description == "⬜ #1"

    def test_grey_color_when_nothing_in_progress(self):
        """Grey color when there's nothing done and nothing in_progress."""
        from bridge.backends.discord.formatting import _build_task_list_embed

        embed = _build_task_list_embed({
            "entries": [{"id": "1", "status": "pending", "subject": ""}],
            "done": 0,
            "total": 1,
            "in_progress": 0,
        })
        assert embed.color.value == 0x95A5A6


class TestBuildSubagentEmbed:
    """Unit tests for _build_subagent_embed helper."""

    def test_no_actions_shows_placeholder(self):
        """Empty actions list should show placeholder text."""
        from bridge.backends.discord.formatting import _build_subagent_embed

        embed = _build_subagent_embed({
            "attribution": "worker",
            "actions": [],
            "total_actions": 0,
            "finished": False,
            "duration": "5s",
        })
        assert "_(no actions yet)_" in embed.description

    def test_footer_contains_all_info(self):
        """Footer should contain status, total_actions, and duration."""
        from bridge.backends.discord.formatting import _build_subagent_embed

        embed = _build_subagent_embed({
            "attribution": "agent-1",
            "actions": ["Did stuff"],
            "total_actions": 42,
            "finished": True,
            "duration": "3.1m",
        })
        assert "finished" in embed.footer.text
        assert "42 actions" in embed.footer.text
        assert "3.1m" in embed.footer.text

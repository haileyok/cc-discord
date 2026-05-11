"""Tests for the Mattermost bot backend."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest import mock

import pytest

from bridge.backends.mattermost.api import RateLimitError
from bridge.backends.mattermost.bot import MattermostBot, _chunk, _emoji_to_mattermost


class TestChunkFunction:
    """Tests for message chunking."""

    def test_chunk_short_message_not_chunked(self):
        """Test short messages are not chunked."""
        text = "hello world"
        chunks = _chunk(text, 100)
        assert chunks == ["hello world"]

    def test_chunk_long_message_split_on_newline(self):
        """Test long messages are split on newlines."""
        text = "line 1\nline 2\nline 3\nline 4"
        chunks = _chunk(text, 20)
        assert len(chunks) == 2
        assert chunks[0] == "line 1\nline 2"
        assert chunks[1] == "line 3\nline 4"

    def test_chunk_no_newline_available_splits_at_limit(self):
        """Test split at limit when no newline available."""
        text = "a" * 150
        chunks = _chunk(text, 100)
        assert len(chunks) == 2
        assert chunks[0] == "a" * 100
        assert chunks[1] == "a" * 50

    def test_chunk_multiple_chunks(self):
        """Test multiple chunks are created."""
        text = "x\n" * 100  # 200 chars with newlines
        chunks = _chunk(text, 30)
        assert len(chunks) > 2

    def test_chunk_strips_leading_newlines(self):
        """Test leading newlines are stripped after split."""
        text = "line1\n\n\nline2"
        chunks = _chunk(text, 10)
        assert len(chunks) == 2
        # lstrip removes all leading newlines
        assert chunks[0] == "line1\n\n"
        assert chunks[1] == "line2"


class TestEmojiMapping:
    """Tests for emoji mapping."""

    def test_emoji_checkmark_mapped(self):
        """Test checkmark emoji is mapped."""
        assert _emoji_to_mattermost("✅") == "white_check_mark"

    def test_emoji_x_mapped(self):
        """Test X emoji is mapped."""
        assert _emoji_to_mattermost("❌") == "x"

    def test_emoji_thumbsup_mapped(self):
        """Test thumbsup emoji is mapped."""
        assert _emoji_to_mattermost("👍") == "thumbsup"

    def test_emoji_number_mapped(self):
        """Test number emojis are mapped."""
        assert _emoji_to_mattermost("1️⃣") == "one"
        assert _emoji_to_mattermost("2️⃣") == "two"

    def test_emoji_unknown_returns_as_is(self):
        """Test unknown emoji returns unchanged."""
        assert _emoji_to_mattermost("🎉") == "🎉"


class TestMattermostBotConstruction:
    """Tests for bot construction."""

    def test_bot_construction(self):
        """Test MattermostBot can be constructed."""
        bot = MattermostBot(
            "https://mm.example.com",
            "token",
            "channel-id",
        )
        assert bot._channel_id == "channel-id"
        assert bot._allowed_user_ids is None
        assert bot._on_message is None
        assert bot._on_reaction is None

    def test_bot_with_callbacks(self):
        """Test bot construction with callbacks."""
        on_msg = mock.AsyncMock()
        on_rxn = mock.AsyncMock()

        bot = MattermostBot(
            "https://mm.example.com",
            "token",
            "channel-id",
            on_message=on_msg,
            on_reaction=on_rxn,
        )

        assert bot._on_message is on_msg
        assert bot._on_reaction is on_rxn

    def test_bot_with_allowed_users(self):
        """Test bot construction with allowed user IDs."""
        allowed = ["user1", "user2"]
        bot = MattermostBot(
            "https://mm.example.com",
            "token",
            "channel-id",
            allowed_user_ids=allowed,
        )

        assert bot._allowed_user_ids == {"user1", "user2"}

    def test_bot_is_ready_property_before_start(self):
        """Test is_ready is False before start."""
        bot = MattermostBot("https://mm.example.com", "token", "channel-id")
        assert bot.is_ready is False


class TestMattermostBotLifecycle:
    """Tests for bot lifecycle."""

    @pytest.mark.asyncio
    async def test_bot_start_initializes(self):
        """Test start() initializes API and WebSocket."""
        bot = MattermostBot("https://mm.example.com", "token", "channel-id")

        # Mock the API and WebSocket
        bot._api = mock.AsyncMock()
        bot._api.get_me = mock.AsyncMock(return_value={"id": "bot-user-123"})
        bot._api.base_url = "https://mm.example.com"

        await bot.start()

        assert bot._ready is True
        assert bot._bot_user_id == "bot-user-123"
        bot._api.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_bot_close_cleanup(self):
        """Test close() cleans up resources."""
        bot = MattermostBot("https://mm.example.com", "token", "channel-id")
        bot._ready = True
        bot._ws = mock.AsyncMock()
        bot._api = mock.AsyncMock()

        await bot.close()

        assert bot._ready is False
        bot._ws.close.assert_called_once()
        bot._api.close.assert_called_once()


class TestMattermostBotPosting:
    """Tests for posting messages."""

    @pytest.mark.asyncio
    async def test_post_single_message(self):
        """Test posting a single message."""
        bot = MattermostBot("https://mm.example.com", "token", "channel-id")
        bot._api = mock.AsyncMock()
        bot._api.create_post = mock.AsyncMock(return_value={"id": "msg1"})

        result = await bot.post("hello world")

        assert result == ["msg1"]
        bot._api.create_post.assert_called_once_with(
            "channel-id", "hello world", root_id=None
        )

    @pytest.mark.asyncio
    async def test_post_with_thread_id(self):
        """Test posting to a thread."""
        bot = MattermostBot("https://mm.example.com", "token", "channel-id")
        bot._api = mock.AsyncMock()
        bot._api.create_post = mock.AsyncMock(return_value={"id": "msg1"})

        result = await bot.post("hello", thread_id="thread123")

        assert result == ["msg1"]
        bot._api.create_post.assert_called_once_with(
            "channel-id", "hello", root_id="thread123"
        )

    @pytest.mark.asyncio
    async def test_post_chunked_message(self):
        """Test posting a message that is chunked."""
        bot = MattermostBot("https://mm.example.com", "token", "channel-id")
        bot._api = mock.AsyncMock()
        bot._api.create_post = mock.AsyncMock(
            side_effect=[{"id": "msg1"}, {"id": "msg2"}]
        )

        # Create a message that will be chunked
        long_text = "x" * 5000  # Longer than CHUNK_LIMIT

        result = await bot.post(long_text)

        assert len(result) == 2
        assert result == ["msg1", "msg2"]
        assert bot._api.create_post.call_count == 2

    @pytest.mark.asyncio
    async def test_post_with_attachments_uploads_files(self):
        """Test post_with_attachments uploads files."""
        bot = MattermostBot("https://mm.example.com", "token", "channel-id")
        bot._api = mock.AsyncMock()

        # Mock file upload responses - each call returns different file info
        bot._api.upload_file = mock.AsyncMock(
            side_effect=[
                {"file_infos": [{"id": "file1"}]},
                {"file_infos": [{"id": "file2"}]},
            ]
        )

        # Mock post creation
        bot._api.create_post = mock.AsyncMock(return_value={"id": "msg1"})

        # Create temp files
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            file1 = Path(tmpdir) / "test1.txt"
            file2 = Path(tmpdir) / "test2.txt"
            file1.write_text("content1")
            file2.write_text("content2")

            result = await bot.post_with_attachments(
                [file1, file2], text="Here are files"
            )

        assert result == ["msg1"]
        assert bot._api.upload_file.call_count == 2
        bot._api.create_post.assert_called_once()

        # Check that create_post was called with file_ids
        call_kwargs = bot._api.create_post.call_args[1]
        assert call_kwargs["file_ids"] == ["file1", "file2"]

    @pytest.mark.asyncio
    async def test_post_with_attachments_handles_upload_failure(self):
        """Test post_with_attachments posts text when file upload fails."""
        bot = MattermostBot("https://mm.example.com", "token", "channel-id")
        bot._api = mock.AsyncMock()

        # Mock upload failure
        bot._api.upload_file = mock.AsyncMock(side_effect=Exception("Upload failed"))

        # Mock post creation
        bot._api.create_post = mock.AsyncMock(return_value={"id": "msg1"})

        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            file1 = Path(tmpdir) / "test1.txt"
            file1.write_text("content")

            result = await bot.post_with_attachments(
                [file1], text="Here are files"
            )

        assert result == ["msg1"]

        # Check that create_post was called with error note
        call_args = bot._api.create_post.call_args
        msg = call_args[0][1]
        assert "Failed to upload" in msg
        assert "test1.txt" in msg

    @pytest.mark.asyncio
    async def test_post_with_rate_limit_retry(self):
        """Test post retries on rate limit."""
        bot = MattermostBot("https://mm.example.com", "token", "channel-id")
        bot._api = mock.AsyncMock()

        # Mock rate limit then success
        bot._api.create_post = mock.AsyncMock(
            side_effect=[
                RateLimitError(0.01),  # Very short retry time for testing
                {"id": "msg1"},
            ]
        )

        result = await bot.post("hello")

        assert result == ["msg1"]
        assert bot._api.create_post.call_count == 2


class TestMattermostBotThreadOperations:
    """Tests for thread operations."""

    @pytest.mark.asyncio
    async def test_create_thread(self):
        """Test creating a thread."""
        bot = MattermostBot("https://mm.example.com", "token", "channel-id")
        bot._api = mock.AsyncMock()
        bot._api.create_post = mock.AsyncMock(return_value={"id": "thread123"})

        result = await bot.create_thread("my task")

        assert result == "thread123"
        bot._api.create_post.assert_called_once()
        call_args = bot._api.create_post.call_args
        msg = call_args[0][1]
        assert "my task" in msg
        assert "🟢" in msg

    @pytest.mark.asyncio
    async def test_rename_thread(self):
        """Test renaming a thread."""
        bot = MattermostBot("https://mm.example.com", "token", "channel-id")
        bot._api = mock.AsyncMock()
        bot._api.update_post = mock.AsyncMock()

        await bot.rename_thread("thread123", "new name")

        bot._api.update_post.assert_called_once()
        call_args = bot._api.update_post.call_args
        msg = call_args[0][1]
        assert "new name" in msg

    @pytest.mark.asyncio
    async def test_archive_thread_is_noop(self):
        """Test archive_thread is a no-op for Mattermost."""
        bot = MattermostBot("https://mm.example.com", "token", "channel-id")
        bot._api = mock.AsyncMock()

        # Should not raise
        await bot.archive_thread("thread123")

    @pytest.mark.asyncio
    async def test_thread_alive(self):
        """Test checking if thread is alive."""
        bot = MattermostBot("https://mm.example.com", "token", "channel-id")
        bot._api = mock.AsyncMock()
        bot._api.get_post = mock.AsyncMock(return_value={"id": "thread123"})

        result = await bot.thread_alive("thread123")

        assert result is True
        bot._api.get_post.assert_called_once_with("thread123")

    @pytest.mark.asyncio
    async def test_thread_alive_returns_false_on_404(self):
        """Test thread_alive returns False when post not found."""
        bot = MattermostBot("https://mm.example.com", "token", "channel-id")
        bot._api = mock.AsyncMock()
        bot._api.get_post = mock.AsyncMock(return_value=None)

        result = await bot.thread_alive("thread123")

        assert result is False


class TestMattermostBotAttachments:
    """Tests for attachment operations."""

    @pytest.mark.asyncio
    async def test_download_attachment(self):
        """Test downloading an attachment."""
        bot = MattermostBot("https://mm.example.com", "token", "channel-id")
        bot._api = mock.AsyncMock()
        bot._api.download_file = mock.AsyncMock(return_value=b"file content")

        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            dest_dir = Path(tmpdir)
            attachment_ref = {"id": "file123", "name": "test.txt"}

            result = await bot.download_attachment(attachment_ref, dest_dir)

            assert result == dest_dir / "test.txt"
            assert result.read_bytes() == b"file content"


class TestMattermostBotReactions:
    """Tests for reaction operations."""

    @pytest.mark.asyncio
    async def test_add_reactions(self):
        """Test adding reactions."""
        bot = MattermostBot("https://mm.example.com", "token", "channel-id")
        bot._api = mock.AsyncMock()
        bot._bot_user_id = "bot123"

        await bot.add_reactions("msg1", "thread1", ["✅", "👍"])

        assert bot._api.add_reaction.call_count == 2
        calls = bot._api.add_reaction.call_args_list
        # First reaction
        assert calls[0][0] == ("bot123", "msg1", "white_check_mark")
        # Second reaction
        assert calls[1][0] == ("bot123", "msg1", "thumbsup")


class TestMattermostBotEditing:
    """Tests for message editing."""

    @pytest.mark.asyncio
    async def test_edit_message(self):
        """Test editing a message."""
        bot = MattermostBot("https://mm.example.com", "token", "channel-id")
        bot._api = mock.AsyncMock()

        await bot.edit_message("thread1", "msg1", content="new content")

        bot._api.update_post.assert_called_once_with("msg1", "new content")

    @pytest.mark.asyncio
    async def test_edit_message_without_content_is_noop(self):
        """Test edit_message without content is no-op."""
        bot = MattermostBot("https://mm.example.com", "token", "channel-id")
        bot._api = mock.AsyncMock()

        await bot.edit_message("thread1", "msg1")

        bot._api.update_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetch_messageable(self):
        """Test fetch_messageable returns thread_id."""
        bot = MattermostBot("https://mm.example.com", "token", "channel-id")

        result = await bot.fetch_messageable("thread1")

        assert result == "thread1"


class TestMattermostBotEventHandling:
    """Tests for event handling."""

    @pytest.mark.asyncio
    async def test_handle_posted_event_calls_handler(self):
        """Test posted events are dispatched."""
        handler = mock.AsyncMock()
        bot = MattermostBot(
            "https://mm.example.com",
            "token",
            "channel-id",
            on_message=handler,
        )
        bot._bot_user_id = "bot123"

        post = {
            "id": "msg1",
            "message": "hello",
            "user_id": "user1",
            "channel_id": "channel-id",
        }

        await bot._handle_event("posted", {"post": post})

        handler.assert_called_once_with(post)

    @pytest.mark.asyncio
    async def test_handle_posted_event_ignores_own_posts(self):
        """Test own posts are ignored."""
        handler = mock.AsyncMock()
        bot = MattermostBot(
            "https://mm.example.com",
            "token",
            "channel-id",
            on_message=handler,
        )
        bot._bot_user_id = "bot123"

        post = {
            "id": "msg1",
            "message": "hello",
            "user_id": "bot123",
            "channel_id": "channel-id",
        }

        await bot._handle_event("posted", {"post": post})

        handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_posted_event_filters_by_user(self):
        """Test user filtering for posted events."""
        handler = mock.AsyncMock()
        bot = MattermostBot(
            "https://mm.example.com",
            "token",
            "channel-id",
            on_message=handler,
            allowed_user_ids=["user1"],
        )
        bot._bot_user_id = "bot123"

        post = {
            "id": "msg1",
            "message": "hello",
            "user_id": "user2",
            "channel_id": "channel-id",
        }

        await bot._handle_event("posted", {"post": post})

        handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_posted_event_filters_by_channel(self):
        """Test channel filtering for posted events."""
        handler = mock.AsyncMock()
        bot = MattermostBot(
            "https://mm.example.com",
            "token",
            "channel-id",
            on_message=handler,
        )
        bot._bot_user_id = "bot123"

        post = {
            "id": "msg1",
            "message": "hello",
            "user_id": "user1",
            "channel_id": "other-channel",
        }

        await bot._handle_event("posted", {"post": post})

        handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_reaction_added_calls_handler(self):
        """Test reaction_added events are dispatched."""
        handler = mock.AsyncMock()
        bot = MattermostBot(
            "https://mm.example.com",
            "token",
            "channel-id",
            on_reaction=handler,
        )
        bot._bot_user_id = "bot123"

        reaction = {
            "user_id": "user1",
            "post_id": "msg1",
            "emoji_name": "thumbsup",
        }

        await bot._handle_event("reaction_added", {"reaction": reaction})

        handler.assert_called_once_with(reaction)

    @pytest.mark.asyncio
    async def test_handle_reaction_added_ignores_own_reactions(self):
        """Test own reactions are ignored."""
        handler = mock.AsyncMock()
        bot = MattermostBot(
            "https://mm.example.com",
            "token",
            "channel-id",
            on_reaction=handler,
        )
        bot._bot_user_id = "bot123"

        reaction = {
            "user_id": "bot123",
            "post_id": "msg1",
            "emoji_name": "thumbsup",
        }

        await bot._handle_event("reaction_added", {"reaction": reaction})

        handler.assert_not_called()

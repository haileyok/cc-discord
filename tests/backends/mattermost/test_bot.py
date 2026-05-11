"""Tests for the Mattermost bot backend."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest import mock

import pytest

from bridge.approvals import ApprovalRouter
from bridge.backends.mattermost.api import RateLimitError
from bridge.backends.mattermost.bot import (
    MattermostBot,
    _chunk,
    _emoji_to_mattermost,
    _mattermost_to_emoji,
    _process_post_files,
)


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


class TestMattermostToEmojiMapping:
    """Tests for reverse emoji mapping (Mattermost name → Unicode)."""

    def test_mattermost_white_check_mark_mapped(self):
        """Test white_check_mark maps to checkmark emoji."""
        assert _mattermost_to_emoji("white_check_mark") == "✅"

    def test_mattermost_x_mapped(self):
        """Test x maps to X emoji."""
        assert _mattermost_to_emoji("x") == "❌"

    def test_mattermost_thumbsup_mapped(self):
        """Test thumbsup maps to thumbsup emoji."""
        assert _mattermost_to_emoji("thumbsup") == "👍"

    def test_mattermost_thumbsdown_mapped(self):
        """Test thumbsdown maps to thumbsdown emoji."""
        assert _mattermost_to_emoji("thumbsdown") == "👎"

    def test_mattermost_number_mapped(self):
        """Test number names map to number emojis."""
        assert _mattermost_to_emoji("one") == "1️⃣"
        assert _mattermost_to_emoji("two") == "2️⃣"
        assert _mattermost_to_emoji("three") == "3️⃣"
        assert _mattermost_to_emoji("four") == "4️⃣"

    def test_mattermost_unknown_returns_as_is(self):
        """Test unknown Mattermost emoji name returns unchanged."""
        assert _mattermost_to_emoji("custom_emoji") == "custom_emoji"

    def test_bidirectional_mapping_consistency(self):
        """Test that mapped emojis are consistent in both directions."""
        test_pairs = [
            ("✅", "white_check_mark"),
            ("❌", "x"),
            ("1️⃣", "one"),
            ("2️⃣", "two"),
            ("3️⃣", "three"),
            ("4️⃣", "four"),
            ("👍", "thumbsup"),
            ("👎", "thumbsdown"),
        ]
        for emoji, mm_name in test_pairs:
            assert _emoji_to_mattermost(emoji) == mm_name
            assert _mattermost_to_emoji(mm_name) == emoji


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

        handler.assert_called_once()
        called_reaction = handler.call_args[0][0]
        assert called_reaction["post_id"] == "msg1"
        assert called_reaction["user_id"] == "user1"
        assert called_reaction["emoji"] == "👍"

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

    @pytest.mark.asyncio
    async def test_handle_reaction_added_normalizes_emoji(self):
        """Test reaction handler receives normalized Unicode emoji."""
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
            "emoji_name": "white_check_mark",
        }

        await bot._handle_event("reaction_added", {"reaction": reaction})

        handler.assert_called_once()
        called_reaction = handler.call_args[0][0]
        # Should have normalized emoji_name to Unicode
        assert called_reaction["emoji"] == "✅"
        assert called_reaction["post_id"] == "msg1"
        assert called_reaction["user_id"] == "user1"

    @pytest.mark.asyncio
    async def test_handle_reaction_added_normalizes_deny_emoji(self):
        """Test X emoji reaction is normalized correctly."""
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
            "emoji_name": "x",
        }

        await bot._handle_event("reaction_added", {"reaction": reaction})

        handler.assert_called_once()
        called_reaction = handler.call_args[0][0]
        assert called_reaction["emoji"] == "❌"

    @pytest.mark.asyncio
    async def test_handle_reaction_added_normalizes_number_emoji(self):
        """Test numeric emoji reactions are normalized correctly."""
        handler = mock.AsyncMock()
        bot = MattermostBot(
            "https://mm.example.com",
            "token",
            "channel-id",
            on_reaction=handler,
        )
        bot._bot_user_id = "bot123"

        for mm_name, expected_emoji in [("one", "1️⃣"), ("two", "2️⃣"), ("three", "3️⃣")]:
            handler.reset_mock()
            reaction = {
                "user_id": "user1",
                "post_id": "msg1",
                "emoji_name": mm_name,
            }

            await bot._handle_event("reaction_added", {"reaction": reaction})

            handler.assert_called_once()
            called_reaction = handler.call_args[0][0]
            assert called_reaction["emoji"] == expected_emoji


class TestAudioProcessing:
    """Tests for audio file handling and transcription."""

    @pytest.mark.asyncio
    async def test_process_post_files_with_no_files(self):
        """Test processing post with no files returns empty lists."""
        post = {"message": "hello", "file_ids": []}
        api = mock.AsyncMock()

        voice_blocks, file_refs = await _process_post_files(post, api)

        assert voice_blocks == []
        assert file_refs == []

    @pytest.mark.asyncio
    async def test_process_post_files_with_non_audio_file(self):
        """Test non-audio files are returned as file references."""
        post = {
            "message": "hello",
            "file_ids": ["file1"],
        }
        api = mock.AsyncMock()
        api.get_file_info = mock.AsyncMock(
            return_value={
                "id": "file1",
                "name": "document.pdf",
                "mime_type": "application/pdf",
            }
        )

        voice_blocks, file_refs = await _process_post_files(post, api)

        assert voice_blocks == []
        assert len(file_refs) == 1
        assert file_refs[0]["id"] == "file1"
        assert file_refs[0]["name"] == "document.pdf"

    @pytest.mark.asyncio
    async def test_process_post_files_with_audio_file_successful_transcription(self):
        """Test audio files are transcribed successfully."""
        post = {
            "message": "hello",
            "file_ids": ["audio1"],
        }
        api = mock.AsyncMock()
        api.get_file_info = mock.AsyncMock(
            return_value={
                "id": "audio1",
                "name": "memo.wav",
                "mime_type": "audio/wav",
            }
        )
        api.download_file = mock.AsyncMock(return_value=b"fake audio data")

        with mock.patch("bridge.backends.mattermost.bot.voice.transcribe") as mock_transcribe:
            mock_transcribe.return_value = "hello world"

            voice_blocks, file_refs = await _process_post_files(post, api)

        assert len(voice_blocks) == 1
        assert "[voice memo] hello world" in voice_blocks[0]
        assert file_refs == []

    @pytest.mark.asyncio
    async def test_process_post_files_with_audio_file_transcription_failure(self):
        """Test audio transcription failure gracefully falls back."""
        post = {
            "message": "hello",
            "file_ids": ["audio1"],
        }
        api = mock.AsyncMock()
        api.get_file_info = mock.AsyncMock(
            return_value={
                "id": "audio1",
                "name": "memo.wav",
                "mime_type": "audio/wav",
            }
        )
        api.download_file = mock.AsyncMock(return_value=b"fake audio data")

        with mock.patch("bridge.backends.mattermost.bot.voice.transcribe") as mock_transcribe:
            mock_transcribe.return_value = None

            voice_blocks, file_refs = await _process_post_files(post, api)

        assert len(voice_blocks) == 1
        assert "[voice memo received — transcription unavailable" in voice_blocks[0]
        assert file_refs == []

    @pytest.mark.asyncio
    async def test_process_post_files_with_mixed_audio_and_non_audio(self):
        """Test mixed audio and non-audio files are handled correctly."""
        post = {
            "message": "check these",
            "file_ids": ["audio1", "file2", "audio3"],
        }
        api = mock.AsyncMock()
        api.get_file_info = mock.AsyncMock(
            side_effect=[
                {
                    "id": "audio1",
                    "name": "memo.m4a",
                    "mime_type": "audio/mp4",
                },
                {
                    "id": "file2",
                    "name": "report.docx",
                    "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                },
                {
                    "id": "audio3",
                    "name": "note.wav",
                    "mime_type": "audio/wav",
                },
            ]
        )
        api.download_file = mock.AsyncMock(return_value=b"fake data")

        with mock.patch("bridge.backends.mattermost.bot.voice.transcribe") as mock_transcribe:
            mock_transcribe.side_effect = ["memo transcription", "note transcription"]

            voice_blocks, file_refs = await _process_post_files(post, api)

        assert len(voice_blocks) == 2
        assert "[voice memo] memo transcription" in voice_blocks[0]
        assert "[voice memo] note transcription" in voice_blocks[1]
        assert len(file_refs) == 1
        assert file_refs[0]["id"] == "file2"

    @pytest.mark.asyncio
    async def test_handle_event_includes_transcription_in_message(self):
        """Test that posted events with audio include transcriptions."""
        handler = mock.AsyncMock()
        bot = MattermostBot(
            "https://mm.example.com",
            "token",
            "channel-id",
            on_message=handler,
        )
        bot._bot_user_id = "bot123"
        bot._api = mock.AsyncMock()
        bot._api.get_file_info = mock.AsyncMock(
            return_value={
                "id": "audio1",
                "name": "memo.wav",
                "mime_type": "audio/wav",
            }
        )
        bot._api.download_file = mock.AsyncMock(return_value=b"fake audio")

        post = {
            "id": "msg1",
            "message": "check this",
            "user_id": "user1",
            "channel_id": "channel-id",
            "file_ids": ["audio1"],
        }

        with mock.patch("bridge.backends.mattermost.bot.voice.transcribe") as mock_transcribe:
            mock_transcribe.return_value = "audio transcription"

            await bot._handle_event("posted", {"post": post})

        handler.assert_called_once()
        called_post = handler.call_args[0][0]
        # The post should be modified with transcription blocks
        assert "check this" in called_post.get("message", "")
        assert "[voice memo]" in called_post.get("message", "")


class TestMattermostBotTextCommands:
    """Tests for text command handling in bot."""

    @pytest.mark.asyncio
    async def test_handle_event_text_command_start(self):
        """Test that !start commands are intercepted and dispatched."""
        registry = mock.MagicMock()
        task = mock.MagicMock()
        task.task_id = "task123abc"
        registry.spawn_task = mock.AsyncMock(return_value=task)
        registry.get_by_task_id = mock.MagicMock(return_value=task)

        bot = MattermostBot(
            "https://mm.example.com",
            "token",
            "channel-id",
        )
        bot._bot_user_id = "bot123"
        bot._registry = registry
        bot.post = mock.AsyncMock(return_value=["msg1"])

        post = {
            "id": "msg1",
            "message": "!start /tmp",
            "user_id": "user1",
            "channel_id": "channel-id",
            "root_id": None,
        }

        await bot._handle_event("posted", {"post": post})

        # Command should be intercepted, not passed to on_message
        bot.post.assert_called_once()
        registry.spawn_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_event_text_command_in_thread(self):
        """Test that text commands work in threads."""
        registry = mock.MagicMock()
        registry.stop_task = mock.AsyncMock(return_value=True)

        bot = MattermostBot(
            "https://mm.example.com",
            "token",
            "channel-id",
        )
        bot._bot_user_id = "bot123"
        bot._registry = registry
        bot.post = mock.AsyncMock(return_value=["msg1"])

        post = {
            "id": "msg2",
            "message": "!stop",
            "user_id": "user1",
            "channel_id": "channel-id",
            "root_id": "msg1",  # Thread parent
        }

        await bot._handle_event("posted", {"post": post})

        # Should post response to the thread
        bot.post.assert_called_once()
        call_args = bot.post.call_args
        assert call_args[1].get("thread_id") == "msg1"

    @pytest.mark.asyncio
    async def test_handle_event_non_command_message(self):
        """Test that non-command messages pass to on_message."""
        on_msg = mock.AsyncMock()
        registry = mock.MagicMock()

        bot = MattermostBot(
            "https://mm.example.com",
            "token",
            "channel-id",
            on_message=on_msg,
        )
        bot._bot_user_id = "bot123"
        bot._registry = registry

        post = {
            "id": "msg1",
            "message": "hello world",  # No ! prefix
            "user_id": "user1",
            "channel_id": "channel-id",
            "root_id": None,
        }

        await bot._handle_event("posted", {"post": post})

        # Should pass through to on_message callback
        on_msg.assert_called_once_with(post)

    @pytest.mark.asyncio
    async def test_handle_event_own_messages_ignored(self):
        """Test that bot ignores its own messages."""
        on_msg = mock.AsyncMock()
        bot = MattermostBot(
            "https://mm.example.com",
            "token",
            "channel-id",
            on_message=on_msg,
        )
        bot._bot_user_id = "bot123"

        post = {
            "id": "msg1",
            "message": "!start /tmp",
            "user_id": "bot123",  # Bot's own message
            "channel_id": "channel-id",
            "root_id": None,
        }

        await bot._handle_event("posted", {"post": post})

        # Should ignore completely
        on_msg.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_event_command_result_posted(self):
        """Test that command result is posted back to thread."""
        registry = mock.MagicMock()
        registry.list_tasks = mock.AsyncMock(return_value=[])

        bot = MattermostBot(
            "https://mm.example.com",
            "token",
            "channel-id",
        )
        bot._bot_user_id = "bot123"
        bot._registry = registry
        bot.post = mock.AsyncMock(return_value=["msg1"])

        post = {
            "id": "msg1",
            "message": "!list",
            "user_id": "user1",
            "channel_id": "channel-id",
            "root_id": "thread1",
        }

        await bot._handle_event("posted", {"post": post})

        # Result should be posted
        bot.post.assert_called_once()
        result_msg = bot.post.call_args[0][0]
        assert "No active tasks" in result_msg or "Active" in result_msg


class TestMattermostBotApprovalRouterBinding:
    """Tests for bind_approval_router binding."""

    def test_bind_approval_router(self):
        """Test bind_approval_router stores the router."""
        bot = MattermostBot("https://mm.example.com", "token", "channel-id")
        router = mock.MagicMock()

        bot.bind_approval_router(router)

        assert bot._approval_router is router

    def test_bind_approval_router_initially_none(self):
        """Test approval_router is None before binding."""
        bot = MattermostBot("https://mm.example.com", "token", "channel-id")

        assert bot._approval_router is None


class TestMattermostBotApprovalTextResolution:
    """Tests for text-based approval/TUI resolution in threads."""

    @pytest.mark.asyncio
    async def test_text_reply_in_thread_resolves_approval(self):
        """Test text reply in thread with pending approval resolves as deny."""
        on_msg = mock.AsyncMock()
        bot = MattermostBot(
            "https://mm.example.com",
            "token",
            "channel-id",
            on_message=on_msg,
        )
        bot._bot_user_id = "bot123"

        # Mock the approval router
        approval_router = mock.AsyncMock()
        approval_router.resolve_by_text = mock.AsyncMock(return_value=True)
        approval_router.resolve_tui_by_text = mock.AsyncMock()
        bot.bind_approval_router(approval_router)

        post = {
            "id": "msg2",
            "message": "deny because of security",
            "user_id": "user1",
            "channel_id": "channel-id",
            "root_id": "msg1",  # In a thread
        }

        await bot._handle_event("posted", {"post": post})

        # resolve_by_text should be called
        approval_router.resolve_by_text.assert_called_once_with(
            "msg1", "deny because of security", author_is_bot=False
        )
        # on_message should NOT be called since approval was resolved
        on_msg.assert_not_called()

    @pytest.mark.asyncio
    async def test_text_reply_in_thread_resolves_tui_when_approval_fails(self):
        """Test text reply tries TUI resolver if approval resolver returns False."""
        on_msg = mock.AsyncMock()
        bot = MattermostBot(
            "https://mm.example.com",
            "token",
            "channel-id",
            on_message=on_msg,
        )
        bot._bot_user_id = "bot123"

        # Mock the approval router
        approval_router = mock.AsyncMock()
        approval_router.resolve_by_text = mock.AsyncMock(return_value=False)
        approval_router.resolve_tui_by_text = mock.AsyncMock(return_value=True)
        bot.bind_approval_router(approval_router)

        post = {
            "id": "msg2",
            "message": "option 1",
            "user_id": "user1",
            "channel_id": "channel-id",
            "root_id": "msg1",  # In a thread
        }

        await bot._handle_event("posted", {"post": post})

        # resolve_by_text should be called first
        approval_router.resolve_by_text.assert_called_once_with(
            "msg1", "option 1", author_is_bot=False
        )
        # resolve_tui_by_text should be called second
        approval_router.resolve_tui_by_text.assert_called_once_with(
            "msg1", "option 1", author_is_bot=False
        )
        # on_message should NOT be called since TUI was resolved
        on_msg.assert_not_called()

    @pytest.mark.asyncio
    async def test_text_reply_in_thread_passes_to_on_message_if_no_approval(self):
        """Test text reply without pending prompt passes to on_message."""
        on_msg = mock.AsyncMock()
        bot = MattermostBot(
            "https://mm.example.com",
            "token",
            "channel-id",
            on_message=on_msg,
        )
        bot._bot_user_id = "bot123"

        # Mock the approval router
        approval_router = mock.AsyncMock()
        approval_router.resolve_by_text = mock.AsyncMock(return_value=False)
        approval_router.resolve_tui_by_text = mock.AsyncMock(return_value=False)
        bot.bind_approval_router(approval_router)

        post = {
            "id": "msg2",
            "message": "just a regular message",
            "user_id": "user1",
            "channel_id": "channel-id",
            "root_id": "msg1",  # In a thread
        }

        await bot._handle_event("posted", {"post": post})

        # resolve_by_text and resolve_tui_by_text should be called
        approval_router.resolve_by_text.assert_called_once()
        approval_router.resolve_tui_by_text.assert_called_once()
        # on_message should be called since nothing was resolved
        on_msg.assert_called_once_with(post)

    @pytest.mark.asyncio
    async def test_text_reply_without_approval_router_passes_to_on_message(self):
        """Test text reply without router bound still passes to on_message."""
        on_msg = mock.AsyncMock()
        bot = MattermostBot(
            "https://mm.example.com",
            "token",
            "channel-id",
            on_message=on_msg,
        )
        bot._bot_user_id = "bot123"
        # Don't bind approval router

        post = {
            "id": "msg2",
            "message": "message in thread",
            "user_id": "user1",
            "channel_id": "channel-id",
            "root_id": "msg1",  # In a thread
        }

        await bot._handle_event("posted", {"post": post})

        # on_message should be called since there's no router
        on_msg.assert_called_once_with(post)

    @pytest.mark.asyncio
    async def test_text_reply_not_in_thread_skips_approval_check(self):
        """Test top-level messages don't check approval resolver."""
        on_msg = mock.AsyncMock()
        bot = MattermostBot(
            "https://mm.example.com",
            "token",
            "channel-id",
            on_message=on_msg,
        )
        bot._bot_user_id = "bot123"

        # Mock the approval router
        approval_router = mock.AsyncMock()
        bot.bind_approval_router(approval_router)

        post = {
            "id": "msg1",
            "message": "top-level message",
            "user_id": "user1",
            "channel_id": "channel-id",
            # No root_id = top-level message
        }

        await bot._handle_event("posted", {"post": post})

        # Approval resolvers should NOT be called for top-level messages
        approval_router.resolve_by_text.assert_not_called()
        approval_router.resolve_tui_by_text.assert_not_called()
        # on_message should be called
        on_msg.assert_called_once_with(post)

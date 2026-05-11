"""Tests for ChatPlatform protocol and implementations."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from bridge.platform import ChatPlatform, RichFormatter
from tests.fakes import FakePlatform


def test_fake_platform_implements_protocol() -> None:
    """Verify FakePlatform satisfies ChatPlatform protocol via runtime_checkable."""
    fake = FakePlatform()
    assert isinstance(fake, ChatPlatform)


@pytest.mark.asyncio
async def test_fake_platform_post() -> None:
    """Test FakePlatform.post() tracking."""
    fake = FakePlatform()

    # Post to main channel
    msg_ids = await fake.post("Hello world")
    assert len(msg_ids) == 1
    assert msg_ids[0] == "1001"
    assert len(fake._post_calls) == 1
    assert fake._post_calls[0]["content"] == "Hello world"
    assert fake._post_calls[0]["thread_id"] is None

    # Post to thread
    msg_ids = await fake.post("Thread message", thread_id="2001")
    assert len(msg_ids) == 1
    assert msg_ids[0] == "1002"
    assert len(fake._post_calls) == 2
    assert fake._post_calls[1]["thread_id"] == "2001"


@pytest.mark.asyncio
async def test_fake_platform_post_with_attachments() -> None:
    """Test FakePlatform.post_with_attachments() tracking."""
    fake = FakePlatform()

    file_paths = [Path("/tmp/file1.txt"), Path("/tmp/file2.txt")]
    msg_ids = await fake.post_with_attachments(
        file_paths, thread_id="2001", text="Files here"
    )

    assert len(msg_ids) == 1
    assert msg_ids[0] == "1001"
    assert len(fake._attachment_calls) == 1
    call = fake._attachment_calls[0]
    assert call["file_paths"] == file_paths
    assert call["thread_id"] == "2001"
    assert call["text"] == "Files here"


@pytest.mark.asyncio
async def test_fake_platform_create_thread() -> None:
    """Test FakePlatform.create_thread() returns string IDs."""
    fake = FakePlatform()

    tid1 = await fake.create_thread("Thread 1")
    tid2 = await fake.create_thread("Thread 2")

    assert isinstance(tid1, str)
    assert isinstance(tid2, str)
    assert tid1 == "2001"
    assert tid2 == "2002"
    assert len(fake._thread_calls) == 2
    assert fake._thread_calls[0]["name"] == "Thread 1"


@pytest.mark.asyncio
async def test_fake_platform_archive_thread() -> None:
    """Test FakePlatform.archive_thread() tracking."""
    fake = FakePlatform()

    await fake.archive_thread("2001")

    assert len(fake._archive_calls) == 1
    assert fake._archive_calls[0]["thread_id"] == "2001"


@pytest.mark.asyncio
async def test_fake_platform_thread_alive() -> None:
    """Test FakePlatform.thread_alive() always returns True."""
    fake = FakePlatform()

    # Fake always returns True
    assert await fake.thread_alive("2001") is True
    assert await fake.thread_alive("nonexistent") is True


@pytest.mark.asyncio
async def test_fake_platform_add_reactions() -> None:
    """Test FakePlatform.add_reactions() tracking."""
    fake = FakePlatform()

    await fake.add_reactions("1001", "2001", ["👍", "❌"])

    assert len(fake._reaction_calls) == 1
    call = fake._reaction_calls[0]
    assert call["message_id"] == "1001"
    assert call["thread_id"] == "2001"
    assert call["emoji"] == ["👍", "❌"]


@pytest.mark.asyncio
async def test_fake_platform_edit_message() -> None:
    """Test FakePlatform.edit_message() tracking."""
    fake = FakePlatform()

    await fake.edit_message("2001", "1001", content="Updated text")

    assert len(fake._edit_calls) == 1
    call = fake._edit_calls[0]
    assert call["thread_id"] == "2001"
    assert call["message_id"] == "1001"
    assert call["content"] == "Updated text"


@pytest.mark.asyncio
async def test_fake_platform_download_attachment() -> None:
    """Test FakePlatform.download_attachment() tracking."""
    fake = FakePlatform()
    dest_dir = Path("/tmp/attachments")

    path = await fake.download_attachment("some_ref", dest_dir)

    assert path == dest_dir / "fake_download.txt"
    assert len(fake._download_calls) == 1
    assert fake._download_calls[0]["ref"] == "some_ref"
    assert fake._download_calls[0]["dest_dir"] == dest_dir


@pytest.mark.asyncio
async def test_fake_platform_fetch_messageable() -> None:
    """Test FakePlatform.fetch_messageable() returns a messageable object."""
    fake = FakePlatform()

    messageable = await fake.fetch_messageable("2001")

    # Should return a FakeBotChannel with typing context
    assert hasattr(messageable, "typing")


@pytest.mark.asyncio
async def test_fake_platform_start_close() -> None:
    """Test FakePlatform.start() and close()."""
    fake = FakePlatform()

    await fake.start()
    assert fake.is_ready is True

    await fake.close()
    assert fake.is_ready is True  # No-op, still ready


def _has_protocol_methods(cls: type) -> bool:
    """Check that cls has all ChatPlatform protocol methods.

    Verifies that a class structurally conforms to the ChatPlatform protocol
    by checking for the presence of all required methods.
    """
    import inspect

    protocol_methods = [
        name
        for name, member in inspect.getmembers(ChatPlatform, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]
    for method in protocol_methods:
        if not hasattr(cls, method):
            return False
    return True


def test_discord_bot_satisfies_protocol() -> None:
    """DiscordBot structurally conforms to ChatPlatform.

    Note: We cannot instantiate DiscordBot without Discord credentials,
    so we use structural checks (hasattr/inspect) on the class itself.
    """
    from bridge.backends.discord.bot import DiscordBot

    assert _has_protocol_methods(DiscordBot)


def test_mattermost_bot_satisfies_protocol() -> None:
    """MattermostBot structurally conforms to ChatPlatform."""
    from bridge.backends.mattermost.bot import MattermostBot

    assert _has_protocol_methods(MattermostBot)


def test_fake_platform_satisfies_protocol() -> None:
    """FakePlatform structurally conforms to ChatPlatform."""
    assert _has_protocol_methods(FakePlatform)

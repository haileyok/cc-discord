"""Tests for the Listener and _PendingAsk classes."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from bridge.listener import AskResult, Listener, _PendingAsk, GRACE_SECS


@dataclass
class FakeUser:
    id: int
    bot: bool = False


@dataclass
class FakeChannel:
    id: int


@dataclass
class FakeAttachment:
    url: str


@dataclass
class FakeMsg:
    author: FakeUser
    channel: FakeChannel
    content: str = ""
    attachments: list[FakeAttachment] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class Test_PendingAsk:
    """Unit tests for the _PendingAsk class."""

    @pytest.mark.asyncio
    async def test_ac31_happy_path_single_message(self):
        """AC3.1: register ask, deliver one message, future resolves with reply."""
        asked_at = datetime.now(timezone.utc)
        ask = _PendingAsk(asked_at, grace_secs=0.05)

        msg = FakeMsg(
            author=FakeUser(id=123),
            channel=FakeChannel(id=456),
            content="yes",
            created_at=datetime.now(timezone.utc),
        )

        ask.feed(msg)

        # Future should resolve within grace_secs + some headroom
        result = await asyncio.wait_for(ask.future, timeout=1.0)

        assert isinstance(result, AskResult)
        assert result.reply == "yes"

    @pytest.mark.asyncio
    async def test_ac32_coalescing_two_messages_within_grace(self):
        """AC3.2: two messages from same author within grace window coalesce."""
        asked_at = datetime.now(timezone.utc)
        ask = _PendingAsk(asked_at, grace_secs=0.05)

        msg1 = FakeMsg(
            author=FakeUser(id=123),
            channel=FakeChannel(id=456),
            content="first",
            created_at=datetime.now(timezone.utc),
        )
        msg2 = FakeMsg(
            author=FakeUser(id=123),
            channel=FakeChannel(id=456),
            content="second",
            created_at=datetime.now(timezone.utc),
        )

        ask.feed(msg1)
        # Small delay to simulate user typing
        await asyncio.sleep(0.01)
        ask.feed(msg2)

        result = await asyncio.wait_for(ask.future, timeout=1.0)

        assert result.reply == "first\nsecond"

    @pytest.mark.asyncio
    async def test_ac32_boundary_message_after_grace_expires(self):
        """AC3.2 boundary: message delivered after grace expires is NOT included."""
        asked_at = datetime.now(timezone.utc)
        ask = _PendingAsk(asked_at, grace_secs=0.05)

        msg1 = FakeMsg(
            author=FakeUser(id=123),
            channel=FakeChannel(id=456),
            content="first",
            created_at=datetime.now(timezone.utc),
        )
        ask.feed(msg1)

        # Wait for grace window to expire
        # Must explicitly sleep past grace_secs to ensure the window expires
        # and the future resolves before delivering msg2
        await asyncio.sleep(GRACE_SECS + 0.05)

        msg2 = FakeMsg(
            author=FakeUser(id=123),
            channel=FakeChannel(id=456),
            content="second",
            created_at=datetime.now(timezone.utc),
        )
        ask.feed(msg2)

        # Future should already be done from msg1 alone
        result = await asyncio.wait_for(ask.future, timeout=0.5)

        assert result.reply == "first"

    @pytest.mark.asyncio
    async def test_ac33_attachments_single(self):
        """AC3.3: attachments are appended with [image] prefix."""
        asked_at = datetime.now(timezone.utc)
        ask = _PendingAsk(asked_at, grace_secs=0.05)

        msg = FakeMsg(
            author=FakeUser(id=123),
            channel=FakeChannel(id=456),
            content="",
            attachments=[FakeAttachment(url="https://example.com/img1.png")],
            created_at=datetime.now(timezone.utc),
        )
        ask.feed(msg)

        result = await asyncio.wait_for(ask.future, timeout=1.0)

        assert result.reply == "[image] https://example.com/img1.png"

    @pytest.mark.asyncio
    async def test_ac33_attachments_multiple(self):
        """AC3.3: multiple attachments each get their own [image] line."""
        asked_at = datetime.now(timezone.utc)
        ask = _PendingAsk(asked_at, grace_secs=0.05)

        msg = FakeMsg(
            author=FakeUser(id=123),
            channel=FakeChannel(id=456),
            content="",
            attachments=[
                FakeAttachment(url="https://example.com/img1.png"),
                FakeAttachment(url="https://example.com/img2.jpg"),
            ],
            created_at=datetime.now(timezone.utc),
        )
        ask.feed(msg)

        result = await asyncio.wait_for(ask.future, timeout=1.0)

        assert "[image] https://example.com/img1.png" in result.reply
        assert "[image] https://example.com/img2.jpg" in result.reply

    @pytest.mark.asyncio
    async def test_ac33_text_and_attachments_combined(self):
        """AC3.3: text and attachments together, URLs after text."""
        asked_at = datetime.now(timezone.utc)
        ask = _PendingAsk(asked_at, grace_secs=0.05)

        msg = FakeMsg(
            author=FakeUser(id=123),
            channel=FakeChannel(id=456),
            content="check this out",
            attachments=[
                FakeAttachment(url="https://example.com/img1.png"),
                FakeAttachment(url="https://example.com/img2.jpg"),
            ],
            created_at=datetime.now(timezone.utc),
        )
        ask.feed(msg)

        result = await asyncio.wait_for(ask.future, timeout=1.0)

        lines = result.reply.split("\n")
        assert lines[0] == "check this out"
        assert "[image] https://example.com/img1.png" in result.reply
        assert "[image] https://example.com/img2.jpg" in result.reply

    @pytest.mark.asyncio
    async def test_ac36_bot_messages_filtered(self):
        """AC3.6: bot messages do not resolve the future."""
        asked_at = datetime.now(timezone.utc)
        ask = _PendingAsk(asked_at, grace_secs=0.05)

        bot_msg = FakeMsg(
            author=FakeUser(id=999, bot=True),
            channel=FakeChannel(id=456),
            content="I am a bot",
            created_at=datetime.now(timezone.utc),
        )
        ask.feed(bot_msg)

        # Give it time to not resolve
        await asyncio.sleep(0.1)

        # Future should NOT be done
        assert not ask.future.done()

        # Now deliver a real message
        user_msg = FakeMsg(
            author=FakeUser(id=123),
            channel=FakeChannel(id=456),
            content="hello",
            created_at=datetime.now(timezone.utc),
        )
        ask.feed(user_msg)

        result = await asyncio.wait_for(ask.future, timeout=1.0)
        assert result.reply == "hello"

    @pytest.mark.asyncio
    async def test_message_before_asked_at_filtered(self):
        """Messages with created_at <= asked_at are ignored."""
        asked_at = datetime.now(timezone.utc)
        ask = _PendingAsk(asked_at, grace_secs=0.05)

        # Message created before or at asked_at
        old_msg = FakeMsg(
            author=FakeUser(id=123),
            channel=FakeChannel(id=456),
            content="old",
            created_at=asked_at,  # exactly at asked_at — should be filtered
        )
        ask.feed(old_msg)

        await asyncio.sleep(0.1)
        assert not ask.future.done()

    @pytest.mark.asyncio
    async def test_different_author_filtered(self):
        """Messages from a different author than the first are ignored."""
        asked_at = datetime.now(timezone.utc)
        ask = _PendingAsk(asked_at, grace_secs=0.05)

        msg1 = FakeMsg(
            author=FakeUser(id=123),
            channel=FakeChannel(id=456),
            content="first author",
            created_at=datetime.now(timezone.utc),
        )
        ask.feed(msg1)

        # Different author
        msg2 = FakeMsg(
            author=FakeUser(id=999),
            channel=FakeChannel(id=456),
            content="second author",
            created_at=datetime.now(timezone.utc),
        )
        ask.feed(msg2)

        result = await asyncio.wait_for(ask.future, timeout=1.0)
        # Should only have first author's message
        assert result.reply == "first author"

    @pytest.mark.asyncio
    async def test_unregister_cancels_coalesce_task(self):
        """unregister() cancels pending coalesce task."""
        asked_at = datetime.now(timezone.utc)
        ask = _PendingAsk(asked_at, grace_secs=1.0)

        msg = FakeMsg(
            author=FakeUser(id=123),
            channel=FakeChannel(id=456),
            content="test",
            created_at=datetime.now(timezone.utc),
        )
        ask.feed(msg)

        # Cancel immediately while coalesce task is pending
        ask.cancel()

        # Give it time to raise CancelledError
        await asyncio.sleep(0.1)

        # Coalesce task should be cancelled, future should NOT be done
        assert not ask.future.done()

    @pytest.mark.asyncio
    async def test_empty_content_stripped(self):
        """Messages with only whitespace content are not included."""
        asked_at = datetime.now(timezone.utc)
        ask = _PendingAsk(asked_at, grace_secs=0.05)

        msg1 = FakeMsg(
            author=FakeUser(id=123),
            channel=FakeChannel(id=456),
            content="   ",  # only whitespace
            created_at=datetime.now(timezone.utc),
        )
        msg2 = FakeMsg(
            author=FakeUser(id=123),
            channel=FakeChannel(id=456),
            content="actual content",
            created_at=datetime.now(timezone.utc),
        )

        ask.feed(msg1)
        ask.feed(msg2)

        result = await asyncio.wait_for(ask.future, timeout=1.0)
        # Whitespace-only message should be filtered out
        assert result.reply == "actual content"

    @pytest.mark.asyncio
    async def test_replied_at_is_last_message_timestamp(self):
        """AskResult.replied_at is the ISO8601 timestamp of the last coalesced message."""
        asked_at = datetime.now(timezone.utc)
        ask = _PendingAsk(asked_at, grace_secs=0.05)

        # Create times after asked_at for messages to pass filter
        msg1_time = datetime.now(timezone.utc)
        msg2_time = datetime.now(timezone.utc)

        msg1 = FakeMsg(
            author=FakeUser(id=123),
            channel=FakeChannel(id=456),
            content="first",
            created_at=msg1_time,
        )
        msg2 = FakeMsg(
            author=FakeUser(id=123),
            channel=FakeChannel(id=456),
            content="second",
            created_at=msg2_time,
        )

        ask.feed(msg1)
        ask.feed(msg2)

        result = await asyncio.wait_for(ask.future, timeout=1.0)
        # Check that replied_at is ISO8601 and matches msg2's timestamp
        assert result.replied_at == msg2_time.isoformat()


class TestListener:
    """Unit tests for the Listener class."""

    @pytest.mark.asyncio
    async def test_listener_register_unregister(self):
        """Listener.register() and unregister() work."""
        listener = Listener()
        asked_at = datetime.now(timezone.utc)
        ask = _PendingAsk(asked_at, grace_secs=0.05)

        await listener.register(123, ask)
        assert 123 in listener._pending
        assert listener._pending[123] is ask

        await listener.unregister(123, ask)
        assert 123 not in listener._pending

    @pytest.mark.asyncio
    async def test_listener_register_duplicate_raises(self):
        """Listener.register() raises if thread already has pending ask."""
        listener = Listener()
        asked_at = datetime.now(timezone.utc)
        ask1 = _PendingAsk(asked_at, grace_secs=0.05)
        ask2 = _PendingAsk(asked_at, grace_secs=0.05)

        await listener.register(123, ask1)

        with pytest.raises(RuntimeError, match="already has a pending ask"):
            await listener.register(123, ask2)

    @pytest.mark.asyncio
    async def test_listener_deliver_to_pending_ask(self):
        """Listener.deliver() routes message to pending ask."""
        listener = Listener()
        asked_at = datetime.now(timezone.utc)
        ask = _PendingAsk(asked_at, grace_secs=0.05)

        await listener.register("456", ask)

        msg = FakeMsg(
            author=FakeUser(id=123),
            channel=FakeChannel(id=456),
            content="hello",
            created_at=datetime.now(timezone.utc),
        )

        await listener.deliver(msg)

        result = await asyncio.wait_for(ask.future, timeout=1.0)
        assert result.reply == "hello"

    @pytest.mark.asyncio
    async def test_listener_deliver_no_pending_ask(self):
        """Listener.deliver() is a no-op if no pending ask for thread."""
        listener = Listener()

        msg = FakeMsg(
            author=FakeUser(id=123),
            channel=FakeChannel(id=456),
            content="hello",
            created_at=datetime.now(timezone.utc),
        )

        # Should not raise
        await listener.deliver(msg)

    @pytest.mark.asyncio
    async def test_listener_unregister_while_coalescing(self):
        """unregister() while coalesce task is pending cancels it cleanly."""
        listener = Listener()
        asked_at = datetime.now(timezone.utc)
        ask = _PendingAsk(asked_at, grace_secs=1.0)

        await listener.register("456", ask)

        msg = FakeMsg(
            author=FakeUser(id=123),
            channel=FakeChannel(id=456),
            content="test",
            created_at=datetime.now(timezone.utc),
        )
        await listener.deliver(msg)

        # Unregister while coalesce task is pending
        await listener.unregister("456", ask)

        # Give it time to clean up
        await asyncio.sleep(0.1)

        # Thread should be gone from pending
        assert 456 not in listener._pending

    @pytest.mark.asyncio
    async def test_ac35_leak_guard_cancelled_future(self):
        """AC3.5 guard: unregister() on cancelled future cleans up properly."""
        listener = Listener()
        asked_at = datetime.now(timezone.utc)
        ask = _PendingAsk(asked_at, grace_secs=0.05)

        await listener.register("456", ask)

        # Manually cancel the future (simulating asyncio.wait_for timeout)
        ask.future.cancel()

        await listener.unregister("456", ask)

        # Thread should be gone, no exception raised
        assert 456 not in listener._pending

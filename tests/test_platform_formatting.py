"""Tests for ChatPlatform formatting methods.

Tests format_mention() and format_channel_link() across all platform backends.
"""

from __future__ import annotations

import pytest

from tests.fakes import FakePlatform


@pytest.mark.asyncio
class TestPlatformFormatting:
    """Test formatting methods on ChatPlatform implementations."""

    async def test_fake_platform_format_mention(self) -> None:
        """FakePlatform.format_mention returns @user_id."""
        platform = FakePlatform()
        result = platform.format_mention("12345")
        assert result == "@12345"

    async def test_fake_platform_format_channel_link(self) -> None:
        """FakePlatform.format_channel_link returns #channel_id."""
        platform = FakePlatform()
        result = platform.format_channel_link("67890")
        assert result == "#67890"

    async def test_fake_platform_format_mention_with_various_ids(self) -> None:
        """FakePlatform.format_mention works with different ID formats."""
        platform = FakePlatform()
        assert platform.format_mention("user123") == "@user123"
        assert platform.format_mention("a-b-c") == "@a-b-c"
        assert platform.format_mention("") == "@"

    async def test_fake_platform_format_channel_link_with_various_ids(
        self,
    ) -> None:
        """FakePlatform.format_channel_link works with different ID formats."""
        platform = FakePlatform()
        assert platform.format_channel_link("general") == "#general"
        assert platform.format_channel_link("channel-1") == "#channel-1"
        assert platform.format_channel_link("") == "#"

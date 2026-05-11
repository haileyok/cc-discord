"""Discord rich formatting implementation (embeds)."""

from __future__ import annotations

import logging
from typing import Any

import discord

from bridge.backends.discord.bot import DiscordBot

logger = logging.getLogger(__name__)


class DiscordRichFormatter:
    """Formats rich content (embeds) for Discord.

    Implements the RichFormatter protocol by wrapping a DiscordBot instance
    and calling its post_embed / edit_message methods with Discord-specific
    embed objects.
    """

    def __init__(self, bot: DiscordBot) -> None:
        """Initialize with a DiscordBot instance."""
        self._bot = bot

    async def post_rich(
        self, thread_id: str, block_type: str, data: dict[str, Any]
    ) -> str:
        """Post a rich block (currently only Discord embeds supported).

        Args:
            thread_id: The thread ID.
            block_type: Type of block (e.g., "subagent_block", "task_list").
            data: Block-specific data. For Discord, this should contain
                  an "embed" key with a discord.Embed object.

        Returns:
            The message ID of the posted embed.
        """
        if block_type in ("subagent_block", "task_list"):
            embed = data.get("embed")
            if not isinstance(embed, discord.Embed):
                raise ValueError(f"Expected discord.Embed for {block_type}, got {type(embed)}")
            return await self._bot.post_embed(embed, thread_id=thread_id)
        raise NotImplementedError(f"Unknown block type: {block_type}")

    async def edit_rich(
        self,
        thread_id: str,
        message_id: str,
        block_type: str,
        data: dict[str, Any],
    ) -> None:
        """Edit a rich block.

        Args:
            thread_id: The thread ID.
            message_id: The message ID to edit.
            block_type: Type of block.
            data: Updated block data (should contain "embed" key).
        """
        if block_type in ("subagent_block", "task_list"):
            embed = data.get("embed")
            if not isinstance(embed, discord.Embed):
                raise ValueError(f"Expected discord.Embed for {block_type}, got {type(embed)}")
            await self._bot.edit_message(
                thread_id, message_id, embed=embed
            )
            return
        raise NotImplementedError(f"Unknown block type: {block_type}")

"""Discord rich formatting implementation (embeds)."""

from __future__ import annotations

import logging
from typing import Any

import discord

from bridge.backends.discord.bot import DiscordBot

logger = logging.getLogger(__name__)


def _build_task_list_embed(data: dict[str, Any]) -> discord.Embed:
    """Build a Discord Embed for a task_list block from plain data.

    Expected data keys:
      - "entries": list of dicts with "id", "status", "subject"
      - "done": int count of completed tasks
      - "total": int total task count
      - "in_progress": int count of in-progress tasks
    """
    entries = data.get("entries", [])
    done = data.get("done", 0)
    total = data.get("total", 0)
    in_progress = data.get("in_progress", 0)

    lines: list[str] = []
    for entry in entries:
        tid = entry.get("id", "?")
        status = entry.get("status", "pending")
        subject = entry.get("subject", "")
        if status == "completed":
            mark = "✅"
        elif status == "in_progress":
            mark = "▶️"
        elif status == "deleted":
            mark = "🗑"
        else:
            mark = "⬜"
        line = f"{mark} #{tid}"
        if subject:
            line += f" {subject}"
        lines.append(line)

    description = "\n".join(lines) or "_(no tasks)_"
    if len(description) > 4000:
        description = description[:3997] + "…"

    # Color: green when everything's done, yellow if any in progress,
    # otherwise neutral grey.
    if total > 0 and done == total:
        color = 0x57F287
    elif in_progress > 0:
        color = 0xFEE75C
    else:
        color = 0x95A5A6

    embed = discord.Embed(
        title="📋 Tasks",
        description=description,
        color=color,
    )
    embed.set_footer(text=f"{done}/{total} done")
    return embed


def _build_subagent_embed(data: dict[str, Any]) -> discord.Embed:
    """Build a Discord Embed for a subagent_block from plain data.

    Expected data keys:
      - "attribution": str agent name/label
      - "actions": list[str] of recent action lines
      - "total_actions": int total action count
      - "finished": bool whether the subagent has finished
      - "duration": str human-readable elapsed time (e.g. "30s", "1.5m")
    """
    attribution = data.get("attribution", "")
    actions = data.get("actions", [])
    total_actions = data.get("total_actions", 0)
    finished = data.get("finished", False)
    duration = data.get("duration", "0s")

    status = "finished" if finished else "running"
    # Color cues: yellow while running, green when finished cleanly.
    color = 0x57F287 if finished else 0xFEE75C  # discord brand yellow/green

    # Discord embed.description hard cap is 4096; truncate safely.
    description = "\n".join(actions)
    if len(description) > 3900:
        description = description[:3897] + "…"

    embed = discord.Embed(
        title=f"🤖 {attribution}",
        description=description or "_(no actions yet)_",
        color=color,
    )
    embed.set_footer(text=f"{status} · {total_actions} actions · {duration}")
    return embed


class DiscordRichFormatter:
    """Formats rich content (embeds) for Discord.

    Implements the RichFormatter protocol by wrapping a DiscordBot instance
    and calling its post_embed / edit_message methods with Discord-specific
    embed objects built from plain data dicts.
    """

    def __init__(self, bot: DiscordBot) -> None:
        """Initialize with a DiscordBot instance."""
        self._bot = bot

    async def post_rich(
        self, thread_id: str, block_type: str, data: dict[str, Any]
    ) -> str:
        """Post a rich block as a Discord embed.

        Args:
            thread_id: The thread ID.
            block_type: Type of block (e.g., "subagent_block", "task_list").
            data: Block-specific data dict. Each block type has its own
                  expected keys — see _build_*_embed helpers above.

        Returns:
            The message ID of the posted embed.
        """
        if block_type == "task_list":
            embed = _build_task_list_embed(data)
            return await self._bot.post_embed(embed, thread_id=thread_id)
        if block_type == "subagent_block":
            embed = _build_subagent_embed(data)
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
            data: Updated block data dict.
        """
        if block_type == "task_list":
            embed = _build_task_list_embed(data)
            await self._bot.edit_message(thread_id, message_id, embed=embed)
            return
        if block_type == "subagent_block":
            embed = _build_subagent_embed(data)
            await self._bot.edit_message(thread_id, message_id, embed=embed)
            return
        raise NotImplementedError(f"Unknown block type: {block_type}")

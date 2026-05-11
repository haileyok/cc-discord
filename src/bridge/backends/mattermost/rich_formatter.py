"""Mattermost RichFormatter implementation using markdown."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bridge.backends.mattermost.formatting import (
    format_subagent_block,
    format_task_list,
    format_task_todos,
)

if TYPE_CHECKING:
    from bridge.backends.mattermost.bot import MattermostBot


class MattermostRichFormatter:
    """RichFormatter implementation using Mattermost markdown.

    Renders rich content (task lists, subagent blocks, todos) as markdown
    and posts them via the MattermostBot interface.
    """

    def __init__(self, bot: MattermostBot) -> None:
        """Initialize with a MattermostBot instance.

        Args:
            bot: The MattermostBot instance to use for posting/editing messages.
        """
        self._bot = bot

    async def post_rich(
        self, thread_id: str, block_type: str, data: dict[str, Any]
    ) -> str:
        """Post a rich-formatted block as markdown.

        Args:
            thread_id: The ID of the thread/channel.
            block_type: Type of rich block (e.g., "subagent_block", "task_list").
            data: Block-specific data.

        Returns:
            The ID of the posted message.
        """
        if block_type == "subagent_block":
            text = format_subagent_block(
                data["attribution"],
                data["actions"],
                data["total_actions"],
                data["finished"],
                data["duration"],
            )
        elif block_type == "task_list":
            text = format_task_list(data["tasks"])
        elif block_type == "todo_list":
            text = format_task_todos(data["todos"])
        else:
            text = str(data)

        msg_ids = await self._bot.post(text, thread_id=thread_id)
        return msg_ids[0]

    async def edit_rich(
        self,
        thread_id: str,
        message_id: str,
        block_type: str,
        data: dict[str, Any],
    ) -> None:
        """Edit a rich-formatted block.

        Args:
            thread_id: The ID of the thread/channel.
            message_id: The ID of the message to edit.
            block_type: Type of rich block.
            data: Updated block data.
        """
        if block_type == "subagent_block":
            text = format_subagent_block(
                data["attribution"],
                data["actions"],
                data["total_actions"],
                data["finished"],
                data["duration"],
            )
        else:
            text = str(data)

        await self._bot.edit_message(thread_id, message_id, content=text)

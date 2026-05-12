"""ChatPlatform protocol for backend-agnostic chat operations."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ChatPlatform(Protocol):
    """Protocol for chat platform backends (Discord, Mattermost, etc.).

    All IDs are strings. Implementations handle their own type conversions
    at the platform boundary.
    """

    @property
    def is_ready(self) -> bool:
        """Whether the platform is ready to handle requests."""
        ...

    async def start(self) -> None:
        """Start the platform connection."""
        ...

    async def close(self) -> None:
        """Close the platform connection."""
        ...

    async def post(
        self, message: str, *, thread_id: str | None = None
    ) -> list[str]:
        """Post a message to the channel or thread.

        Args:
            message: The text content to post.
            thread_id: Optional thread/channel ID. If None, posts to the main channel.

        Returns:
            List of message IDs that were created (may be multiple if chunked).
        """
        ...

    async def post_with_attachments(
        self,
        file_paths: list[Path],
        *,
        thread_id: str | None = None,
        text: str | None = None,
    ) -> list[str]:
        """Post attachments to the channel or thread.

        Args:
            file_paths: Paths to files to attach.
            thread_id: Optional thread/channel ID.
            text: Optional accompanying text.

        Returns:
            List of message IDs that were created.
        """
        ...

    async def create_thread(self, name: str) -> str:
        """Create a new thread with the given name.

        Returns:
            The ID of the created thread.
        """
        ...

    async def archive_thread(self, thread_id: str) -> None:
        """Archive a thread by ID."""
        ...

    async def rename_thread(self, thread_id: str, name: str) -> None:
        """Rename a thread."""
        ...

    async def thread_alive(self, thread_id: str) -> bool:
        """Check if a thread still exists."""
        ...

    async def download_attachment(
        self, attachment_ref: Any, dest_dir: Path
    ) -> Path:
        """Download an attachment to the given directory.

        Args:
            attachment_ref: Platform-specific attachment reference object.
            dest_dir: Directory to download to.

        Returns:
            Path to the downloaded file.
        """
        ...

    async def add_reactions(
        self, message_id: str, thread_id: str, emoji: list[str]
    ) -> None:
        """Add emoji reactions to a message.

        Args:
            message_id: The ID of the message to react to.
            thread_id: The ID of the thread/channel containing the message.
            emoji: List of emoji strings.
        """
        ...

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        *,
        content: str | None = None,
    ) -> None:
        """Edit an existing message's text content.

        Args:
            thread_id: The ID of the thread/channel.
            message_id: The ID of the message to edit.
            content: New text content.
        """
        ...

    def start_typing(
        self, thread_id: str
    ) -> contextlib.AbstractAsyncContextManager[None]:
        """Return a context manager that keeps a typing indicator active.

        The indicator is shown until the context is exited. Backends handle
        their own periodic re-send if required by the platform API.

        Args:
            thread_id: The thread/channel to show typing in.
        """
        ...

    async def fetch_messageable(self, thread_id: str) -> Any:
        """Fetch a messageable (channel/thread) object.

        Returns:
            Platform-specific messageable object.
        """
        ...


class RichFormatter(Protocol):
    """Protocol for rich formatting (embeds, markdown blocks, etc.)."""

    async def post_rich(
        self, thread_id: str, block_type: str, data: dict[str, Any]
    ) -> str:
        """Post a rich-formatted block.

        Args:
            thread_id: The ID of the thread/channel.
            block_type: Type of rich block (e.g., "subagent_block", "tasklist").
            data: Block data specific to the type.

        Returns:
            The ID of the posted message.
        """
        ...

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
        ...

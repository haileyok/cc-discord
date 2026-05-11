"""Mattermost backend implementing ChatPlatform protocol."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

from bridge.backends.mattermost.api import MattermostAPI, RateLimitError, MAX_MESSAGE_LENGTH
from bridge.backends.mattermost.ws import MattermostWebSocket

logger = logging.getLogger(__name__)

CHUNK_LIMIT = 3500  # soft limit, well under the 16383 hard limit


class MattermostBot:
    """Mattermost backend implementing ChatPlatform protocol."""

    def __init__(
        self,
        server_url: str,
        token: str,
        channel_id: str,
        *,
        on_message: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_reaction: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        allowed_user_ids: list[str] | None = None,
    ) -> None:
        self._api = MattermostAPI(server_url, token)
        self._channel_id = channel_id
        self._on_message = on_message
        self._on_reaction = on_reaction
        self._allowed_user_ids = set(allowed_user_ids) if allowed_user_ids else None
        self._ws: MattermostWebSocket | None = None
        self._ready = False
        self._bot_user_id: str | None = None

    @property
    def is_ready(self) -> bool:
        """Whether the bot is ready."""
        return self._ready

    async def start(self) -> None:
        """Start the bot."""
        await self._api.start()
        me = await self._api.get_me()
        self._bot_user_id = me["id"]
        self._ws = MattermostWebSocket(
            self._api.base_url, self._api._token, self._handle_event
        )
        await self._ws.start()
        self._ready = True

    async def close(self) -> None:
        """Close the bot."""
        self._ready = False
        if self._ws:
            await self._ws.close()
        await self._api.close()

    async def post(
        self, message: str, *, thread_id: str | None = None
    ) -> list[str]:
        """Post a message to the channel or thread."""
        chunks = _chunk(message, CHUNK_LIMIT)
        msg_ids: list[str] = []
        for chunk in chunks:
            result = await self._api_with_retry(
                lambda c=chunk: self._api.create_post(
                    self._channel_id, c, root_id=thread_id
                )
            )
            msg_ids.append(result["id"])
        return msg_ids

    async def post_with_attachments(
        self,
        file_paths: list[Path],
        *,
        thread_id: str | None = None,
        text: str | None = None,
    ) -> list[str]:
        """Post attachments to the channel or thread."""
        file_ids: list[str] = []
        failed: list[str] = []
        for fp in file_paths:
            try:
                result = await self._api.upload_file(self._channel_id, fp)
                file_infos = result.get("file_infos", [])
                if file_infos:
                    file_ids.append(file_infos[0]["id"])
            except Exception as e:
                logger.warning("File upload failed for %s: %s", fp.name, e)
                failed.append(fp.name)

        msg = text or ""
        if failed:
            msg += f"\n\n⚠️ Failed to upload: {', '.join(failed)}"

        result = await self._api_with_retry(
            lambda: self._api.create_post(
                self._channel_id,
                msg,
                root_id=thread_id,
                file_ids=file_ids if file_ids else None,
            )
        )
        return [result["id"]]

    async def create_thread(self, name: str) -> str:
        """Create a new thread."""
        result = await self._api.create_post(
            self._channel_id,
            f"🟢 cc-bridge task: {name}",
        )
        return result["id"]

    async def archive_thread(self, thread_id: str) -> None:
        """Archive a thread (no-op for Mattermost)."""
        pass

    async def rename_thread(self, thread_id: str, name: str) -> None:
        """Rename a thread."""
        await self._api.update_post(
            thread_id, f"🟢 cc-bridge task: {name}"
        )

    async def thread_alive(self, thread_id: str) -> bool:
        """Check if a thread still exists."""
        result = await self._api.get_post(thread_id)
        return result is not None

    async def download_attachment(
        self, attachment_ref: Any, dest_dir: Path
    ) -> Path:
        """Download an attachment."""
        file_id = attachment_ref["id"]
        filename = attachment_ref.get("name", f"{file_id}.bin")
        data = await self._api.download_file(file_id)
        dest = dest_dir / filename
        dest.write_bytes(data)
        return dest

    async def add_reactions(
        self, message_id: str, thread_id: str, emoji: list[str]
    ) -> None:
        """Add emoji reactions to a message."""
        assert self._bot_user_id is not None
        for name in emoji:
            mm_name = _emoji_to_mattermost(name)
            await self._api.add_reaction(self._bot_user_id, message_id, mm_name)

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        *,
        content: str | None = None,
    ) -> None:
        """Edit an existing message."""
        if content is not None:
            await self._api.update_post(message_id, content)

    async def fetch_messageable(self, thread_id: str) -> Any:
        """Fetch a messageable object."""
        return thread_id

    async def _handle_event(self, event: str, data: dict[str, Any]) -> None:
        """Handle incoming WebSocket events."""
        if event == "posted":
            post = data.get("post", {})
            # Ignore own posts
            if post.get("user_id") == self._bot_user_id:
                return
            # Check allowed users
            if (
                self._allowed_user_ids
                and post.get("user_id") not in self._allowed_user_ids
            ):
                return
            # Only handle posts in our channel
            if post.get("channel_id") != self._channel_id:
                return
            if self._on_message:
                await self._on_message(post)

        elif event == "reaction_added":
            reaction = data.get("reaction", {})
            if reaction.get("user_id") == self._bot_user_id:
                return
            if self._on_reaction:
                await self._on_reaction(reaction)

    async def _api_with_retry(
        self, factory: Callable[[], Awaitable[Any]], max_retries: int = 3
    ) -> Any:
        """Retry API calls with backoff on rate limit or network errors."""
        delays = [0.5, 1.5, 4.0]
        for attempt in range(max_retries):
            try:
                return await factory()
            except RateLimitError as e:
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(e.retry_after)
            except (aiohttp.ClientError, OSError) as e:
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(delays[min(attempt, len(delays) - 1)])


def _chunk(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Split text into chunks, breaking on newlines when possible."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def _emoji_to_mattermost(emoji: str) -> str:
    """Convert Unicode emoji to Mattermost emoji name."""
    mapping = {
        "✅": "white_check_mark",
        "❌": "x",
        "1️⃣": "one",
        "2️⃣": "two",
        "3️⃣": "three",
        "4️⃣": "four",
        "👍": "thumbsup",
        "👎": "thumbsdown",
    }
    return mapping.get(emoji, emoji)

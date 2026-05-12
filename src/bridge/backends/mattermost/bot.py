"""Mattermost backend implementing ChatPlatform protocol."""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import aiohttp

from bridge import voice
from bridge.backends.mattermost.api import MattermostAPI, RateLimitError
from bridge.backends.mattermost.commands import dispatch_text_command, parse_text_command
from bridge.backends.mattermost.ws import MattermostWebSocket

if TYPE_CHECKING:
    from bridge.approvals import ApprovalRouter
    from bridge.tasks import TaskRegistry

logger = logging.getLogger(__name__)

CHUNK_LIMIT = 3500  # soft limit, well under the 16383 hard limit


class MattermostMessageAdapter:
    """Wraps a Mattermost post dict to satisfy the MessageLike protocol.

    Lets the platform-agnostic dispatcher and task router use attribute
    access (.channel.id, .content, .author.bot, .attachments) without
    knowing the message came from Mattermost.
    """

    class _Channel:
        __slots__ = ("id",)
        def __init__(self, thread_id: str) -> None:
            self.id = thread_id

    class _Author:
        __slots__ = ("bot",)
        def __init__(self, *, is_bot: bool) -> None:
            self.bot = is_bot

    __slots__ = ("channel", "content", "author", "attachments", "created_at", "id")

    def __init__(self, post: dict[str, Any], *, bot_user_id: str | None = None) -> None:
        thread_id = post.get("root_id") or post.get("id", "")
        self.channel = self._Channel(thread_id)
        self.content = post.get("message", "")
        self.author = self._Author(is_bot=(post.get("user_id") == bot_user_id))
        self.attachments = []
        self.id = post.get("id")
        create_at = post.get("create_at")
        if create_at:
            self.created_at = datetime.fromtimestamp(create_at / 1000, tz=timezone.utc)
        else:
            self.created_at = datetime.now(tz=timezone.utc)


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
        self._server_url = server_url
        self._token = token
        self._api = MattermostAPI(server_url, token)
        self._channel_id = channel_id
        self._on_message = on_message
        self._on_reaction = on_reaction
        self._allowed_user_ids = set(allowed_user_ids) if allowed_user_ids else None
        self._ws: MattermostWebSocket | None = None
        self._ready = False
        self._bot_user_id: str | None = None
        self._registry: TaskRegistry | None = None
        self._approval_router: ApprovalRouter | None = None

    @property
    def is_ready(self) -> bool:
        """Whether the bot is ready."""
        return self._ready

    @property
    def channel_id(self) -> str:
        return self._channel_id

    def bind_registry(self, registry: TaskRegistry) -> None:
        """Bind a TaskRegistry for text command handling.

        Args:
            registry: TaskRegistry instance for dispatching commands
        """
        self._registry = registry

    def bind_approval_router(self, approval_router: ApprovalRouter) -> None:
        """Bind an ApprovalRouter for handling approval/TUI text-based resolutions.

        Args:
            approval_router: ApprovalRouter instance for resolving text replies
        """
        self._approval_router = approval_router

    async def start(self) -> None:
        """Start the bot."""
        await self._api.start()
        me = await self._api.get_me()
        self._bot_user_id = me["id"]
        self._ws = MattermostWebSocket(
            self._server_url, self._token, self._handle_event
        )
        await self._ws.start()
        self._ready = True

    async def close(self) -> None:
        """Close the bot."""
        self._ready = False
        if self._ws:
            await self._ws.close()
        await self._api.close()

    @contextlib.asynccontextmanager
    async def start_typing(
        self, thread_id: str
    ) -> collections.abc.AsyncIterator[None]:
        """Keep Mattermost typing indicator active by re-posting every 4s."""
        stop = asyncio.Event()

        async def _loop() -> None:
            while not stop.is_set():
                try:
                    assert self._bot_user_id is not None
                    await self._api.trigger_typing(
                        self._bot_user_id, self._channel_id
                    )
                except Exception:
                    logger.debug(
                        "typing indicator failed for thread %s",
                        thread_id,
                        exc_info=True,
                    )
                try:
                    await asyncio.wait_for(stop.wait(), timeout=4.0)
                except TimeoutError:
                    pass

        task = asyncio.create_task(_loop())
        try:
            yield
        finally:
            stop.set()
            await task

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

            # Process audio attachments and integrate transcriptions into message
            if post.get("file_ids"):
                voice_blocks, file_refs = await _process_post_files(post, self._api)
                # Reconstruct message with transcriptions and file refs
                text_parts: list[str] = []
                if post.get("message"):
                    text_parts.append(post["message"])
                text_parts.extend(voice_blocks)
                for file_ref in file_refs:
                    text_parts.append(f"[attached: {file_ref['id']}]")
                post["message"] = " ".join(text_parts)

            # Check for text commands (!command syntax)
            message = post.get("message", "")
            if self._registry:
                parsed = parse_text_command(message)
                if parsed:
                    command, args = parsed
                    thread_id = post.get("root_id") or post.get("id")
                    result = await dispatch_text_command(
                        command, args, self._registry, thread_id
                    )
                    await self.post(result.message, thread_id=thread_id)
                    return

            # If reply in a thread, check for pending approval/TUI resolution
            thread_id = post.get("root_id") or None
            is_reply_in_thread = bool(post.get("root_id"))

            if is_reply_in_thread and self._approval_router:
                resolved = await self._approval_router.resolve_by_text(
                    thread_id, message, author_is_bot=False
                )
                if resolved:
                    return
                resolved = await self._approval_router.resolve_tui_by_text(
                    thread_id, message, author_is_bot=False
                )
                if resolved:
                    return

            # Normal message handling
            if self._on_message:
                await self._on_message(post)

        elif event == "reaction_added":
            reaction = data.get("reaction", {})
            if reaction.get("user_id") == self._bot_user_id:
                return
            if self._on_reaction:
                # Convert MM emoji name to Unicode for the approval router
                emoji_unicode = _mattermost_to_emoji(reaction.get("emoji_name", ""))
                normalized_reaction = {
                    "post_id": reaction.get("post_id"),
                    "user_id": reaction.get("user_id"),
                    "emoji": emoji_unicode,
                }
                await self._on_reaction(normalized_reaction)

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


_MATTERMOST_TO_UNICODE: dict[str, str] = {
    "white_check_mark": "✅",
    "x": "❌",
    "one": "1️⃣",
    "two": "2️⃣",
    "three": "3️⃣",
    "four": "4️⃣",
    "thumbsup": "👍",
    "thumbsdown": "👎",
}


def _mattermost_to_emoji(mm_name: str) -> str:
    """Convert Mattermost emoji name to Unicode emoji for the approval router."""
    return _MATTERMOST_TO_UNICODE.get(mm_name, mm_name)


def _is_audio_mime_type(mime_type: str) -> bool:
    """Check if a MIME type represents audio."""
    return mime_type.startswith("audio/")


async def _process_post_files(
    post: dict[str, Any], api: MattermostAPI
) -> tuple[list[str], list[dict[str, Any]]]:
    """Process file_ids in a Mattermost post.

    Returns a tuple of:
    - List of voice memo blocks (strings to include in message)
    - List of file reference dicts for non-audio files
    """
    file_ids = post.get("file_ids", [])
    if not file_ids:
        return [], []

    voice_blocks: list[str] = []
    file_refs: list[dict[str, Any]] = []

    for file_id in file_ids:
        try:
            file_info = await api.get_file_info(file_id)
        except Exception as e:
            logger.warning("Failed to get file info for %s: %s", file_id, e)
            continue

        mime_type = file_info.get("mime_type", "")

        if _is_audio_mime_type(mime_type):
            # Download and transcribe audio
            try:
                audio_data = await api.download_file(file_id)
                # Save to temp file for transcription
                with tempfile.NamedTemporaryFile(
                    suffix=Path(file_info.get("name", f"{file_id}.bin")).suffix,
                    delete=False,
                ) as tmp:
                    tmp.write(audio_data)
                    tmp_path = Path(tmp.name)

                transcript_text = await voice.transcribe(tmp_path)
                if transcript_text:
                    voice_blocks.append(f"[voice memo] {transcript_text}")
                else:
                    voice_blocks.append(
                        "[voice memo received — transcription unavailable; "
                        f"raw file: {tmp_path}]"
                    )
            except Exception as e:
                logger.warning("Failed to transcribe audio file %s: %s", file_id, e)
                voice_blocks.append(
                    "[voice memo received — transcription unavailable; "
                    f"raw file: (download failed: {file_id})]"
                )
        else:
            # Non-audio file reference
            file_refs.append(file_info)

    return voice_blocks, file_refs

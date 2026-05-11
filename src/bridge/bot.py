"""Discord bot client wrapper with chunked send."""

import asyncio
import base64
import contextlib
import logging
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

import aiohttp
import discord


_T = TypeVar("_T")


# Discord occasionally serves transient 5xx during incidents. Retry the
# bracketed call a few times with exponential backoff before propagating.
_RETRY_DELAYS_SECS = (0.5, 1.5, 4.0)


async def _with_retry(label: str, factory: Callable[[], Awaitable[_T]]) -> _T:
    """Retry `factory()` on Discord 5xx / connection errors. Raises on the
    final attempt or on any non-retryable error."""
    last_exc: BaseException | None = None
    for attempt, delay in enumerate((0.0,) + _RETRY_DELAYS_SECS):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await factory()
        except (discord.DiscordServerError, aiohttp.ClientConnectionError) as e:
            last_exc = e
            logger.warning(
                "%s: transient discord error (attempt %d/%d): %s",
                label,
                attempt + 1,
                len(_RETRY_DELAYS_SECS) + 1,
                e,
            )
    assert last_exc is not None  # the loop above always raises or returns
    raise last_exc

logger = logging.getLogger(__name__)


# Discord enforces 2000 chars per message; leave headroom for an attribution
# header / future codes appended by the bot.
MAX_CHUNK = 1900


def _chunk(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Split text into <=limit-char chunks, breaking on newlines when possible."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:  # no good break point — hard split
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


# Per-image cap. Matches Discord's free-tier upload limit. Larger files are
# logged and skipped rather than crashing the agent call.
MAX_IMAGE_BYTES = 10 * 1024 * 1024


async def _extract_images(
    message: discord.Message,
) -> list[dict[str, str]]:
    """Pull image attachments off a Discord message as base64 blobs.

    Non-image attachments and oversized images are ignored (logged).
    Returned dicts are shaped for Agent.chat(..., images=...).
    """
    images: list[dict[str, str]] = []
    for att in message.attachments:
        content_type = (att.content_type or "").lower()
        if not content_type.startswith("image/"):
            continue
        if att.size and att.size > MAX_IMAGE_BYTES:
            logger.warning(
                "Skipping oversized image %s (%d bytes, max %d)",
                att.filename,
                att.size,
                MAX_IMAGE_BYTES,
            )
            continue
        try:
            data = await att.read()
        except Exception:
            logger.exception("Failed to read attachment %s", att.filename)
            continue
        images.append(
            {
                "media_type": content_type,
                "data": base64.b64encode(data).decode("ascii"),
            }
        )
    return images


class BotNotReady(RuntimeError):
    """Raised when attempting operations on a bot that hasn't finished handshake."""

    pass


class Bot:
    """Wraps a discord.py Client with the operations the bridge needs.

    State machine: instances start `not connected`. After `await start()` the
    Gateway handshake runs in the background; `is_ready` flips to True when
    the bot has fully connected and resolved the configured channel.
    """

    def __init__(
        self,
        token: str,
        channel_id: int,
        *,
        on_message: Callable[[discord.Message], Awaitable[None]] | None = None,
        on_reaction: Callable[[discord.RawReactionActionEvent], Awaitable[None]] | None = None,
    ) -> None:
        intents = discord.Intents.default()
        # Privileged intent — required for `on_message` payloads to carry
        # `content`, which the bridge reads to route Discord replies into
        # the corresponding zellij pane. Must also be enabled on the bot
        # user in the Discord Developer Portal.
        intents.message_content = True
        self._client = discord.Client(intents=intents)
        self._token = token
        self._channel_id = channel_id
        self._channel: discord.TextChannel | None = None
        self._ready = asyncio.Event()
        self._on_message_cb = on_message
        self._on_reaction_cb = on_reaction
        # discord.py registers event handlers by method name.
        self._client.event(self.on_ready)
        if on_message is not None:
            self._client.event(self.on_message)
        if on_reaction is not None:
            self._client.event(self.on_raw_reaction_add)

    @property
    def channel_id(self) -> int:
        return self._channel_id

    @property
    def client(self) -> discord.Client:
        """Underlying discord.py Client. Used by commands.py to attach a CommandTree."""
        return self._client

    @property
    def channel(self) -> discord.TextChannel | None:
        """Configured channel object (set after on_ready). Used to resolve guild for command sync."""
        return self._channel

    async def on_ready(self) -> None:
        """Called when the bot finishes the gateway handshake."""
        ch = self._client.get_channel(self._channel_id) or await self._client.fetch_channel(
            self._channel_id
        )
        if not isinstance(ch, discord.TextChannel):
            raise RuntimeError(
                f"Configured DISCORD_CHANNEL_ID={self._channel_id} "
                f"is not a TextChannel (got {type(ch).__name__})."
            )
        self._channel = ch
        self._ready.set()
        logger.info("Bot ready as %s, watching #%s", self._client.user, ch.name)

    async def on_message(self, msg: discord.Message) -> None:
        """Dispatch incoming messages to the registered callback.

        Filters out the bot's own messages (AC3.6).
        """
        # Always ignore our own messages
        if msg.author == self._client.user:
            return
        if self._on_message_cb is not None:
            await self._on_message_cb(msg)

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set() and not self._client.is_closed()

    async def start(self) -> None:
        """Schedules the gateway handshake. Returns once the task is running.
        Caller must `await close()` to shut down cleanly."""
        self._task = asyncio.create_task(self._client.start(self._token))

    async def close(self) -> None:
        await self._client.close()
        if hasattr(self, "_task"):
            with contextlib.suppress(Exception):
                await self._task

    async def post(self, message: str, *, thread_id: str | None = None) -> list[str]:
        """Post `message` to the configured channel (or thread within it).

        Chunks per `_chunk()`. Returns the list of created message IDs (as strings).
        Raises `BotNotReady` if the bot isn't connected yet. Transient
        Discord 5xx / connection errors are retried with backoff before
        propagating.

        Partial-send semantics: each chunk is retried independently. If
        chunk N exhausts its retry budget after chunks 1..N-1 already
        landed, this call raises but the earlier chunks remain visible
        in the thread. The caller has no way to know exactly how many
        landed beyond inspecting the (incomplete) returned list before
        the raise — so treat any exception from this method as
        "message may have been partially delivered".
        """
        if not self.is_ready or self._channel is None:
            raise BotNotReady("bot not connected to Discord")
        target: discord.abc.Messageable = self._channel
        discord_thread_id: int | None = None
        if thread_id is not None:
            discord_thread_id = int(thread_id)
            target = await _with_retry(
                f"fetch_channel({discord_thread_id})",
                lambda: self._client.fetch_channel(discord_thread_id),
            )
        ids: list[str] = []
        for chunk in _chunk(message):
            msg = await _with_retry(
                f"send(thread={discord_thread_id})",
                lambda c=chunk: target.send(c),
            )
            ids.append(str(msg.id))
        return ids

    async def post_with_attachments(
        self,
        file_paths: list[Path],
        *,
        thread_id: str | None = None,
        text: str | None = None,
    ) -> list[str]:
        """Send up to 10 file attachments (Discord's per-message cap) plus an
        optional text body. If `text` exceeds Discord's char limit it's chunked
        across follow-up text-only messages, with the attachments going on the
        first send. Returns created message IDs in order.
        """
        if not self.is_ready or self._channel is None:
            raise BotNotReady("bot not connected to Discord")
        if not file_paths:
            raise ValueError("file_paths must not be empty")
        target: discord.abc.Messageable = self._channel
        discord_thread_id: int | None = None
        if thread_id is not None:
            discord_thread_id = int(thread_id)
            target = await _with_retry(
                f"fetch_channel({discord_thread_id})",
                lambda: self._client.fetch_channel(discord_thread_id),
            )

        capped = file_paths[:10]
        chunks = list(_chunk(text)) if text else [None]
        first_chunk = chunks[0]
        # discord.File is consumed when sent — open fresh handles for retries.
        send_first = lambda: target.send(  # noqa: E731
            content=first_chunk,
            files=[discord.File(str(p)) for p in capped],
        )
        ids: list[str] = []
        msg = await _with_retry(f"send(thread={discord_thread_id}, with files)", send_first)
        ids.append(str(msg.id))
        for follow in chunks[1:]:
            msg = await _with_retry(
                f"send(thread={discord_thread_id}, follow-up)",
                lambda c=follow: target.send(c),
            )
            ids.append(str(msg.id))
        return ids

    async def create_thread(self, name: str) -> str:
        """Create a public thread off the configured channel. Returns its ID (as string)."""
        if not self.is_ready or self._channel is None:
            raise BotNotReady("bot not connected to Discord")
        thread = await self._channel.create_thread(
            name=name,
            type=discord.ChannelType.public_thread,
            auto_archive_duration=10080,  # 7 days — max for non-boosted servers
        )
        return str(thread.id)

    async def thread_alive(self, thread_id: str) -> bool:
        """Probe whether a thread still exists (returns False on 404)."""
        if not self.is_ready:
            raise BotNotReady("bot not connected to Discord")
        try:
            discord_thread_id = int(thread_id)
            await self._client.fetch_channel(discord_thread_id)
            return True
        except discord.NotFound:
            return False

    async def fetch_messageable(self, thread_id: str) -> discord.abc.Messageable:
        """Resolve a thread id to a Messageable (caches via _client.fetch_channel)."""
        if not self.is_ready:
            raise BotNotReady("bot not connected to Discord")
        discord_thread_id = int(thread_id)
        return await self._client.fetch_channel(discord_thread_id)

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
    ) -> None:
        """Edit an existing message. Pass either `content` or `embed` (or
        both, but typically one). Used by the live-updating subagent blocks.
        Discord limits each field; caller must truncate.
        """
        if not self.is_ready or self._channel is None:
            raise BotNotReady("bot not connected to Discord")
        discord_thread_id = int(thread_id)
        discord_message_id = int(message_id)
        target = await _with_retry(
            f"fetch_channel({discord_thread_id})",
            lambda: self._client.fetch_channel(discord_thread_id),
        )
        msg = await _with_retry(
            f"fetch_message({discord_message_id})",
            lambda: target.fetch_message(discord_message_id),
        )
        await _with_retry(
            f"edit({discord_message_id})",
            lambda: msg.edit(content=content, embed=embed),
        )

    async def post_embed(
        self, embed: discord.Embed, *, thread_id: str | None = None
    ) -> str:
        """Send a single embed to the channel/thread; return the message id (as string)."""
        if not self.is_ready or self._channel is None:
            raise BotNotReady("bot not connected to Discord")
        target: discord.abc.Messageable = self._channel
        discord_thread_id: int | None = None
        if thread_id is not None:
            discord_thread_id = int(thread_id)
            target = await _with_retry(
                f"fetch_channel({discord_thread_id})",
                lambda: self._client.fetch_channel(discord_thread_id),
            )
        msg = await _with_retry(
            f"send-embed(thread={discord_thread_id})",
            lambda: target.send(embed=embed),
        )
        return str(msg.id)

    async def rename_thread(self, thread_id: str, name: str) -> None:
        """Rename a Discord thread. Discord enforces 1–100 chars; caller should sanitize."""
        if not self.is_ready:
            raise BotNotReady("bot not connected to Discord")
        discord_thread_id = int(thread_id)
        thread = await self._client.fetch_channel(discord_thread_id)
        await thread.edit(name=name)

    async def archive_thread(self, thread_id: str) -> None:
        """Archive a Discord thread by ID.

        Fetches the thread and marks it as archived. Silently ignores 404 (thread already gone).
        Raises BotNotReady if the bot isn't connected.
        """
        if not self.is_ready:
            raise BotNotReady("bot not connected to Discord")
        try:
            discord_thread_id = int(thread_id)
            thread = await self._client.fetch_channel(discord_thread_id)
            if isinstance(thread, discord.Thread):
                await thread.edit(archived=True)
        except discord.NotFound:
            pass  # Thread already gone, which is fine

    async def add_reactions(self, message_id: str, thread_id: str, emoji: list[str]) -> None:
        """Add emoji reactions to a message.

        Args:
            message_id: The Discord message ID (as string)
            thread_id: The Discord thread ID (or channel ID) (as string)
            emoji: List of emoji strings to add (e.g., ["✅", "❌"])

        Raises BotNotReady if the bot isn't connected.
        """
        if not self.is_ready:
            raise BotNotReady("bot not connected to Discord")
        discord_thread_id = int(thread_id)
        discord_message_id = int(message_id)
        channel = await self._client.fetch_channel(discord_thread_id)
        msg = await channel.fetch_message(discord_message_id)
        for e in emoji:
            await msg.add_reaction(e)

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Called by discord.py on every reaction-add. Bridges to the approval router callback."""
        if self._on_reaction_cb is not None:
            await self._on_reaction_cb(payload)

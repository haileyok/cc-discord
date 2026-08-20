"""Slack adapter used by the bridge.

The adapter keeps the small Bot-shaped surface consumed by the existing task
renderer, but all transport and event handling is Slack-native.  Web API calls
are made with the official ``slack-sdk`` AsyncWebClient and Socket Mode uses
its aiohttp client behind :class:`AiohttpSocketMode`.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import re
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, TypeVar
from urllib.parse import urlparse

import aiohttp
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from bridge.redaction import safe_error, safe_log

try:  # slack-sdk exposes this only when Socket Mode extras are installed.
    from slack_sdk.socket_mode.aiohttp import SocketModeClient as _SdkSocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse
except ImportError:  # pragma: no cover - import-time compatibility
    _SdkSocketModeClient = None  # type: ignore[assignment,misc]
    SocketModeRequest = None  # type: ignore[assignment,misc]
    SocketModeResponse = None  # type: ignore[assignment,misc]

_T = TypeVar("_T")
logger = logging.getLogger(__name__)

MAX_CHUNK = 1900
MAX_PRIVATE_DOWNLOAD_BYTES = int(os.environ.get("BRIDGE_MAX_PRIVATE_DOWNLOAD_BYTES", str(50 * 1024 * 1024)))
MAX_UPLOAD_BYTES = int(os.environ.get("BRIDGE_MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024)))
MAX_UPLOAD_AGGREGATE_BYTES = int(os.environ.get("BRIDGE_MAX_UPLOAD_AGGREGATE_BYTES", str(2 * 1024 * 1024 * 1024)))
_UPLOAD_CHUNK_BYTES = 64 * 1024
_TRUSTED_SLACK_HOSTS = frozenset({"slack.com", "files.slack.com", "api.slack.com"})
_RETRY_DELAYS_SECS = (0.5, 1.5, 4.0)
_OUTBOUND_MIN_INTERVAL_SECS = 0.05


class SlackAdapterError(RuntimeError):
    """A malformed or unsuccessful Slack API response."""

    def __init__(self, message: str, *, status: int | None = None,
                 retry_after: float | None = None, error: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.error = error


class BotNotReady(RuntimeError):
    """Raised when an operation is attempted before Slack validation."""


class BotMissingPermission(RuntimeError):
    """Raised when Slack denies a channel-management operation."""


class SocketModeUnavailable(RuntimeError):
    """Raised when Socket Mode was requested but slack-sdk is unavailable."""


def _response_value(response: Any, key: str, default: Any = None) -> Any:
    if isinstance(response, Mapping):
        return response.get(key, default)
    data = getattr(response, "data", None)
    if isinstance(data, Mapping):
        return data.get(key, default)
    try:
        return response[key]
    except (KeyError, TypeError, AttributeError):
        return getattr(response, key, default)


def _response_status(response: Any) -> int | None:
    value = getattr(response, "status_code", None)
    if value is None:
        value = _response_value(response, "status_code")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _retry_after(response: Any) -> float | None:
    headers = getattr(response, "headers", None) or _response_value(response, "headers", {})
    value = None
    if isinstance(headers, Mapping):
        value = headers.get("Retry-After") or headers.get("retry-after")
    if value is None:
        value = _response_value(response, "retry_after")
    try:
        return max(0.0, float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _exception_response(exc: BaseException) -> Any:
    return getattr(exc, "response", None)


def _is_retryable_exception(exc: BaseException) -> bool:
    if isinstance(exc, SlackAdapterError):
        return exc.status == 429 or (exc.status is not None and 500 <= exc.status < 600) or exc.error in {
            "ratelimited", "server_error", "service_unavailable", "temporarily_unavailable",
        }
    if isinstance(exc, aiohttp.ClientResponseError):
        return exc.status == 429 or 500 <= exc.status < 600
    if isinstance(exc, (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError,
                        asyncio.TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, SlackApiError):
        response = _exception_response(exc)
        status = _response_status(response)
        error = str(_response_value(response, "error", ""))
        return status == 429 or (status is not None and 500 <= status < 600) or error in {
            "ratelimited", "server_error", "service_unavailable", "temporarily_unavailable",
        }
    return False


def _exception_retry_after(exc: BaseException) -> float | None:
    if isinstance(exc, SlackAdapterError):
        return exc.retry_after
    return _retry_after(_exception_response(exc))


async def _with_retry(
    label: str,
    factory: Callable[[], Awaitable[_T]],
    *,
    sleeper: Callable[[float], Awaitable[Any]] | None = None,
    delays: tuple[float, ...] = _RETRY_DELAYS_SECS,
) -> _T:
    """Run a Slack operation with a bounded, narrowly-scoped retry policy.

    429 responses honor ``Retry-After``.  Transport errors and HTTP 5xx use
    bounded exponential delays.  Slack's application errors (``invalid_auth``,
    ``channel_not_found``, etc.) are never retried.
    """
    sleep = sleeper or asyncio.sleep
    last: BaseException | None = None
    for attempt in range(len(delays) + 1):
        try:
            return await factory()
        except BaseException as exc:
            if not _is_retryable_exception(exc) or attempt >= len(delays):
                raise
            last = exc
            wait = _exception_retry_after(exc)
            if wait is None:
                wait = delays[attempt]
            logger.warning("%s transient Slack failure (attempt %d/%d): %s",
                           safe_log(label), attempt + 1, len(delays) + 1,
                           safe_error(exc, "transient Slack failure"))
            slept = sleep(wait)
            if inspect.isawaitable(slept):
                await slept
    assert last is not None
    raise last


def _ensure_ok(response: Any, label: str) -> Any:
    ok = _response_value(response, "ok", True)
    if ok is False:
        error = str(_response_value(response, "error", "unknown_error"))
        status = _response_status(response)
        retry_after = _retry_after(response)
        raise SlackAdapterError(f"{label} failed: {error}", status=status,
                                retry_after=retry_after, error=error)
    return response


def _chunk(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Split text into bounded chunks, preferring sensible newline breaks."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def sanitize_channel_name(name: str) -> str:
    """Return a Slack-safe, deterministic channel name (1..80 chars)."""
    value = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.lower())
    value = re.sub(r"[-_]{2,}", "-", value).strip("-_")
    return (value[:80].rstrip("-_") or "task")


def unique_channel_name(name: str, existing: set[str] | list[str] = ()) -> str:
    """Sanitize ``name`` and add a stable numeric suffix on collisions."""
    used = {str(item).lower() for item in existing}
    base = sanitize_channel_name(name)
    if base not in used:
        return base
    for index in range(2, 10000):
        suffix = f"-{index}"
        candidate = f"{base[:80-len(suffix)]}{suffix}"
        if candidate not in used:
            return candidate
    raise ValueError("unable to allocate a unique Slack channel name")


@dataclass(frozen=True)
class SlackUser:
    id: str
    bot: bool = False
    name: str = ""


@dataclass(frozen=True)
class SlackAppIdentity:
    """Canonical external Slack app identity.

    Slack events identify an installed app with a stable ``bot_id`` (B...),
    while ``conversations.invite`` and ``conversations.kick`` require the
    app's workspace user id (U...).
    """

    bot_id: str
    user_id: str


@dataclass(frozen=True)
class SlackChannel:
    id: str
    name: str = ""
    is_private: bool = True
    is_member: bool = False


@dataclass
class SlackAttachment:
    """Normalized Slack file attachment consumed by ``TaskRegistry``."""
    filename: str
    url: str
    content_type: str = "application/octet-stream"
    size: int = 0
    _reader: Callable[[], Awaitable[bytes]] | None = field(default=None, repr=False)

    @property
    def mimetype(self) -> str:
        return self.content_type

    @property
    def url_private_download(self) -> str:
        return self.url

    async def read(self) -> bytes:
        if self._reader is None:
            raise RuntimeError("Slack attachment has no authenticated reader")
        return await self._reader()


@dataclass
class SlackActor:
    actor_id: str
    is_app: bool = False
    display_name: str | None = None
    kind: str = "human"

    def __post_init__(self) -> None:
        self.actor_id = str(self.actor_id).strip()
        self.kind = "app" if self.is_app else (str(self.kind or "human").strip().lower() or "human")
        if self.kind not in {"human", "app"}:
            raise ValueError("Slack actor kind must be human or app")
        self.is_app = self.kind == "app"
        if self.display_name is not None:
            self.display_name = str(self.display_name).strip() or None

    @property
    def id(self) -> str:
        return self.actor_id


@dataclass
class SlackMessage:
    """Provider-neutral inbound event consumed by the task registry."""
    team_id: str
    channel_id: str
    root_ts: str
    actor: SlackActor
    text: str = ""
    event_id: str | None = None
    message_ts: str | None = None
    files: tuple[SlackAttachment, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)


class EnvelopeAcknowledger(Protocol):
    async def ack(self, envelope_id: str) -> None: ...


class AiohttpSocketMode:
    """Small injectable abstraction over slack-sdk's aiohttp Socket Mode client."""

    def __init__(self, app_token: str, web_client: AsyncWebClient,
                 handler: Callable[[Any], Awaitable[None]], *, client: Any = None) -> None:
        self.app_token = app_token
        self.web_client = web_client
        self._handler = handler
        self._client = client
        self.connected = False

    def _build(self) -> Any:
        """Build exactly one SDK client and retain it for its whole lifetime."""
        if self._client is not None:
            return self._client
        if _SdkSocketModeClient is None:
            raise SocketModeUnavailable("install slack-sdk with Socket Mode support")
        self._client = _SdkSocketModeClient(app_token=self.app_token, web_client=self.web_client)
        return self._client

    async def connect(self) -> None:
        client = self._build()
        listeners = getattr(client, "socket_mode_request_listeners", None)
        if listeners is not None and self._listener not in listeners:
            listeners.append(self._listener)
        try:
            result = client.connect()
            if inspect.isawaitable(result):
                await result
        except BaseException:
            # A failed startup must not leave a websocket/session behind.
            with contextlib.suppress(Exception):
                await self.close()
            raise
        self.connected = True

    async def close(self) -> None:
        # Ack/close always use the same retained SDK object.  Do not call
        # _build() here: close-before-connect must not create a new client.
        # Retain the same object after close so a later ack/close cannot create
        # a second SDK client for this adapter instance.
        client = self._client
        if client is not None:
            result = getattr(client, "close", None) or getattr(client, "disconnect", None)
            if result is not None:
                value = result()
                if inspect.isawaitable(value):
                    await value
        self.connected = False

    async def ack(self, envelope_id: str) -> None:
        client = self._build()
        sender = getattr(client, "send_socket_mode_response", None)
        if sender is None:
            sender = getattr(client, "ack", None)
        if sender is None:
            raise SocketModeUnavailable("Socket Mode client cannot acknowledge envelopes")
        payload: Any = {"envelope_id": envelope_id}
        if SocketModeResponse is not None and getattr(sender, "__name__", "") == "send_socket_mode_response":
            payload = SocketModeResponse(envelope_id=envelope_id)
        value = sender(payload)
        if inspect.isawaitable(value):
            await value

    async def _listener(self, *args: Any) -> None:
        # slack-sdk calls listeners as (client, SocketModeRequest).  Keeping
        # the request object intact lets callers inspect the real SDK type.
        request = args[-1] if args else None
        await self._handler(request)


# Friendly aliases for callers/tests that prefer the longer name.
AiohttpSocketModeClient = AiohttpSocketMode


class Bot:
    """Slack Web API + Socket Mode adapter."""

    def __init__(
        self,
        token: str,
        *,
        team_id: str | None = None,
        owner_user_id: str | None = None,
        home_channel_id: str | None = None,
        app_token: str | None = None,
        on_message: Callable[[SlackMessage], Awaitable[None]] | None = None,
        on_reaction: Callable[[Mapping[str, Any]], Awaitable[None]] | None = None,
        on_dispatch: Callable[[Mapping[str, Any]], Awaitable[Any]] | None = None,
        client: AsyncWebClient | Any | None = None,
        socket_client: Any | None = None,
        http_session: aiohttp.ClientSession | Any | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
    ) -> None:
        self._token = token
        self._team_id = team_id
        self._owner_user_id = owner_user_id
        self._channel_id = home_channel_id
        self._app_token = app_token
        self._client = client or AsyncWebClient(token=token)
        self._http_session = http_session
        self._sleep = sleep
        self._socket = socket_client
        self._on_message_cb = on_message
        self._on_reaction_cb = on_reaction
        self._on_dispatch_cb = on_dispatch
        self._ready = asyncio.Event()
        self._closed = False
        self._socket_connected = False
        self._last_error: str | None = None
        self._bot_id: str | None = None
        self._bot_user_id: str | None = None
        self._app_identity_cache: dict[str, SlackAppIdentity] = {}
        self._channel: SlackChannel | None = None
        self._used_channel_names: set[str] = set()
        self._owned_channel_ids: set[str] = set()
        self._outbound_locks: dict[str, asyncio.Lock] = {}
        self._outbound_last: dict[str, float] = {}
        self._socket_handler = self.handle_socket_envelope

    @property
    def channel_id(self) -> str | None:
        return self._channel_id

    @property
    def home_channel_id(self) -> str | None:
        return self._channel_id

    @property
    def team_id(self) -> str | None:
        return self._team_id

    @property
    def owner_user_id(self) -> str | None:
        return self._owner_user_id

    @property
    def bot_user_id(self) -> str | None:
        return self._bot_user_id

    @property
    def bot_id(self) -> str | None:
        return self._bot_id

    async def resolve_app_identity(self, bot_id: str) -> SlackAppIdentity:
        """Resolve an external app's stable bot id to its Slack user id."""
        key = str(bot_id).strip()
        if not key:
            raise ValueError("bot_id must not be empty")
        cached = self._app_identity_cache.get(key)
        if cached is not None:
            return cached
        response = await self._api("bots_info", bot=key)
        bot = _response_value(response, "bot", {}) or {}
        if not isinstance(bot, Mapping):
            raise SlackAdapterError("bots.info returned malformed bot data")
        canonical_bot_id = str(bot.get("id") or key).strip()
        user_id = str(bot.get("user_id") or bot.get("app_user_id") or "").strip()
        if canonical_bot_id != key or not user_id:
            raise SlackAdapterError("bots.info response omitted canonical bot/user identity")
        identity = SlackAppIdentity(canonical_bot_id, user_id)
        self._app_identity_cache[key] = identity
        return identity

    @property
    def client(self) -> Any:
        return self._client

    @property
    def channel(self) -> SlackChannel | None:
        return self._channel

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set() and not self._closed

    @property
    def socket_mode_connected(self) -> bool:
        return self._socket_connected

    @property
    def health_fields(self) -> dict[str, Any]:
        return {
            "bot_connected": self.is_ready,
            "slack_connected": self.is_ready,
            "socket_mode_connected": self._socket_connected,
            "team_id": self._team_id,
            "owner_user_id": self._owner_user_id,
            "home_channel_id": self._channel_id,
            "channel_id": self._channel_id,
            "bot_user_id": self._bot_user_id,
            "last_error": self._last_error,
        }

    def health(self) -> dict[str, Any]:
        return dict(self.health_fields)

    async def _throttle_outbound(self, channel_id: str | None) -> None:
        if not channel_id:
            return
        lock = self._outbound_locks.setdefault(str(channel_id), asyncio.Lock())
        async with lock:
            now = asyncio.get_running_loop().time()
            wait = _OUTBOUND_MIN_INTERVAL_SECS - (now - self._outbound_last.get(str(channel_id), 0.0))
            if wait > 0:
                await asyncio.sleep(wait)
            self._outbound_last[str(channel_id)] = asyncio.get_running_loop().time()

    async def _api(self, method: str, **kwargs: Any) -> Any:
        if method in {"chat_postMessage", "chat_update", "reactions_add", "files_completeUploadExternal"}:
            await self._throttle_outbound(kwargs.get("channel") or kwargs.get("channel_id"))
        operation = getattr(self._client, method, None)
        if operation is None or not callable(operation):
            raise SlackAdapterError(f"Slack client lacks {method}()")

        async def call() -> Any:
            result = operation(**kwargs)
            if inspect.isawaitable(result):
                result = await result
            return _ensure_ok(result, method)

        return await _with_retry(method, call, sleeper=self._sleep)

    async def _validate_startup(self) -> None:
        auth = await self._api("auth_test")
        auth_team = _response_value(auth, "team_id")
        bot_user_id = _response_value(auth, "user_id") or _response_value(auth, "bot_user_id")
        bot_id = _response_value(auth, "bot_id") or _response_value(auth, "app_id")
        if not auth_team or not bot_user_id:
            raise SlackAdapterError("auth.test response omitted team_id or bot user id")
        if self._team_id and auth_team != self._team_id:
            raise SlackAdapterError(f"Slack team mismatch: expected {self._team_id}, got {auth_team}")
        self._team_id = str(auth_team)
        self._bot_user_id = str(bot_user_id)
        self._bot_id = str(bot_id) if bot_id else None

        if not self._owner_user_id:
            raise SlackAdapterError("owner_user_id is required for Slack startup")
        owner = await self._api("users_info", user=self._owner_user_id)
        user = _response_value(owner, "user", {}) or {}
        actual_owner = user.get("id") if isinstance(user, Mapping) else None
        if str(actual_owner or "") != str(self._owner_user_id):
            raise SlackAdapterError("configured Slack owner user does not match users.info")
        if bool(user.get("is_bot", False)) or bool(user.get("is_app_user", False)):
            raise SlackAdapterError("configured Slack owner must be a human user")

        if not self._channel_id:
            raise SlackAdapterError("home_channel_id is required for Slack startup")
        info = await self._api("conversations_info", channel=self._channel_id)
        channel = _response_value(info, "channel", {}) or {}
        if not isinstance(channel, Mapping):
            raise SlackAdapterError("conversations.info returned malformed channel data")
        actual_channel = str(channel.get("id", self._channel_id))
        if actual_channel != self._channel_id:
            raise SlackAdapterError("configured home channel does not match conversations.info")
        if not bool(channel.get("is_private", False)):
            raise SlackAdapterError("configured Slack home channel must be private")
        if not bool(channel.get("is_member", False)):
            raise SlackAdapterError("Slack bot is not a member of the configured home channel")
        self._channel = SlackChannel(
            id=self._channel_id,
            name=str(channel.get("name", "")),
            is_private=True,
            is_member=True,
        )

    async def start(self) -> None:
        """Validate identity/channel, then connect Socket Mode."""
        if self.is_ready:
            return
        self._closed = False
        try:
            await self._validate_startup()
            if self._socket is None and self._app_token:
                self._socket = AiohttpSocketMode(self._app_token, self._client, self._socket_handler)
            if self._socket is not None:
                register = getattr(self._socket, "register_handler", None)
                if register is not None:
                    register(self._socket_handler)
                await self._socket.connect()
                self._socket_connected = True
            self._ready.set()
            logger.info("Slack adapter ready for team=%s channel=%s", self._team_id, self._channel_id)
        except Exception as exc:
            self._last_error = safe_error(exc, "Slack startup validation failed")
            self._socket_connected = False
            if self._socket is not None:
                with contextlib.suppress(Exception):
                    result = self._socket.close()
                    if inspect.isawaitable(result):
                        await result
            self._ready.clear()
            raise

    async def close(self) -> None:
        self._closed = True
        self._ready.clear()
        if self._socket is not None:
            with contextlib.suppress(Exception):
                result = self._socket.close()
                if inspect.isawaitable(result):
                    await result
        self._socket_connected = False
        if self._http_session is not None and getattr(self._http_session, "closed", False) is False:
            # The injected session belongs to the caller; do not close it.
            pass

    def _require_ready(self) -> None:
        if not self.is_ready or self._channel_id is None:
            raise BotNotReady("Slack adapter is not ready")

    def _channel_for(self, channel_id: str | None) -> str:
        channel = channel_id or self._channel_id
        if channel is None:
            raise BotNotReady("Slack home channel is not configured")
        return channel

    @staticmethod
    def _message_id(response: Any) -> str:
        value = _response_value(response, "ts") or _response_value(response, "message_ts")
        if value is None:
            message = _response_value(response, "message", {})
            value = message.get("ts") if isinstance(message, Mapping) else None
        if not value:
            raise SlackAdapterError("Slack response omitted message timestamp")
        return str(value)

    @staticmethod
    def _blocks_fallback(blocks: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            text = block.get("text")
            if isinstance(text, Mapping) and text.get("text"):
                parts.append(str(text["text"]))
            for element in block.get("elements") or []:
                if isinstance(element, Mapping) and element.get("text"):
                    parts.append(str(element["text"]))
        return "\n".join(parts).strip() or "Slack message"

    async def post(self, message: str, *, channel_id: str | None = None,
                   root_ts: str | None = None,
                   blocks: list[dict[str, Any]] | None = None,
                   fallback_text: str | None = None) -> list[str]:
        """Post root messages or replies; ``fallback_text`` is required for blocks."""
        self._require_ready()
        ids: list[str] = []
        chunks = _chunk(str(message))
        for index, chunk in enumerate(chunks):
            kwargs: dict[str, Any] = {"channel": self._channel_for(channel_id), "text": chunk}
            if root_ts is not None:
                kwargs["thread_ts"] = str(root_ts)
            if blocks is not None and index == 0:
                kwargs["blocks"] = blocks
                kwargs["text"] = fallback_text or self._blocks_fallback(blocks) or chunk or "Slack message"
            response = await self._api("chat_postMessage", **kwargs)
            ids.append(self._message_id(response))
        return ids

    async def post_blocks(self, blocks: list[dict[str, Any]], fallback_text: str | None = None,
                          *, channel_id: str | None = None, root_ts: str | None = None) -> list[str]:
        fallback = fallback_text or self._blocks_fallback(blocks)
        return await self.post(fallback, channel_id=channel_id, root_ts=root_ts,
                               blocks=blocks, fallback_text=fallback)

    async def post_with_attachments(self, file_paths: list[str | Path],
                                    *, channel_id: str | None = None,
                                    root_ts: str | None = None,
                                    text: str | None = None) -> list[str]:
        self._require_ready()
        if not file_paths:
            raise ValueError("file_paths must not be empty")
        selected = list(file_paths[:10])
        aggregate = 0
        for path in selected:
            try:
                size = Path(path).stat().st_size
            except OSError as exc:
                raise ValueError("an attachment cannot be accessed") from exc
            aggregate += size
            if aggregate > MAX_UPLOAD_AGGREGATE_BYTES:
                raise ValueError("attachments exceed the configured aggregate upload limit")
        ids: list[str] = []
        for index, path in enumerate(selected):
            result = await self.upload_file(path, channel_id=channel_id or self._channel_id,
                                            root_ts=root_ts,
                                            initial_comment=text if index == 0 else None)
            file_id = _response_value(result, "file_id")
            message_ts = _response_value(result, "ts") or _response_value(result, "message_ts")
            if message_ts:
                ids.append(str(message_ts))
            elif file_id:
                ids.append(str(file_id))
        if text and not ids:
            ids.extend(await self.post(text, channel_id=channel_id, root_ts=root_ts))
        return ids

    async def upload_file(self, path: str | Path, *, channel_id: str | None = None,
                          root_ts: str | None = None, title: str | None = None,
                          initial_comment: str | None = None) -> Any:
        self._require_ready()
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(file_path)
        try:
            file_stat = file_path.stat()
        except OSError as exc:
            raise ValueError("file cannot be accessed") from exc
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("only regular files may be uploaded")
        size = file_stat.st_size
        if size > MAX_UPLOAD_BYTES:
            raise ValueError("file exceeds the configured upload limit")
        name = title or file_path.name
        pre = await self._api("files_getUploadURLExternal", filename=name, length=size)
        upload_url = _response_value(pre, "upload_url")
        file_id = _response_value(pre, "file_id")
        if not upload_url or not file_id:
            raise SlackAdapterError("Slack upload URL response is malformed")
        session = self._http_session or aiohttp.ClientSession()
        owns_session = self._http_session is None
        fd = None
        try:
            fd = os.open(file_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            opened_stat = os.fstat(fd)
            if not stat.S_ISREG(opened_stat.st_mode) or opened_stat.st_size != size:
                raise ValueError("file changed while preparing upload")
            file_obj = os.fdopen(fd, "rb", closefd=True)
            fd = None
            async def body() -> Any:
                while True:
                    chunk = await asyncio.to_thread(file_obj.read, _UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    yield chunk
            request = session.post(str(upload_url), data=body(), allow_redirects=False,
                                   headers={"Content-Type": "application/octet-stream"})
            response = await request if inspect.isawaitable(request) else request
            if hasattr(response, "__aenter__"):
                async with response as entered:
                    status = getattr(entered, "status", 200)
                    if status < 200 or status >= 300:
                        raise SlackAdapterError(f"Slack upload failed with HTTP {status}", status=status)
            else:
                status = getattr(response, "status", 200)
                if status < 200 or status >= 300:
                    raise SlackAdapterError(f"Slack upload failed with HTTP {status}", status=status)
        finally:
            # ``fdopen`` owns the descriptor after construction.  Null the raw
            # fd before cleanup so an upload error cannot close it twice.
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
                fd = None
            with contextlib.suppress(Exception):
                if 'file_obj' in locals():
                    file_obj.close()
            if owns_session:
                await session.close()
        kwargs: dict[str, Any] = {"files": [{"id": file_id, "title": name}]}
        destination = channel_id or self._channel_id
        if destination:
            kwargs["channel_id"] = destination
        if root_ts is not None:
            kwargs["thread_ts"] = str(root_ts)
        if initial_comment:
            kwargs["initial_comment"] = initial_comment
        return await self._api("files_completeUploadExternal", **kwargs)

    async def download_private_file(self, url: str, *, max_bytes: int = MAX_PRIVATE_DOWNLOAD_BYTES,
                                    destination: str | Path | None = None) -> bytes | Path:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or hostname not in _TRUSTED_SLACK_HOSTS and not hostname.endswith(".slack.com"):
            raise ValueError("private Slack downloads require an https Slack URL")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        session = self._http_session or aiohttp.ClientSession()
        owns_session = self._http_session is None
        headers = {"Authorization": f"Bearer {self._token}"}
        output = Path(destination) if destination is not None else None
        partial = output.with_name(output.name + ".partial") if output is not None else None
        completed = False
        try:
            request = session.get(url, headers=headers, allow_redirects=False)
            response = await request if inspect.isawaitable(request) else request
            if hasattr(response, "__aenter__"):
                async with response as entered:
                    data = await self._read_bounded_response(entered, max_bytes, partial=partial)
            else:
                data = await self._read_bounded_response(response, max_bytes, partial=partial)
        except BaseException:
            if partial is not None:
                with contextlib.suppress(OSError):
                    partial.unlink()
            raise
        finally:
            if owns_session:
                await session.close()
        if output is None:
            return data
        assert partial is not None
        try:
            if data is not None:
                # Small injected/fake responses use the bounded bytes fallback.
                await asyncio.to_thread(self._atomic_write_bytes, output, data)
            else:
                os.replace(partial, output)
            completed = True
            return output
        finally:
            if not completed and partial.exists():
                with contextlib.suppress(OSError):
                    partial.unlink()

    async def download_file(self, url: str, destination: str | Path,
                            max_bytes: int = MAX_PRIVATE_DOWNLOAD_BYTES) -> Path:
        """Download a private Slack file to disk with auth and a hard byte cap."""
        result = await self.download_private_file(url, max_bytes=max_bytes,
                                                  destination=destination)
        assert isinstance(result, Path)
        return result

    async def _read_bounded_response(self, response: Any, max_bytes: int, *, partial: Path | None = None) -> bytes | None:
        status = getattr(response, "status", 200)
        if status < 200 or status >= 300:
            raise SlackAdapterError(f"Slack private download failed with HTTP {status}", status=status)
        length = getattr(response, "content_length", None)
        if length is not None and int(length) > max_bytes:
            raise ValueError("private Slack file exceeds configured size limit")
        if partial is None:
            content = getattr(response, "content", None)
            if content is not None and hasattr(content, "iter_chunked"):
                chunks: list[bytes] = []
                total = 0
                async for chunk in content.iter_chunked(_UPLOAD_CHUNK_BYTES):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("private Slack file exceeds configured size limit")
                    chunks.append(bytes(chunk))
                return b"".join(chunks)
            data = response.read()
            if inspect.isawaitable(data):
                data = await data
            if len(data) > max_bytes:
                raise ValueError("private Slack file exceeds configured size limit")
            return bytes(data)
        partial.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        total = 0
        try:
            with os.fdopen(fd, "wb", closefd=True) as output:
                fd = None
                content = getattr(response, "content", None)
                if content is not None and hasattr(content, "iter_chunked"):
                    async for chunk in content.iter_chunked(_UPLOAD_CHUNK_BYTES):
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError("private Slack file exceeds configured size limit")
                        output.write(bytes(chunk))
                else:
                    data = response.read()
                    if inspect.isawaitable(data):
                        data = await data
                    total = len(data)
                    if total > max_bytes:
                        raise ValueError("private Slack file exceeds configured size limit")
                    output.write(bytes(data))
                output.flush()
                os.fsync(output.fileno())
            return None
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise

    @staticmethod
    def _atomic_write_bytes(output: Path, data: bytes) -> None:
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        partial = output.with_name(output.name + ".partial")
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=True) as handle:
                fd = None
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(partial, output)
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
            with contextlib.suppress(OSError):
                partial.unlink()
            raise

    async def create_task_root(self, channel_id: str, text: str,
                               *, blocks: list[dict[str, Any]] | None = None) -> str:
        """Create and return the timestamp anchoring a normalized Slack task."""
        result = await self.post(text, channel_id=channel_id, blocks=blocks)
        if not result:
            raise SlackAdapterError("Slack root post returned no timestamp")
        return result[0]

    async def create_channel(self, name: str, *, private: bool = True,
                             invite_user_id: str | None = None) -> str:
        self._require_ready()
        safe = unique_channel_name(name, self._used_channel_names)
        response = await self._api("conversations_create", name=safe, is_private=private)
        channel = _response_value(response, "channel", {}) or {}
        channel_id = channel.get("id") if isinstance(channel, Mapping) else None
        if not channel_id:
            raise SlackAdapterError("conversations.create response omitted channel id")
        info = await self._api("conversations_info", channel=str(channel_id))
        verified = _response_value(info, "channel", {}) or {}
        if not isinstance(verified, Mapping) or str(verified.get("id") or "") != str(channel_id):
            raise SlackAdapterError("conversations.info returned a mismatched channel")
        if bool(verified.get("is_private", False)) is not bool(private):
            raise SlackAdapterError("created Slack channel privacy does not match request")
        team = verified.get("team_id") or verified.get("team")
        if not team or str(team) != str(self._team_id):
            raise SlackAdapterError("created Slack channel belongs to another team")
        if not bool(verified.get("is_member", False)):
            raise SlackAdapterError("Slack bot is not a member of created channel")
        self._used_channel_names.add(safe)
        self._owned_channel_ids.add(str(channel_id))
        invite = invite_user_id or (self._owner_user_id if invite_user_id is None else None)
        if invite and str(invite) != self._bot_user_id:
            await self.invite_channel(str(channel_id), str(invite))
        return str(channel_id)

    async def _verify_managed_private_channel(self, channel_id: str) -> Mapping[str, Any]:
        if not channel_id or str(channel_id) == str(self._channel_id):
            raise ValueError("refusing to manage the configured home channel")
        if str(channel_id) not in self._owned_channel_ids:
            raise ValueError("channel is not a verified bridge-owned channel")
        info = await self._api("conversations_info", channel=str(channel_id))
        channel = _response_value(info, "channel", {}) or {}
        if not isinstance(channel, Mapping) or str(channel.get("id") or "") != str(channel_id):
            raise ValueError("channel verification failed")
        if not bool(channel.get("is_private", False)):
            raise ValueError("channel must be private")
        team = channel.get("team_id") or channel.get("team")
        if str(team or self._team_id) != str(self._team_id):
            raise ValueError("channel belongs to another Slack team")
        if not bool(channel.get("is_member", False)):
            raise ValueError("Slack bot is not a member of channel")
        return channel

    async def invite_channel(self, channel_id: str, user_id: str) -> Any:
        if not channel_id or not user_id:
            raise ValueError("channel_id and user_id are required")
        await self._verify_managed_private_channel(channel_id)
        return await self._api("conversations_invite", channel=channel_id, users=user_id)

    async def invite_participants(self, channel_id: str, user_ids: list[str]) -> Any:
        """Invite humans and resolve external app B IDs to Slack U IDs."""
        users: list[str] = []
        for actor_id in dict.fromkeys(str(user) for user in user_ids if str(user)):
            if actor_id.startswith("B"):
                actor_id = (await self.resolve_app_identity(actor_id)).user_id
            users.append(actor_id)
        if not users:
            return None
        await self._verify_managed_private_channel(channel_id)
        return await self._api("conversations_invite", channel=channel_id,
                               users=",".join(users))

    async def create_private_channel(self, name: str, *, invite_user_id: str | None = None) -> str:
        return await self.create_channel(name, private=True, invite_user_id=invite_user_id)

    def remember_owned_channel(self, channel_id: str) -> None:
        """Restore a persisted bridge-owned channel capability after restart."""
        if channel_id and str(channel_id) != str(self._channel_id):
            self._owned_channel_ids.add(str(channel_id))

    async def archive_channel(self, channel_id: str, *, force: bool = False) -> None:
        self._require_ready()
        # ``force`` is retained only for source compatibility; it can never
        # bypass ownership/safety checks.
        if force:
            raise ValueError("force archive is not supported")
        if not channel_id or channel_id == self._channel_id:
            raise ValueError("refusing to archive the configured Slack home channel")
        if str(channel_id) not in self._owned_channel_ids:
            raise ValueError("refusing to archive an unverified bridge-owned channel")
        try:
            info = await self._api("conversations_info", channel=str(channel_id))
            channel = _response_value(info, "channel", {}) or {}
            if not isinstance(channel, Mapping) or str(channel.get("id") or "") != str(channel_id):
                raise ValueError("channel verification failed")
            if not bool(channel.get("is_private", False)) or not bool(channel.get("is_member", False)):
                raise ValueError("refusing to archive a non-private or non-member channel")
            team = channel.get("team_id") or channel.get("team")
            if not team or str(team) != str(self._team_id):
                raise ValueError("refusing to archive a channel from another team")
            await self._api("conversations_archive", channel=channel_id)
        except SlackApiError as exc:
            response = _exception_response(exc)
            if _response_value(response, "error") == "channel_not_found":
                return
            raise

    async def edit_message(self, channel_id: str, message_ts: str, *,
                           text: str | None = None,
                           blocks: list[dict[str, Any]] | None = None) -> None:
        self._require_ready()
        kwargs: dict[str, Any] = {"channel": self._channel_for(channel_id), "ts": str(message_ts),
                                  "text": text or "Slack message"}
        if blocks is not None:
            kwargs["blocks"] = blocks
        await self._api("chat_update", **kwargs)

    async def add_reaction(self, channel_id: str, message_id: str, emoji: str) -> None:
        self._require_ready()
        name = str(emoji).strip().strip(":")
        if name:
            await self._api("reactions_add", channel=self._channel_for(channel_id),
                            timestamp=str(message_id), name=name)

    async def remove_participants(self, channel_id: str, user_ids: list[str]) -> Any:
        """Remove humans and resolve external app B IDs to Slack U IDs."""
        users: list[str] = []
        for actor_id in dict.fromkeys(str(user) for user in user_ids if str(user)):
            if actor_id.startswith("B"):
                actor_id = (await self.resolve_app_identity(actor_id)).user_id
            users.append(actor_id)
        if not users:
            return None
        await self._verify_managed_private_channel(channel_id)
        result = None
        for user in users:
            result = await self._api("conversations_kick", channel=channel_id, user=user)
        return result

    @staticmethod
    def _socket_payload(envelope: Any) -> tuple[str, dict[str, Any]]:
        """Normalize dict fixtures and the real slack-sdk SocketModeRequest."""
        if SocketModeRequest is not None and isinstance(envelope, SocketModeRequest):
            envelope_id = str(envelope.envelope_id or "")
            raw = envelope.payload
        elif isinstance(envelope, Mapping):
            envelope_id = str(envelope.get("envelope_id") or "")
            raw = envelope.get("payload", envelope)
        else:
            # Be tolerant of SDK-compatible objects without importing a second
            # SDK version, while rejecting arbitrary object guesses.
            envelope_id = str(getattr(envelope, "envelope_id", "") or "")
            raw = getattr(envelope, "payload", None)
            if not envelope_id or raw is None:
                raise ValueError("malformed Socket Mode envelope")
        if isinstance(raw, str):
            import json
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("malformed Socket Mode envelope payload") from exc
        if not isinstance(raw, Mapping):
            raise ValueError("malformed Socket Mode envelope payload")
        return envelope_id, dict(raw)

    def _authenticated_team(self, payload: Mapping[str, Any], event: Mapping[str, Any]) -> str:
        team = payload.get("team_id") or payload.get("team")
        if isinstance(team, Mapping):
            team = team.get("id")
        if not team:
            team = event.get("team") or event.get("team_id")
        team_id = str(team or "")
        if not team_id or not self._team_id or team_id != self._team_id:
            raise SlackAdapterError("Slack ingress team is not authenticated", error="team_mismatch")
        return team_id

    def _normalize_socket_dispatch(self, envelope: Any) -> dict[str, Any] | None:
        envelope_id, payload = self._socket_payload(envelope)
        event = payload.get("event", payload)
        if not isinstance(event, Mapping):
            # Slash and interactive payloads are themselves mappings; other
            # envelope shapes are rejected rather than guessed at.
            raise ValueError("malformed Slack event fixture")
        event = dict(event)
        team_id = self._authenticated_team(payload, event)
        outer_type = str(payload.get("type") or "")
        event_type = str(event.get("type") or outer_type or "")
        kind = event_type
        if outer_type in {"events_api", "event_callback", "events"}:
            kind = event_type or "events_api"
        elif outer_type in {"interactive", "block_actions", "view_submission", "shortcut", "global_shortcut", "message_shortcut", "shortcuts"}:
            kind = outer_type if outer_type != "interactive" else event_type or "interactive"
        elif payload.get("command"):
            kind = "slash_commands"
        stable_id = str(event.get("event_id") or payload.get("event_id") or envelope_id or event.get("ts") or payload.get("trigger_id") or "")
        channel_id = str(event.get("channel") or payload.get("channel_id") or "")
        if isinstance(payload.get("channel"), Mapping):
            channel_id = str(payload["channel"].get("id") or channel_id)
        root_ts = str(event.get("thread_ts") or event.get("ts") or payload.get("thread_ts") or payload.get("message_ts") or "")
        # bot_message events intentionally carry no ``user`` field.  Their
        # stable authorization identity is the external app's B... bot_id;
        # channel membership APIs resolve that to a U... user id separately.
        actor_id = str(event.get("bot_id") or event.get("user") or payload.get("user_id") or "")
        display = str(event.get("text") or payload.get("text") or event.get("username") or event.get("name") or kind)
        # Self-generated bot messages are filtered only when Slack verifies
        # the bot identity; arbitrary bot/app actors are not blanket-dropped.
        if event_type in {"message", "app_mention", "bot_message"} and self._bot_user_id:
            source_actor = str(event.get("bot_id") or event.get("user") or "")
            if source_actor in {self._bot_user_id, self._bot_id}:
                return None
        normalized = dict(payload)
        if payload.get("event") is not None:
            normalized["event"] = event
        normalized.update({
            "type": kind,
            "kind": kind,
            "id": stable_id,
            "display": display,
            "team_id": team_id,
            "channel_id": channel_id,
            "root_ts": root_ts,
            "actor_id": actor_id,
            "_bridge_acknowledged": True,
            "envelope_id": envelope_id or payload.get("envelope_id"),
        })
        return normalized

    async def handle_socket_envelope(self, envelope: Any) -> None:
        """Ack exactly once, then invoke the explicit Socket Mode dispatcher."""
        envelope_id, _ = self._socket_payload(envelope)
        if envelope_id and self._socket is not None:
            ack = getattr(self._socket, "ack", None)
            if ack is not None:
                value = ack(envelope_id)
                if inspect.isawaitable(value):
                    await value
        normalized = self._normalize_socket_dispatch(envelope)
        kind = str(normalized.get("kind") if normalized else "")
        if normalized is None:
            return
        # One explicit callback owns all Socket Mode ingress. The legacy hooks
        # are compatibility shims for callers that have not migrated yet.
        if self._on_dispatch_cb is not None:
            result = self._on_dispatch_cb(normalized)
            if inspect.isawaitable(result):
                await result
            return
        event = normalized.get("event") if isinstance(normalized.get("event"), Mapping) else normalized
        if kind in {"message", "app_mention", "bot_message"}:
            if str(event.get("bot_id") or event.get("user") or "") in {self._bot_user_id, self._bot_id}:
                return
            if self._on_message_cb is not None:
                result = self._on_message_cb(self._normalize_message(event))
                if inspect.isawaitable(result):
                    await result
        elif kind in {"reaction_added", "reaction_removed"} and self._on_reaction_cb is not None:
            result = self._on_reaction_cb(self._normalize_reaction(event))
            if inspect.isawaitable(result):
                await result

    async def on_message(self, event: Any) -> None:
        await self.handle_socket_envelope(event)

    async def on_raw_reaction_add(self, payload: Any) -> None:
        if self._on_reaction_cb is not None:
            await self._on_reaction_cb(self._normalize_reaction(payload))

    def _normalize_message(self, event: Mapping[str, Any]) -> SlackMessage:
        channel_id = str(event.get("channel") or "")
        if not channel_id:
            raise ValueError("Slack message event omitted channel")
        files = event.get("files") or []
        if not isinstance(files, list):
            raise ValueError("Slack message files fixture must be a list")
        attachments = [SlackAttachment(
            filename=str(file.get("name") or file.get("title") or "attachment"),
            url=str(file.get("url_private_download") or file.get("url_private") or ""),
            content_type=str(file.get("mimetype") or "application/octet-stream"),
            size=int(file.get("size") or 0),
            _reader=lambda url=str(file.get("url_private_download") or file.get("url_private") or ""): self.download_private_file(url),
        ) for file in files if isinstance(file, Mapping)]
        message_ts = str(event.get("ts") or event.get("event_ts") or "")
        root_ts = str(event.get("thread_ts") or message_ts)
        actor_id = str(event.get("bot_id") or event.get("user") or "")
        return SlackMessage(
            team_id=str(event.get("team") or event.get("team_id") or self._team_id or ""),
            channel_id=channel_id,
            root_ts=root_ts,
            actor=SlackActor(actor_id, is_app=bool(event.get("bot_id"))),
            text=str(event.get("text") or ""),
            event_id=str(event.get("event_id")) if event.get("event_id") else None,
            message_ts=message_ts,
            files=tuple(attachments),
            raw=event,
        )

    @staticmethod
    def _normalize_reaction(event: Mapping[str, Any]) -> Mapping[str, Any]:
        item = event.get("item") or {}
        if not isinstance(item, Mapping):
            raise ValueError("Slack reaction item fixture must be an object")
        return {
            "type": str(event.get("type") or "reaction_added"),
            "user_id": str(event.get("user") or ""),
            "emoji": str(event.get("reaction") or ""),
            "channel_id": str(item.get("channel") or ""),
            "message_id": str(item.get("ts") or ""),
            "raw": event,
        }

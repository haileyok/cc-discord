"""Mattermost WebSocket client with auto-reconnection."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

import websockets
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)

EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class MattermostWebSocket:
    """Mattermost WebSocket client with auto-reconnection."""

    def __init__(
        self,
        server_url: str,
        token: str,
        event_handler: EventHandler,
    ) -> None:
        self._ws_url = self._build_ws_url(server_url)
        self._token = token
        self._handler = event_handler
        self._seq = 0
        self._ws: ClientConnection | None = None
        self._task: asyncio.Task[None] | None = None
        self._closing = False

    @staticmethod
    def _build_ws_url(server_url: str) -> str:
        """Build WebSocket URL from server URL."""
        url = server_url.rstrip("/")
        if url.startswith("https://"):
            url = "wss://" + url[8:]
        elif url.startswith("http://"):
            url = "ws://" + url[7:]
        return f"{url}/api/v4/websocket"

    async def start(self) -> None:
        """Start the WebSocket client."""
        self._closing = False
        self._task = asyncio.create_task(self._run_loop())

    async def close(self) -> None:
        """Close the WebSocket client."""
        self._closing = True
        if self._ws:
            await self._ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self) -> None:
        """Main connection loop with auto-reconnection."""
        backoff = 1.0
        max_backoff = 30.0
        while not self._closing:
            try:
                async with websockets.connect(
                    self._ws_url,
                    ping_interval=30,
                    ping_timeout=10,
                ) as ws:
                    self._ws = ws
                    await self._authenticate(ws)
                    backoff = 1.0
                    logger.info("Mattermost WebSocket connected")
                    async for raw in ws:
                        if self._closing:
                            break
                        await self._dispatch(raw)
            except (
                websockets.ConnectionClosed,
                ConnectionError,
                OSError,
            ) as e:
                if self._closing:
                    break
                logger.warning(
                    "WebSocket disconnected: %s — reconnecting in %.1fs",
                    e,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
            except Exception:
                logger.exception(
                    "Unexpected error in WebSocket loop — reconnecting in %.1fs",
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
            finally:
                self._ws = None

    async def _authenticate(self, ws: ClientConnection) -> None:
        """Send authentication challenge."""
        self._seq += 1
        await ws.send(
            json.dumps(
                {
                    "seq": self._seq,
                    "action": "authentication_challenge",
                    "data": {"token": self._token},
                }
            )
        )

    async def _dispatch(self, raw: str | bytes) -> None:
        """Dispatch incoming WebSocket message."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Non-JSON WebSocket message: %s", str(raw)[:200])
            return

        event = msg.get("event")
        if not event:
            return

        data = msg.get("data", {})

        # Double-decode posted events — data.post is a JSON string
        if event == "posted" and isinstance(data.get("post"), str):
            try:
                data["post"] = json.loads(data["post"])
            except json.JSONDecodeError:
                pass
        if event == "posted" and isinstance(data.get("mentions"), str):
            try:
                data["mentions"] = json.loads(data["mentions"])
            except json.JSONDecodeError:
                pass

        # reaction_added data.reaction may also be double-encoded
        if event == "reaction_added" and isinstance(data.get("reaction"), str):
            try:
                data["reaction"] = json.loads(data["reaction"])
            except json.JSONDecodeError:
                pass

        await self._handler(event, data)

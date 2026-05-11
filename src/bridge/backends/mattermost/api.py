"""Thin async wrapper around Mattermost REST API v4."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 16383


class MattermostAPI:
    """Thin async wrapper around Mattermost REST API v4."""

    def __init__(self, server_url: str, token: str) -> None:
        self._base = server_url.rstrip("/")
        self._token = token
        self._session: aiohttp.ClientSession | None = None

    @property
    def base_url(self) -> str:
        return self._base

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self._token}"},
        )

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    def _url(self, path: str) -> str:
        return f"{self._base}/api/v4{path}"

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> Any:
        assert self._session is not None, "call start() first"
        async with self._session.request(method, self._url(path), **kwargs) as resp:
            if resp.status == 429:
                retry_after = resp.headers.get("X-RateLimit-Reset", "1")
                raise RateLimitError(float(retry_after))
            resp.raise_for_status()
            if resp.content_type == "application/json":
                return await resp.json()
            return await resp.read()

    async def get_me(self) -> dict:
        return await self._request("GET", "/users/me")

    async def create_post(
        self,
        channel_id: str,
        message: str,
        *,
        root_id: str | None = None,
        file_ids: list[str] | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "channel_id": channel_id,
            "message": message,
        }
        if root_id:
            body["root_id"] = root_id
        if file_ids:
            body["file_ids"] = file_ids
        return await self._request("POST", "/posts", json=body)

    async def get_post(self, post_id: str) -> dict | None:
        try:
            return await self._request("GET", f"/posts/{post_id}")
        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                return None
            raise

    async def update_post(self, post_id: str, message: str) -> dict:
        return await self._request(
            "PUT", f"/posts/{post_id}", json={"id": post_id, "message": message}
        )

    async def add_reaction(
        self, user_id: str, post_id: str, emoji_name: str
    ) -> dict:
        return await self._request(
            "POST",
            "/reactions",
            json={
                "user_id": user_id,
                "post_id": post_id,
                "emoji_name": emoji_name,
            },
        )

    async def upload_file(
        self, channel_id: str, file_path: Path
    ) -> dict:
        assert self._session is not None
        file_bytes = file_path.read_bytes()
        data = aiohttp.FormData()
        data.add_field("channel_id", channel_id)
        data.add_field(
            "files",
            file_bytes,
            filename=file_path.name,
        )
        return await self._request("POST", "/files", data=data)

    async def download_file(self, file_id: str) -> bytes:
        return await self._request("GET", f"/files/{file_id}")

    async def get_file_info(self, file_id: str) -> dict:
        return await self._request("GET", f"/files/{file_id}/info")


class RateLimitError(Exception):
    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(f"Rate limited, retry after {retry_after}s")

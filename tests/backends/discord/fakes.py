"""Fake Discord bot for Discord backend tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FakeTypingContext:
    """Fake typing context manager."""

    entered: bool = False
    exited: bool = False

    async def __aenter__(self) -> FakeTypingContext:
        self.entered = True
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.exited = True


@dataclass
class FakeBotChannel:
    """Fake channel object from bot."""

    id: int = 1000
    typing_context: FakeTypingContext = field(default_factory=FakeTypingContext)

    def typing(self) -> FakeTypingContext:
        """Return a fake typing context manager."""
        return self.typing_context


@dataclass
class FakeHTTP:
    """Fake discord.http.HTTPClient."""

    pass


@dataclass
class FakeConnection:
    """Fake discord.gateway.DiscordWebSocket state."""

    _command_tree: Any = None


@dataclass
class FakeClient:
    """Fake discord.Client with minimal attributes needed for CommandTree."""

    http: FakeHTTP = field(default_factory=FakeHTTP)
    _connection: FakeConnection = field(default_factory=FakeConnection)


@dataclass
class FakeDiscordBot:
    """Minimal fake Discord Bot for testing commands and Discord-specific logic."""

    _client: Any = field(default_factory=lambda: FakeClient())
    _post_calls: list[dict] = field(default_factory=list)
    _thread_calls: list[dict] = field(default_factory=list)
    _archive_calls: list[dict] = field(default_factory=list)
    _reaction_calls: list[dict] = field(default_factory=list)
    _fake_channels: dict[str, FakeBotChannel] = field(default_factory=dict)
    is_ready: bool = True

    @property
    def client(self) -> Any:
        return self._client

    @property
    def channel(self) -> Any:
        return FakeBotChannel()

    async def post(self, content: str, *, thread_id: str | None = None) -> list[str]:
        """Fake post: record the call, return a fake message ID."""
        self._post_calls.append({"content": content, "thread_id": thread_id})
        return ["1001"]

    async def create_thread(self, name: str) -> str:
        """Fake create_thread: record the call, return a fake thread ID."""
        thread_id = str(2000 + len(self._thread_calls))
        self._thread_calls.append({"name": name})
        return thread_id

    async def archive_thread(self, thread_id: str) -> None:
        """Fake archive_thread: record the call."""
        self._archive_calls.append({"thread_id": thread_id})

    async def add_reactions(self, message_id: str, thread_id: str, emoji: list[str]) -> None:
        """Fake add_reactions: record the call."""
        self._reaction_calls.append({"message_id": message_id, "thread_id": thread_id, "emoji": emoji})

    async def fetch_messageable(self, thread_id: str) -> FakeBotChannel:
        """Fake fetch_messageable: return a FakeBotChannel."""
        if thread_id not in self._fake_channels:
            self._fake_channels[thread_id] = FakeBotChannel(id=int(thread_id) if thread_id.isdigit() else 1000)
        return self._fake_channels[thread_id]

    async def thread_alive(self, thread_id: str) -> bool:
        """Fake thread_alive: always return True."""
        return True

    async def rename_thread(self, thread_id: str, name: str) -> None:
        """Fake rename_thread: no-op."""
        pass

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        *,
        content: str | None = None,
    ) -> None:
        """Fake edit_message: no-op."""
        pass

    async def post_with_attachments(
        self,
        file_paths: list[Path],
        *,
        thread_id: str | None = None,
        text: str | None = None,
    ) -> list[str]:
        """Fake post_with_attachments: record the call, return fake message IDs."""
        self._post_calls.append({"content": text, "thread_id": thread_id, "files": file_paths})
        return ["1001"]

    async def download_attachment(
        self, attachment_ref: Any, dest_dir: Path
    ) -> Path:
        """Fake download_attachment: return a fake path."""
        return dest_dir / "fake_download.txt"

    def get_post_calls(self) -> list[dict]:
        return self._post_calls

    def get_thread_calls(self) -> list[dict]:
        return self._thread_calls

    def get_archive_calls(self) -> list[dict]:
        return self._archive_calls

    def get_reaction_calls(self) -> list[dict]:
        return self._reaction_calls

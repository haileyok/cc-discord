"""Shared test fixtures for FakeBot, FakePlatform, and FakeZellij."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FakeTypingContext:
    """Fake typing context manager."""

    entered: bool = False
    exited: bool = False

    async def __aenter__(self) -> "FakeTypingContext":
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
class FakeBot:
    """Minimal fake Bot for testing commands and tasks."""

    _client: Any = field(default_factory=lambda: FakeClient())
    _post_calls: list[dict] = field(default_factory=list)
    _thread_calls: list[dict] = field(default_factory=list)
    _archive_calls: list[dict] = field(default_factory=list)
    _reaction_calls: list[dict] = field(default_factory=list)
    _fake_channels: dict[int, FakeBotChannel] = field(default_factory=dict)
    is_ready: bool = True

    @property
    def client(self) -> Any:
        return self._client

    @property
    def channel(self) -> Any:
        return FakeBotChannel()

    async def post(self, content: str, *, thread_id: int | None = None) -> list[int]:
        """Fake post: record the call, return a fake message ID."""
        self._post_calls.append({"content": content, "thread_id": thread_id})
        return [1001]

    async def create_thread(self, name: str) -> int:
        """Fake create_thread: record the call, return a fake thread ID."""
        thread_id = 2000 + len(self._thread_calls)
        self._thread_calls.append({"name": name})
        return thread_id

    async def archive_thread(self, thread_id: int) -> None:
        """Fake archive_thread: record the call."""
        self._archive_calls.append({"thread_id": thread_id})

    async def add_reactions(self, message_id: int, thread_id: int, emoji: list[str]) -> None:
        """Fake add_reactions: record the call."""
        self._reaction_calls.append({"message_id": message_id, "thread_id": thread_id, "emoji": emoji})

    async def fetch_messageable(self, thread_id: int) -> FakeBotChannel:
        """Fake fetch_messageable: return a FakeBotChannel."""
        if thread_id not in self._fake_channels:
            self._fake_channels[thread_id] = FakeBotChannel(id=thread_id)
        return self._fake_channels[thread_id]

    def get_post_calls(self) -> list[dict]:
        return self._post_calls

    def get_thread_calls(self) -> list[dict]:
        return self._thread_calls

    def get_archive_calls(self) -> list[dict]:
        return self._archive_calls

    def get_reaction_calls(self) -> list[dict]:
        return self._reaction_calls


@dataclass
class FakePlatform:
    """Fake ChatPlatform implementation for testing core modules.

    Uses string IDs throughout (platform-agnostic interface).
    Tracks all operations for assertion in tests.
    """

    is_ready: bool = True
    _post_calls: list[dict] = field(default_factory=list)
    _attachment_calls: list[dict] = field(default_factory=list)
    _thread_calls: list[dict] = field(default_factory=list)
    _archive_calls: list[dict] = field(default_factory=list)
    _reaction_calls: list[dict] = field(default_factory=list)
    _edit_calls: list[dict] = field(default_factory=list)
    _download_calls: list[dict] = field(default_factory=list)
    _thread_counter: int = 0
    _message_counter: int = 0

    async def start(self) -> None:
        """Start the platform (no-op for fake)."""
        pass

    async def close(self) -> None:
        """Close the platform (no-op for fake)."""
        pass

    async def post(
        self, message: str, *, thread_id: str | None = None
    ) -> list[str]:
        """Fake post: record the call, return a fake message ID."""
        self._message_counter += 1
        msg_id = str(1000 + self._message_counter)
        self._post_calls.append(
            {"content": message, "thread_id": thread_id, "msg_id": msg_id}
        )
        return [msg_id]

    async def post_with_attachments(
        self,
        file_paths: list[Path],
        *,
        thread_id: str | None = None,
        text: str | None = None,
    ) -> list[str]:
        """Fake post_with_attachments: record the call, return a fake message ID."""
        self._message_counter += 1
        msg_id = str(1000 + self._message_counter)
        self._attachment_calls.append(
            {
                "file_paths": file_paths,
                "thread_id": thread_id,
                "text": text,
                "msg_id": msg_id,
            }
        )
        return [msg_id]

    async def create_thread(self, name: str) -> str:
        """Fake create_thread: record the call, return a fake thread ID."""
        self._thread_counter += 1
        tid = str(2000 + self._thread_counter)
        self._thread_calls.append({"name": name, "thread_id": tid})
        return tid

    async def archive_thread(self, thread_id: str) -> None:
        """Fake archive_thread: record the call."""
        self._archive_calls.append({"thread_id": thread_id})

    async def rename_thread(self, thread_id: str, name: str) -> None:
        """Fake rename_thread: record the call."""
        pass

    async def thread_alive(self, thread_id: str) -> bool:
        """Fake thread_alive: always return True."""
        return True

    async def download_attachment(
        self, attachment_ref: Any, dest_dir: Path
    ) -> Path:
        """Fake download_attachment: record the call, return a fake path."""
        self._download_calls.append({"ref": attachment_ref, "dest_dir": dest_dir})
        return dest_dir / "fake_download.txt"

    async def add_reactions(
        self, message_id: str, thread_id: str, emoji: list[str]
    ) -> None:
        """Fake add_reactions: record the call."""
        self._reaction_calls.append(
            {"message_id": message_id, "thread_id": thread_id, "emoji": emoji}
        )

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        *,
        content: str | None = None,
    ) -> None:
        """Fake edit_message: record the call."""
        self._edit_calls.append(
            {"thread_id": thread_id, "message_id": message_id, "content": content}
        )

    async def fetch_messageable(self, thread_id: str) -> Any:
        """Fake fetch_messageable: return a FakeBotChannel."""
        return FakeBotChannel()


@dataclass
class FakeZellij:
    """Minimal fake ZellijManager for testing."""

    _spawn_calls: list[dict] = field(default_factory=list)
    _write_calls: list[dict] = field(default_factory=list)
    _close_calls: list[dict] = field(default_factory=list)

    async def spawn_task(
        self, cwd: str, pane_name: str, layout_path: str
    ) -> str:
        """Fake spawn_task. The new contract takes a layout file path
        instead of env+extra_argv (env vars and claude argv now live in
        the layout)."""
        self._spawn_calls.append(
            {"cwd": cwd, "pane_name": pane_name, "layout_path": layout_path}
        )
        return "terminal_1"

    async def write_to_pane(self, pane_id: str, text: str) -> None:
        """Fake write_to_pane."""
        self._write_calls.append({"pane_id": pane_id, "text": text})

    async def close_pane(self, pane_id: str) -> None:
        """Fake close_pane."""
        self._close_calls.append({"pane_id": pane_id})

    async def list_panes(self) -> list[dict]:
        """Fake list_panes."""
        return []

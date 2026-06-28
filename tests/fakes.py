"""Shared test fakes for FakeBot, FakeSupervisor, and FakePolytokenClient."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeTypingContext:
    entered: bool = False
    exited: bool = False

    async def __aenter__(self) -> "FakeTypingContext":
        self.entered = True
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.exited = True


@dataclass
class FakeBotChannel:
    id: int = 1000
    typing_context: FakeTypingContext = field(default_factory=FakeTypingContext)

    def typing(self) -> FakeTypingContext:
        return self.typing_context


@dataclass
class FakeHTTP:
    pass


@dataclass
class FakeConnection:
    _command_tree: Any = None


@dataclass
class FakeClient:
    http: FakeHTTP = field(default_factory=FakeHTTP)
    _connection: FakeConnection = field(default_factory=FakeConnection)


@dataclass
class FakeBot:
    """Minimal fake Bot for testing commands and tasks."""

    _client: Any = field(default_factory=lambda: FakeClient())
    _post_calls: list[dict] = field(default_factory=list)
    _thread_calls: list[dict] = field(default_factory=list)
    _channel_calls: list[dict] = field(default_factory=list)
    _archive_calls: list[dict] = field(default_factory=list)
    _reaction_calls: list[dict] = field(default_factory=list)
    _embed_calls: list[dict] = field(default_factory=list)
    _edit_calls: list[dict] = field(default_factory=list)
    _rename_calls: list[dict] = field(default_factory=list)
    _attachment_calls: list[dict] = field(default_factory=list)
    _fake_channels: dict[int, FakeBotChannel] = field(default_factory=dict)
    _next_embed_id: int = 5000
    is_ready: bool = True
    channel_id: int = 12345

    @property
    def client(self) -> Any:
        return self._client

    @property
    def channel(self) -> Any:
        return FakeBotChannel()

    async def post(self, content: str, *, thread_id: int | None = None) -> list[int]:
        self._post_calls.append({"content": content, "thread_id": thread_id})
        return [1001]

    async def post_with_attachments(
        self, file_paths: list[str], *, thread_id: int | None = None, text: str | None = None
    ) -> list[int]:
        self._attachment_calls.append({"files": file_paths, "thread_id": thread_id, "text": text})
        return [1002]

    async def post_embed(self, embed: Any, *, thread_id: int | None = None) -> int:
        self._next_embed_id += 1
        self._embed_calls.append({"embed": embed, "thread_id": thread_id, "id": self._next_embed_id})
        return self._next_embed_id

    async def edit_message(self, thread_id: int, message_id: int, *, content=None, embed=None) -> None:
        self._edit_calls.append({"thread_id": thread_id, "message_id": message_id, "embed": embed})

    async def rename_thread(self, thread_id: int, name: str) -> None:
        self._rename_calls.append({"thread_id": thread_id, "name": name})

    async def create_thread(self, name: str) -> int:
        thread_id = 2000 + len(self._thread_calls)
        self._thread_calls.append({"name": name})
        return thread_id

    async def create_channel(self, name: str) -> int:
        channel_id = 3000 + len(self._channel_calls)
        self._channel_calls.append({"name": name})
        return channel_id

    async def archive_thread(self, thread_id: int) -> None:
        self._archive_calls.append({"thread_id": thread_id})

    async def add_reactions(self, message_id: int, thread_id: int, emoji: list[str]) -> None:
        self._reaction_calls.append({"message_id": message_id, "thread_id": thread_id, "emoji": emoji})

    async def fetch_messageable(self, thread_id: int) -> FakeBotChannel:
        if thread_id not in self._fake_channels:
            self._fake_channels[thread_id] = FakeBotChannel(id=thread_id)
        return self._fake_channels[thread_id]

    def get_post_calls(self) -> list[dict]:
        return self._post_calls

    def get_thread_calls(self) -> list[dict]:
        return self._thread_calls

    def get_archive_calls(self) -> list[dict]:
        return self._archive_calls


@dataclass
class _SpawnResult:
    session_id: str
    port: int


@dataclass
class _SessionInfo:
    session_id: str
    port: int
    pid: int = 1234
    started_at: str = "2026-06-15T00:00:00Z"
    project_path: str = "/tmp"


@dataclass
class FakeSupervisor:
    """Fake DaemonSupervisor: deterministic spawn + a controllable session registry."""

    _spawn_calls: list[dict] = field(default_factory=list)
    _next_port: int = 40000
    _seq: int = 0
    fail_spawn: bool = False
    sessions: list[_SessionInfo] = field(default_factory=list)
    terminated: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    fail_list: bool = False
    resume_calls: list[dict] = field(default_factory=list)
    fail_resume: bool = False

    async def spawn(self, cwd: str, *, config_dir: str | None = None) -> _SpawnResult:
        if self.fail_spawn:
            from bridge.daemon_supervisor import DaemonSupervisorError

            raise DaemonSupervisorError("boom")
        self._seq += 1
        self._next_port += 1
        sid = f"sess-{self._seq}"
        self._spawn_calls.append({"cwd": cwd, "session_id": sid, "port": self._next_port})
        self.sessions.append(_SessionInfo(sid, self._next_port, project_path=cwd))
        return _SpawnResult(sid, self._next_port)

    async def resume(self, session_id: str, cwd: str, *, config_dir: str | None = None, discover_timeout: float = 20.0) -> _SpawnResult:
        if self.fail_resume:
            from bridge.daemon_supervisor import DaemonSupervisorError

            raise DaemonSupervisorError("resume failed")
        self._next_port += 1
        port = self._next_port
        self.resume_calls.append({"session_id": session_id, "cwd": cwd, "port": port})
        # The resumed daemon re-registers on a new port.
        self.sessions = [s for s in self.sessions if s.session_id != session_id]
        self.sessions.append(_SessionInfo(session_id, port, project_path=cwd))
        return _SpawnResult(session_id, port)

    def _maybe_fail(self):
        if self.fail_list:
            from bridge.daemon_supervisor import DaemonSupervisorError

            raise DaemonSupervisorError("registry listing failed")

    async def list_sessions(self) -> list[_SessionInfo]:
        self._maybe_fail()
        return list(self.sessions)

    async def find_session(self, session_id: str):
        self._maybe_fail()
        for s in self.sessions:
            if s.session_id == session_id:
                return s
        return None

    async def list_models(self) -> list[str]:
        return list(self.models) or ["anthropic/claude-opus-4-8", "openai/gpt-5.5"]

    async def terminate(self, session_id: str) -> bool:
        self.terminated.append(session_id)
        return True


@dataclass
class FakePolytokenClient:
    """Fake PolytokenClient injected into TaskRegistry._clients for routing tests."""

    port: int = 40001
    prompts: list[str] = field(default_factory=list)
    cancelled: int = 0
    terminated: int = 0
    interrogative_responses: list[dict] = field(default_factory=list)
    model_calls: list[dict] = field(default_factory=list)
    facet_calls: list[str] = field(default_factory=list)
    terminate_error_status: int | None = None
    state_payload: dict = field(default_factory=lambda: {
        "active_model": "anthropic/claude-opus-4-8",
        "active_facet": "execute",
        "active_reasoning_effort": "high",
        "available_skills": ["brainstorming", "code-review"],
        "session_title": "fake-title",
        "todos": [],
    })
    closed: bool = False

    async def prompt(self, content: str, *, max_tool_turns=None):
        self.prompts.append(content)

        class _Accepted:
            prompt_id = "p"
            session_id = "s"
            resolved_references: list = []

        return _Accepted()

    async def state(self) -> dict:
        return dict(self.state_payload)

    async def cancel_turn(self):
        self.cancelled += 1

    async def terminate(self):
        if self.terminate_error_status is not None:
            from bridge.polytoken_client import PolytokenClientError

            raise PolytokenClientError("rejected", status=self.terminate_error_status)
        self.terminated += 1

    async def set_model(self, model: str, *, reasoning_effort=None):
        self.model_calls.append({"model": model, "reasoning_effort": reasoning_effort})

    async def set_facet(self, facet: str):
        self.facet_calls.append(facet)

    async def respond_interrogative(self, interrogative_id: str, response: dict):
        self.interrogative_responses.append({"id": interrogative_id, "response": response})

    async def aclose(self):
        self.closed = True

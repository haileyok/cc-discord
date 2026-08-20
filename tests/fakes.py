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
    _reaction_calls: list[dict] = field(default_factory=list)
    _edit_calls: list[dict] = field(default_factory=list)
    _attachment_calls: list[dict] = field(default_factory=list)
    is_ready: bool = True
    team_id: str = "T-test"
    home_channel_id: str = "G-test-home"
    owner_user_id: str = "U-test-owner"
    channel_id: str = "G-test-home"

    @property
    def client(self) -> Any:
        return self._client

    @property
    def channel(self) -> Any:
        return FakeBotChannel()

    async def post(self, content: str, *, channel_id: str | None = None, root_ts: str | None = None,
                   blocks: list[dict[str, Any]] | None = None, fallback_text: str | None = None) -> list[str]:
        self._post_calls.append({"content": content, "channel_id": channel_id, "root_ts": root_ts, "blocks": blocks})
        return ["1001"]

    async def post_with_attachments(
        self, file_paths: list[str], *, channel_id: str | None = None, root_ts: str | None = None,
        text: str | None = None
    ) -> list[str]:
        self._attachment_calls.append({"files": file_paths, "channel_id": channel_id, "root_ts": root_ts, "text": text})
        return ["1002"]

    async def edit_message(self, channel_id: str, message_ts: str, *, text=None, blocks=None) -> None:
        self._edit_calls.append({"channel_id": channel_id, "message_ts": message_ts, "text": text, "blocks": blocks})

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


class UnexpectedSlackCall(AssertionError):
    """Raised when a scripted Slack fake receives an unplanned operation."""


class MalformedSlackFixture(ValueError):
    """Raised when a scripted response/event is not a valid Slack fixture."""


@dataclass
class ScriptedSlackResponse:
    """Small response object matching the fields consumed by the adapter."""

    data: dict[str, Any]
    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    @property
    def ok(self) -> bool:
        return bool(self.data.get("ok", True))


@dataclass
class ScriptedSlackCall:
    method: str
    kwargs: dict[str, Any]


@dataclass
class FakeSlackClient:
    """Deterministic strict fake for official ``AsyncWebClient`` methods.

    ``script`` is an ordered list of ``(method, response)`` pairs.  Every call
    must consume the next pair and kwargs are optionally checked exactly via
    ``expected_kwargs``.  Unexpected calls and malformed fixture responses fail
    immediately rather than silently creating unrealistic test behavior.
    """

    script: list[tuple[str, Any]] = field(default_factory=list)
    expected_kwargs: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[ScriptedSlackCall] = field(default_factory=list)

    def _validate_response(self, method: str, response: Any) -> Any:
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, dict):
            if "ok" not in response:
                response = {"ok": True, **response}
            return ScriptedSlackResponse(response)
        if isinstance(response, ScriptedSlackResponse):
            if not isinstance(response.data, dict) or "ok" not in response.data:
                raise MalformedSlackFixture(f"{method}: response must include an ok field")
            return response
        raise MalformedSlackFixture(f"{method}: unsupported response fixture {response!r}")

    def assert_complete(self) -> None:
        if self.script:
            raise AssertionError(f"unconsumed Slack calls: {self.script!r}")

    async def _call(self, method: str, **kwargs: Any) -> Any:
        if not self.script:
            raise UnexpectedSlackCall(f"unexpected Slack call {method}({kwargs!r}); script exhausted")
        expected_method, response = self.script.pop(0)
        if expected_method != method:
            raise UnexpectedSlackCall(
                f"expected Slack call {expected_method}, got {method}({kwargs!r})"
            )
        expected = self.expected_kwargs.get(method)
        if expected is not None and kwargs != expected:
            raise UnexpectedSlackCall(
                f"unexpected kwargs for {method}: expected {expected!r}, got {kwargs!r}"
            )
        self.calls.append(ScriptedSlackCall(method, dict(kwargs)))
        return self._validate_response(method, response)

    async def auth_test(self, **kwargs: Any) -> Any:
        return await self._call("auth_test", **kwargs)

    async def users_info(self, **kwargs: Any) -> Any:
        return await self._call("users_info", **kwargs)

    async def conversations_info(self, **kwargs: Any) -> Any:
        return await self._call("conversations_info", **kwargs)

    async def bots_info(self, **kwargs: Any) -> Any:
        return await self._call("bots_info", **kwargs)

    async def chat_postMessage(self, **kwargs: Any) -> Any:
        return await self._call("chat_postMessage", **kwargs)

    async def chat_update(self, **kwargs: Any) -> Any:
        return await self._call("chat_update", **kwargs)

    async def reactions_add(self, **kwargs: Any) -> Any:
        return await self._call("reactions_add", **kwargs)

    async def files_getUploadURLExternal(self, **kwargs: Any) -> Any:
        return await self._call("files_getUploadURLExternal", **kwargs)

    async def files_completeUploadExternal(self, **kwargs: Any) -> Any:
        return await self._call("files_completeUploadExternal", **kwargs)

    async def conversations_create(self, **kwargs: Any) -> Any:
        return await self._call("conversations_create", **kwargs)

    async def conversations_invite(self, **kwargs: Any) -> Any:
        return await self._call("conversations_invite", **kwargs)

    async def conversations_archive(self, **kwargs: Any) -> Any:
        return await self._call("conversations_archive", **kwargs)

    async def conversations_kick(self, **kwargs: Any) -> Any:
        return await self._call("conversations_kick", **kwargs)

    async def conversations_replies(self, **kwargs: Any) -> Any:
        return await self._call("conversations_replies", **kwargs)


@dataclass
class FakeEnvelopeAcknowledger:
    """Strict envelope ack fake with deterministic malformed-fixture checks."""

    acknowledgements: list[str] = field(default_factory=list)
    expected: list[str] = field(default_factory=list)

    async def ack(self, envelope_id: str) -> None:
        if not isinstance(envelope_id, str) or not envelope_id:
            raise MalformedSlackFixture("Socket Mode envelope_id must be a non-empty string")
        if self.expected and envelope_id != self.expected[len(self.acknowledgements)]:
            raise UnexpectedSlackCall(
                f"unexpected envelope ack {envelope_id!r}; expected {self.expected!r}"
            )
        self.acknowledgements.append(envelope_id)

    def assert_complete(self) -> None:
        if self.expected != self.acknowledgements:
            raise AssertionError(
                f"missing envelope acknowledgements: expected {self.expected!r}, "
                f"got {self.acknowledgements!r}"
            )


@dataclass
class FakeSocketMode:
    """Injectable Socket Mode abstraction used by adapter tests."""

    acknowledger: FakeEnvelopeAcknowledger = field(default_factory=FakeEnvelopeAcknowledger)
    handler: Any = None
    connected: bool = False

    def register_handler(self, handler: Any) -> None:
        self.handler = handler

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def ack(self, envelope_id: str) -> None:
        await self.acknowledger.ack(envelope_id)

    async def dispatch(self, envelope: dict[str, Any]) -> None:
        if self.handler is None:
            raise UnexpectedSlackCall("Socket Mode dispatch before handler registration")
        if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), dict):
            raise MalformedSlackFixture("Socket Mode fixture requires an object payload")
        await self.handler(envelope)

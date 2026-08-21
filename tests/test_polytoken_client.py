"""Tests for bridge.polytoken_client against a small local aiohttp stub."""

import json

import pytest
from aiohttp import test_utils, web

from bridge.polytoken_client import (
    PolytokenClient,
    PolytokenClientError,
    PolytokenCredentialError,
    PromptDenied,
    SseEnvelope,
    TurnInFlight,
    load_bearer_token,
)


def _build_stub_app() -> web.Application:
    app = web.Application()
    app["authorization_headers"] = []

    @web.middleware
    async def record_authorization(request: web.Request, handler):
        app["authorization_headers"].append(request.headers.get("Authorization"))
        return await handler(request)

    app.middlewares.append(record_authorization)

    async def prompt(request: web.Request) -> web.Response:
        body = await request.json()
        mode = request.headers.get("X-Test-Mode")
        if mode == "409":
            return web.json_response({"error": "in flight"}, status=409)
        if mode == "422":
            return web.json_response({"prompt_id": "p-denied", "blocked_by_hook": "context-policy", "reason": "sensitive detail"}, status=422)
        return web.json_response(
            {
                "prompt_id": "p-1",
                "session_id": "s-1",
                "resolved_references": [{"kind": "file", "name": body["content"]}],
            },
            status=202,
        )

    async def state(_request: web.Request) -> web.Response:
        return web.json_response({"active_facet": "execute", "todos": []})

    async def title(request: web.Request) -> web.Response:
        body = await request.json()
        return web.json_response({"title": body["title"]})

    async def model(request: web.Request) -> web.Response:
        return web.json_response(await request.json())

    async def cancel(_request: web.Request) -> web.Response:
        return web.json_response({"status": "cancel_requested"}, status=202)

    async def reload(_request: web.Request) -> web.Response:
        return web.json_response({"reloaded": ["models", "skills"], "failed": []})

    async def compact(request: web.Request) -> web.Response:
        assert request.content_type == "application/json"
        assert await request.json() == {}
        return web.json_response({"compaction_id": "compact-123"}, status=202)

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def events(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            status=200, headers={"Content-Type": "text/event-stream"}
        )
        await resp.prepare(request)
        frames = [
            {"seq": 0, "session_id": "s-1", "emitted_at": "t0",
             "event": {"type": "message_start", "prompt_id": "p-1"}},
            {"seq": 1, "session_id": "s-1", "emitted_at": "t1",
             "event": {"type": "tool_call", "prompt_id": "p-1", "call_id": "c1",
                       "name": "shell_exec", "subagent_handle": "agent-7"}},
            {"seq": 2, "session_id": "s-1", "emitted_at": "t2",
             "event": {"type": "message_complete", "prompt_id": "p-1"}},
        ]
        for f in frames:
            await resp.write(f": keep-alive\n".encode())
            await resp.write(f"id: {f['seq']}\n".encode())
            await resp.write(f"data: {json.dumps(f)}\n\n".encode())
        await resp.write_eof()
        return resp

    async def slow(_request: web.Request) -> web.Response:
        import asyncio

        await asyncio.sleep(2)
        return web.json_response({"ok": True})

    app.router.add_get("/slow", slow)
    app.router.add_post("/prompt", prompt)
    app.router.add_get("/state", state)
    app.router.add_post("/title", title)
    app.router.add_post("/model", model)
    app.router.add_post("/turn/cancel", cancel)
    app.router.add_post("/reload", reload)
    app.router.add_post("/compact", compact)
    app.router.add_get("/health", health)
    app.router.add_get("/events", events)
    return app


def _credential_file(tmp_path, token: str = "daemon-secret"):
    path = tmp_path / "credential.json"
    path.write_text(json.dumps({"version": 1, "kind": "polytoken-daemon-credential", "token": token}), encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture
async def stub_port():
    server = test_utils.TestServer(_build_stub_app())
    await server.start_server()
    try:
        yield server.port
    finally:
        await server.close()


@pytest.fixture
async def auth_stub():
    app = _build_stub_app()
    server = test_utils.TestServer(app)
    await server.start_server()
    try:
        yield server.port, app["authorization_headers"]
    finally:
        await server.close()


class TestPolytokenClient:
    async def test_prompt_happy_path(self, stub_port) -> None:
        async with PolytokenClient(stub_port) as client:
            accepted = await client.prompt("hello @/tmp/x.txt")
        assert accepted.prompt_id == "p-1"
        assert accepted.session_id == "s-1"
        assert accepted.resolved_references == [{"kind": "file", "name": "hello @/tmp/x.txt"}]

    async def test_auth_header_is_sent_to_ordinary_and_sse_requests(self, auth_stub, tmp_path) -> None:
        port, authorization_headers = auth_stub
        credential = _credential_file(tmp_path)
        async with PolytokenClient(port, credential_file_path=credential) as client:
            await client.state()
            async for _ in client.stream_events():
                pass
        assert authorization_headers == ["Bearer daemon-secret", "Bearer daemon-secret"]

    async def test_prompt_max_tool_turns_included(self, stub_port) -> None:
        async with PolytokenClient(stub_port) as client:
            accepted = await client.prompt("hi", max_tool_turns=3)
        assert accepted.prompt_id == "p-1"

    async def test_prompt_409_turn_in_flight(self, stub_port) -> None:
        async with PolytokenClient(stub_port) as client:
            client._timeout = client._timeout  # keep ref
            with pytest.raises(TurnInFlight):
                # Inject the test-mode header via a one-off session call.
                await _prompt_with_header(client, "hi", "409")

    async def test_prompt_422_denied(self, stub_port) -> None:
        async with PolytokenClient(stub_port) as client:
            with pytest.raises(PromptDenied) as caught:
                await _prompt_with_header(client, "hi", "422")
        assert caught.value.code == "hook.context-policy"
        assert caught.value.body is None
        assert "sensitive detail" not in str(caught.value)

    async def test_state(self, stub_port) -> None:
        async with PolytokenClient(stub_port) as client:
            st = await client.state()
        assert st["active_facet"] == "execute"

    async def test_set_title(self, stub_port) -> None:
        async with PolytokenClient(stub_port) as client:
            res = await client.set_title("my title")
        assert res["title"] == "my title"

    async def test_set_model_with_effort(self, stub_port) -> None:
        async with PolytokenClient(stub_port) as client:
            res = await client.set_model("anthropic/claude-opus-4-8", reasoning_effort="high")
        assert res == {"model": "anthropic/claude-opus-4-8", "reasoning_effort": "high"}

    async def test_cancel_turn(self, stub_port) -> None:
        async with PolytokenClient(stub_port) as client:
            res = await client.cancel_turn()
        assert res["status"] == "cancel_requested"

    async def test_reload(self, stub_port) -> None:
        async with PolytokenClient(stub_port) as client:
            result = await client.reload()
        assert result == {"reloaded": ["models", "skills"], "failed": []}

    async def test_compact_returns_accepted_operation_id(self, stub_port) -> None:
        async with PolytokenClient(stub_port) as client:
            result = await client.compact()
        assert result == "compact-123"

    async def test_health_true(self, stub_port) -> None:
        async with PolytokenClient(stub_port) as client:
            assert await client.health() is True

    async def test_health_false_on_dead_port(self) -> None:
        # Nothing is listening on this port.
        async with PolytokenClient(59999) as client:
            assert await client.health() is False

    async def test_transport_error_has_none_status(self) -> None:
        async with PolytokenClient(59999) as client:
            with pytest.raises(PolytokenClientError) as ei:
                await client.state()
        assert ei.value.status is None

    async def test_request_timeout_wrapped(self, stub_port) -> None:
        # A ClientTimeout raises asyncio.TimeoutError; it must surface as a
        # PolytokenClientError (status None), not leak as an unhandled exception.
        async with PolytokenClient(stub_port, timeout_secs=0.1) as client:
            with pytest.raises(PolytokenClientError) as ei:
                await client._request("GET", "/slow")
        assert ei.value.status is None

    async def test_stream_events_parses_and_routes(self, stub_port) -> None:
        envs: list[SseEnvelope] = []
        async with PolytokenClient(stub_port) as client:
            async for env in client.stream_events():
                envs.append(env)
        assert [e.event_type for e in envs] == ["message_start", "tool_call", "message_complete"]
        assert [e.seq for e in envs] == [0, 1, 2]
        # subagent_handle is the routing key.
        assert envs[0].subagent_handle is None
        assert envs[1].subagent_handle == "agent-7"


def test_load_bearer_token_validates_file_mode_and_schema(tmp_path) -> None:
    path = _credential_file(tmp_path, "very-secret")
    assert load_bearer_token(path) == "very-secret"
    path.chmod(0o644)
    with pytest.raises(PolytokenCredentialError) as exc:
        load_bearer_token(path)
    assert "very-secret" not in str(exc.value)

    path.chmod(0o600)
    path.write_text(json.dumps({"version": 1, "kind": "wrong", "token": "very-secret"}), encoding="utf-8")
    with pytest.raises(PolytokenCredentialError):
        load_bearer_token(path)


def test_parse_envelope_bad_json() -> None:
    assert PolytokenClient._parse_envelope("{not json") is None


def test_parse_envelope_missing_event() -> None:
    assert PolytokenClient._parse_envelope('{"seq": 1}') is None


def test_parse_envelope_ok() -> None:
    env = PolytokenClient._parse_envelope(
        '{"seq": 5, "session_id": "s", "emitted_at": "t", "event": {"type": "heartbeat"}}'
    )
    assert env is not None
    assert env.seq == 5
    assert env.event_type == "heartbeat"


async def _prompt_with_header(client: PolytokenClient, content: str, mode: str):
    """Drive /prompt with a test-mode header to exercise error mapping."""
    session = client._ensure_session()
    async with session.post(
        client._url("/prompt"), json={"content": content}, headers={"X-Test-Mode": mode}
    ) as resp:
        text = await resp.text()
        if resp.status >= 400:
            client._raise_for_status("POST", "/prompt", resp.status, text)

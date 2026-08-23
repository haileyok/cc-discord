"""Slack server lifecycle and health contract tests (AC.1)."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import test_utils

from bridge.bot import Bot
from bridge.mcp_api import McpCapability
from bridge.server import build_app, make_interaction_dispatcher, make_message_dispatcher, make_socket_dispatcher
from bridge.tasks import normalize_message
import bridge.server as server_module


@dataclass
class LocalBot:
    is_ready: bool = True
    team_id: str = "T1"
    home_channel_id: str = "GHOME"
    bot_user_id: str = "UBOT"
    owner_user_id: str = "UOWNER"
    socket_mode_connected: bool = True
    events: list[str] = field(default_factory=list)

    def health(self) -> dict[str, Any]:
        return {"bot_connected": self.is_ready, "slack_connected": self.is_ready,
                "socket_mode_connected": self.socket_mode_connected, "team_id": self.team_id,
                "home_channel_id": self.home_channel_id, "bot_user_id": self.bot_user_id,
                "bot_token": "must-not-leak"}


class RecordingRegistry:
    def __init__(self, *, fail: bool = False) -> None:
        self.routed: list[Any] = []
        self.fail = fail
        self.events: list[str] = []

    async def maybe_route_message(self, message: Any) -> bool:
        self.routed.append(message)
        if self.fail:
            raise RuntimeError("boom")
        return True


@pytest.mark.asyncio
async def test_health_reports_slack_identity_without_secrets() -> None:
    bot = LocalBot()
    app = await build_app(bot, started_at=0.0)
    async with test_utils.TestClient(test_utils.TestServer(app)) as client:
        response = await client.get("/v1/health")
        assert response.status == 200
        data = json.loads(await response.text())
    assert data["bot_connected"] is True
    assert data["team_id"] == "T1"
    assert data["home_channel_id"] == "GHOME"
    assert data["bot_user_id"] == "UBOT"
    assert "token" not in json.dumps(data).lower()


@pytest.mark.asyncio
async def test_private_mcp_rpc_requires_auth_and_derives_principal() -> None:
    class Facade:
        owner_user_id = "UOWNER"
        team_id = "T1"
        calls = []

        async def call(self, tool, arguments, context):
            self.calls.append((tool, arguments, context))
            return {"ok": True, "result": {"tool": tool}}

    facade = Facade()
    app = await build_app(
        LocalBot(), mcp_facade=facade, mcp_token="private-token",
        mcp_capabilities=frozenset({McpCapability.READ}),
    )
    async with test_utils.TestClient(test_utils.TestServer(app)) as client:
        denied = await client.post("/v1/mcp/call", json={"tool": "bridge_health", "arguments": {}})
        assert denied.status == 401
        response = await client.post(
            "/v1/mcp/call",
            headers={"Authorization": "Bearer private-token", "Idempotency-Key": "request-1"},
            json={"tool": "bridge_health", "arguments": {"owner_user_id": "UOTHER"}},
        )
        assert response.status == 200
        assert (await response.json())["result"]["tool"] == "bridge_health"
    _, _, context = facade.calls[0]
    assert context.owner_user_id == "UOWNER"
    assert context.team_id == "T1"
    assert context.request_id == "request-1"
    assert context.capabilities == frozenset({McpCapability.READ})


@pytest.mark.asyncio
async def test_private_mcp_rpc_rejects_bad_shapes_and_large_bodies() -> None:
    class Facade:
        owner_user_id = "UOWNER"
        team_id = "T1"
    app = await build_app(LocalBot(), mcp_facade=Facade(), mcp_token="private-token")
    headers = {"Authorization": "Bearer private-token"}
    async with test_utils.TestClient(test_utils.TestServer(app)) as client:
        malformed = await client.post("/v1/mcp/call", data="{", headers=headers)
        assert malformed.status == 400
        bad_shape = await client.post("/v1/mcp/call", json={"tool": "x", "arguments": []}, headers=headers)
        assert bad_shape.status == 400
        too_large = await client.post("/v1/mcp/call", data="x" * (65 * 1024), headers=headers)
        assert too_large.status == 413


@pytest.mark.asyncio
async def test_legacy_http_endpoints_are_not_present() -> None:
    app = await build_app(LocalBot())
    async with test_utils.TestClient(test_utils.TestServer(app)) as client:
        for path in ("/v1/notify", "/v1/ask", "/v1/hook/event", "/v1/commands"):
            response = await client.post(path, json={})
            assert response.status == 404


@pytest.mark.asyncio
async def test_message_and_interaction_dispatchers_are_resilient() -> None:
    registry = RecordingRegistry()
    dispatch = make_message_dispatcher(registry)
    await dispatch({"text": "hello"})
    assert registry.routed == [{"text": "hello"}]
    failing = make_message_dispatcher(RecordingRegistry(fail=True))
    await failing(object())

    class Dispatcher:
        async def handle_socket_envelope(self, envelope: Any) -> str:
            return "ok"
    interaction = make_interaction_dispatcher(Dispatcher())
    assert await interaction({"envelope_id": "E"}) == "ok"


@pytest.mark.asyncio
async def test_socket_bot_callback_passes_normalized_human_and_bot_messages_to_registry() -> None:
    class NormalizingRegistry:
        def __init__(self) -> None:
            self.raw: list[Any] = []
            self.messages: list[Any] = []

        async def maybe_route_message(self, payload: Any) -> bool:
            self.raw.append(payload)
            self.messages.append(normalize_message(payload))
            return True

    class Dispatcher:
        async def dispatch(self, payload: Any) -> None:
            raise AssertionError(f"message payload reached command dispatcher: {payload!r}")

    registry = NormalizingRegistry()
    callback = make_socket_dispatcher(Dispatcher(), registry)
    # Use the strict fake Web API client so this follows the same Bot startup
    # and Socket Mode callback path without making network requests.
    from tests.fakes import FakeSlackClient
    bot = Bot(
        "xoxb-test", team_id="T1", owner_user_id="UOWNER", home_channel_id="GHOME",
        client=FakeSlackClient(script=[
        ("auth_test", {"ok": True, "team_id": "T1", "user_id": "UBOT"}),
        ("users_info", {"ok": True, "user": {"id": "UOWNER", "name": "owner"}}),
        ("conversations_info", {"ok": True, "channel": {"id": "GHOME", "is_private": True, "is_member": True}}),
        ]),
    )
    bot._on_dispatch_cb = callback
    await bot.start()
    await bot.handle_socket_envelope({
        "envelope_id": "E-human",
        "payload": {"team_id": "T1", "event": {"type": "message", "team": "T1", "channel": "GHOME", "user": "UOWNER", "text": "owner prompt", "ts": "3.1"}},
    })
    await bot.handle_socket_envelope({
        "envelope_id": "E-app",
        "payload": {"team_id": "T1", "event": {"type": "bot_message", "team": "T1", "channel": "GHOME", "bot_id": "BAPP", "username": "helper", "text": "app prompt", "ts": "3.2"}},
    })
    assert [message.text for message in registry.messages] == ["owner prompt", "app prompt"]
    assert registry.messages[0].actor.actor_id == "UOWNER"
    assert not registry.messages[0].actor.is_app
    assert registry.messages[1].actor.actor_id == "BAPP"
    assert registry.messages[1].actor.is_app
    assert registry.raw[0]["actor_id"] == "UOWNER"
    assert registry.raw[1]["actor_id"] == "BAPP"
    assert registry.raw[1] is not registry.raw[1]["event"]
    direct = normalize_message({"team": "T1", "channel": "GHOME", "user": "UOWNER", "ts": "3.3", "text": "direct"})
    assert direct.actor.actor_id == "UOWNER" and not direct.actor.is_app
    await bot.close()


@pytest.mark.asyncio
async def test_socket_dispatcher_routes_native_agent_stop_to_task_registry() -> None:
    class Registry:
        def __init__(self) -> None:
            self.stops: list[dict[str, Any]] = []

        async def handle_agent_session_stopped(self, payload: dict[str, Any]) -> bool:
            self.stops.append(payload)
            return True

    class Dispatcher:
        async def dispatch(self, payload: Any) -> None:
            raise AssertionError("native stop reached command dispatcher")

    registry = Registry()
    dispatch = make_socket_dispatcher(Dispatcher(), registry)
    payload = {"kind": "agent_session_stopped", "team_id": "T1", "channel_id": "C1", "root_ts": "1.0", "actor_id": "UOWNER"}
    assert await dispatch(payload) is True
    assert registry.stops == [payload]


@pytest.mark.asyncio
async def test_socket_dispatcher_absorbs_agent_view_lifecycle_events() -> None:
    class Registry:
        def __init__(self) -> None:
            self.events: list[str] = []

        async def maybe_route_message(self, payload: Any) -> bool:
            raise AssertionError("lifecycle event reached message routing")

        async def handle_agent_session_stopped(self, payload: Any) -> bool:
            raise AssertionError("lifecycle event reached stop handling")

        async def handle_app_home_opened(self, payload: Any) -> bool:
            self.events.append("app_home_opened")
            return True

        async def handle_app_context_changed(self, payload: Any) -> bool:
            self.events.append("app_context_changed")
            return True

    class Dispatcher:
        async def dispatch(self, payload: Any) -> None:
            raise AssertionError("lifecycle event reached command dispatcher")

    registry = Registry()
    dispatch = make_socket_dispatcher(Dispatcher(), registry)
    for kind in ("app_home_opened", "app_context_changed"):
        assert await dispatch({"kind": kind, "team_id": "T1", "actor_id": "UOWNER"}) is True
    assert registry.events == ["app_home_opened", "app_context_changed"]


@pytest.mark.asyncio
async def test_socket_dispatcher_logs_and_contains_malformed_registry_mapping(caplog: pytest.LogCaptureFixture) -> None:
    class StrictRegistry:
        async def maybe_route_message(self, payload: Any) -> bool:
            message = normalize_message(payload)
            if not message.team_id or not message.channel_id or not message.root_ts or not message.actor_id:
                raise ValueError("malformed normalized message")
            return True

    class Dispatcher:
        async def dispatch(self, payload: Any) -> None:
            raise AssertionError("not a command")

    dispatch = make_socket_dispatcher(Dispatcher(), StrictRegistry())
    assert await dispatch({"kind": "message", "team_id": "T1"}) is None
    assert "Slack Socket Mode routing failed" in caplog.text


@pytest.mark.asyncio
async def test_serve_orders_reconcile_before_slack_and_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    stop = asyncio.Event()
    conn = object()

    class Registry:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            order.append("registry")
        def bind_bot(self, bot: Any) -> None:
            order.append("bind")
        async def load_from_db(self, **kwargs: Any) -> None:
            order.append("reconcile")
        async def flush_startup_notices(self) -> None:
            order.append("flush")
        async def start_event_consumers(self) -> None:
            order.append("consumers")
        async def shutdown(self) -> None:
            order.append("registry-close")
    class Bot(LocalBot):
        def __init__(self, token: str, **kwargs: Any) -> None:
            super().__init__()
            order.append("construct")
            self.callback_kwargs = kwargs
        async def start(self) -> None:
            order.append("slack-start")
            stop.set()
        async def close(self) -> None:
            order.append("slack-close")
            stop.set()
    class Runner:
        async def setup(self) -> None:
            order.append("http-setup")
        async def cleanup(self) -> None:
            order.append("http-close")
    class Site:
        async def start(self) -> None:
            order.append("http-start")

    class Secrets:
        bot_token = "xoxb-test"
        app_token = "xapp-test"
        team_id = "T1"
        home_channel_id = "GHOME"
        owner_user_id = "UOWNER"

    async def open_db() -> Any:
        order.append("db-open")
        return conn
    async def close_db(value: Any) -> None:
        assert value is conn
        order.append("db-close")

    monkeypatch.setattr(server_module.state, "open_db", open_db)
    monkeypatch.setattr(server_module.state, "close_db", close_db)
    monkeypatch.setattr(server_module, "TaskRegistry", Registry)
    monkeypatch.setattr(server_module.web, "AppRunner", lambda app: Runner())
    monkeypatch.setattr(server_module.web, "TCPSite", lambda runner, host, port: Site())
    monkeypatch.setattr(server_module.tasks_module, "sweep_old_attachments", lambda: order.append("sweep"))
    monkeypatch.setattr(server_module, "load_projects_from_env", lambda: []) if hasattr(server_module, "load_projects_from_env") else None

    await server_module.serve(Secrets(), bot_factory=Bot, stop_event=stop)
    assert order.index("reconcile") < order.index("construct") < order.index("slack-start")
    assert order.index("slack-start") < order.index("flush") < order.index("consumers")
    assert order[-1] == "db-close"
    assert "sweep" in order
    # bind_bot must be called again after slack-start: bot.bot_user_id/bot_id
    # are only populated by bot.start()'s auth.test call, so a bind_bot()
    # captured before start() only ever sees None for both. Without this
    # second bind, task_registry._bridge_user_id (used by
    # _strip_verified_mention for every mention-stripping call in the
    # process's lifetime) is permanently None.
    bind_indices = [i for i, entry in enumerate(order) if entry == "bind"]
    assert len(bind_indices) == 2
    assert bind_indices[0] < order.index("slack-start") < bind_indices[1]


@pytest.mark.asyncio
async def test_serve_rebinds_bot_after_start_so_identity_is_populated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct regression for the live bug: the registry's bridge identity
    (mirroring TaskRegistry._bridge_user_id) must reflect the bot's real
    identity once known, not the pre-auth.test None captured by the first
    bind_bot() call. A bind_bot() captured too early made
    _strip_verified_mention silently no-op on every message forever, so any
    reply that included the required bot mention (e.g. "yes @Hailey's Robot")
    fed the raw, unstripped mention token into interrogative-answer parsing
    and was misread."""
    stop = asyncio.Event()
    conn = object()
    bind_calls: list[str | None] = []

    class Registry:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._bridge_user_id: str | None = None

        def bind_bot(self, bot: Any) -> None:
            # Mirrors the real TaskRegistry.bind_bot's merge logic exactly.
            self._bridge_user_id = getattr(bot, "bot_user_id", None) or self._bridge_user_id
            bind_calls.append(self._bridge_user_id)

        async def load_from_db(self, **kwargs: Any) -> None:
            return None

        async def flush_startup_notices(self) -> None:
            return None

        async def start_event_consumers(self) -> None:
            return None

        async def shutdown(self) -> None:
            return None

    class Bot(LocalBot):
        def __init__(self, token: str, **kwargs: Any) -> None:
            super().__init__()
            # Not known until start()'s auth.test call, exactly like the real Bot.
            self.bot_user_id = None

        async def start(self) -> None:
            self.bot_user_id = "UBRIDGE"
            stop.set()

        async def close(self) -> None:
            stop.set()

    class Runner:
        async def setup(self) -> None:
            return None

        async def cleanup(self) -> None:
            return None

    class Site:
        async def start(self) -> None:
            return None

    class Secrets:
        bot_token = "xoxb-test"
        app_token = "xapp-test"
        team_id = "T1"
        home_channel_id = "GHOME"
        owner_user_id = "UOWNER"

    async def open_db() -> Any:
        return conn

    async def close_db(value: Any) -> None:
        return None

    monkeypatch.setattr(server_module.state, "open_db", open_db)
    monkeypatch.setattr(server_module.state, "close_db", close_db)
    monkeypatch.setattr(server_module, "TaskRegistry", Registry)
    monkeypatch.setattr(server_module.web, "AppRunner", lambda app: Runner())
    monkeypatch.setattr(server_module.web, "TCPSite", lambda runner, host, port: Site())
    monkeypatch.setattr(server_module.tasks_module, "sweep_old_attachments", lambda: None)

    await server_module.serve(Secrets(), bot_factory=Bot, stop_event=stop)

    # First call (before start()): bot_user_id not yet known -> None. Second
    # call (after start()): the real id must have propagated.
    assert bind_calls == [None, "UBRIDGE"]

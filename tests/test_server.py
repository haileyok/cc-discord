"""Slack server lifecycle and health contract tests (AC.1)."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import test_utils

from bridge.server import build_app, make_interaction_dispatcher, make_message_dispatcher
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

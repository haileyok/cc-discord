"""Tests for the slimmed bridge HTTP server + message dispatcher."""

import json

from aiohttp import test_utils

from bridge.server import build_app, make_message_dispatcher
from tests.fakes import FakeBot


class _RecordingRegistry:
    def __init__(self, *, raise_exc: bool = False) -> None:
        self.routed: list = []
        self._raise = raise_exc

    async def maybe_route_message(self, msg) -> bool:
        self.routed.append(msg)
        if self._raise:
            raise RuntimeError("boom")
        return True


class TestHealth:
    async def test_health_ok(self) -> None:
        bot = FakeBot(is_ready=True, channel_id=999)
        app = await build_app(bot, started_at=0.0)
        async with test_utils.TestClient(test_utils.TestServer(app)) as client:
            resp = await client.get("/v1/health")
            assert resp.status == 200
            data = json.loads(await resp.text())
            assert data["bot_connected"] is True
            assert data["channel_id"] == 999

    async def test_no_legacy_endpoints(self) -> None:
        bot = FakeBot()
        app = await build_app(bot, started_at=0.0)
        async with test_utils.TestClient(test_utils.TestServer(app)) as client:
            for path in ("/v1/notify", "/v1/ask", "/v1/hook/event", "/v1/hook/pretooluse"):
                resp = await client.post(path, json={})
                assert resp.status == 404


class TestDispatcher:
    async def test_routes_to_registry(self) -> None:
        reg = _RecordingRegistry()
        dispatch = make_message_dispatcher(reg)
        await dispatch(object())
        assert len(reg.routed) == 1

    async def test_swallows_errors(self) -> None:
        reg = _RecordingRegistry(raise_exc=True)
        dispatch = make_message_dispatcher(reg)
        # Must not propagate.
        await dispatch(object())
        assert len(reg.routed) == 1

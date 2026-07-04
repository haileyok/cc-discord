"""Tests for the slimmed bridge HTTP server + message dispatcher."""

import asyncio
import json

import pytest
from aiohttp import test_utils

from bridge.daemon_supervisor import SessionInfo
from bridge.server import (
    NOTIFY_TASKS_KEY,
    TASK_REGISTRY_KEY,
    build_app,
    make_message_dispatcher,
)
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
            for path in ("/v1/ask", "/v1/hook/event", "/v1/hook/pretooluse"):
                resp = await client.post(path, json={})
                assert resp.status == 404


class _NotifyRegistry:
    """Minimal registry stub for /v1/notify: session lookup + mention prefix."""

    def __init__(self, *, known_sessions=(), mention="") -> None:
        self._known = set(known_sessions)
        self._mention = mention

    def get_by_session_id(self, session_id: str):
        return object() if session_id in self._known else None

    def notify_mention_prefix(self) -> str:
        return self._mention


class _FakeStateClient:
    def __init__(self, state: dict | None, exc: Exception | None) -> None:
        self._state = state or {}
        self._exc = exc

    async def state(self) -> dict:
        if self._exc is not None:
            raise self._exc
        return self._state

    async def aclose(self) -> None:
        pass


class _FakeSupervisor:
    """find_session/client_for stub for the stop-verification gate.

    ``info="live"`` registers ``sess-x`` on a fake port; ``info=None`` models a
    session that is gone from `polytoken sessions`.
    """

    def __init__(
        self,
        *,
        info: str | None = "live",
        state: dict | None = None,
        list_exc: Exception | None = None,
        state_exc: Exception | None = None,
    ) -> None:
        self._info = (
            SessionInfo(session_id="sess-x", port=1, pid=1, started_at="", project_path="")
            if info == "live"
            else None
        )
        self._state = state
        self._list_exc = list_exc
        self._state_exc = state_exc

    async def find_session(self, session_id: str):
        if self._list_exc is not None:
            raise self._list_exc
        return self._info

    def client_for(self, port: int) -> _FakeStateClient:
        return _FakeStateClient(self._state, self._state_exc)


# A live session that is idle (turn done, nothing pending) → genuinely waiting.
def _idle_supervisor() -> _FakeSupervisor:
    return _FakeSupervisor(state={"turn_in_flight": False})


class TestNotify:
    async def _client(self, bot, registry, supervisor=None):
        app = await build_app(
            bot, started_at=0.0, supervisor=supervisor, stop_verify_delay=0.0
        )
        app[TASK_REGISTRY_KEY] = registry
        return test_utils.TestClient(test_utils.TestServer(app)), app

    @staticmethod
    async def _drain(app) -> None:
        """Wait for scheduled stop-verification tasks to finish."""
        while app[NOTIFY_TASKS_KEY]:
            await asyncio.gather(*list(app[NOTIFY_TASKS_KEY]))

    async def test_notification_posts_with_mention(self) -> None:
        bot = FakeBot()
        reg = _NotifyRegistry(mention="<@111> ")
        client, _app = await self._client(bot, reg)
        async with client:
            resp = await client.post(
                "/v1/notify",
                json={"summary": "job completed"},
                headers={"X-Polytoken-Event": "notification", "X-Polytoken-Session": "sess-x"},
            )
            assert resp.status == 200
            assert "job completed" in bot._post_calls[0]["content"]
            assert bot._post_calls[0]["content"].startswith("<@111> ")

    async def test_stop_for_idle_unknown_session_posts(self) -> None:
        # A session the bridge doesn't drive, verified idle → ping (the point).
        bot = FakeBot()
        reg = _NotifyRegistry(known_sessions=())
        client, app = await self._client(bot, reg, supervisor=_idle_supervisor())
        async with client:
            resp = await client.post(
                "/v1/notify",
                json={},
                headers={"X-Polytoken-Event": "stop", "X-Polytoken-Session": "sess-x"},
            )
            assert resp.status == 200
            assert "scheduled" in await resp.text()
            await self._drain(app)
            assert "waiting for input" in bot._post_calls[0]["content"]

    async def test_stop_turn_in_flight_suppressed(self) -> None:
        # The session continued (queued prompt / goal continuation / awaiting a
        # forwarded tool result) → not waiting for input → no ping.
        bot = FakeBot()
        reg = _NotifyRegistry(known_sessions=())
        sup = _FakeSupervisor(state={"turn_in_flight": True})
        client, app = await self._client(bot, reg, supervisor=sup)
        async with client:
            resp = await client.post(
                "/v1/notify",
                json={},
                headers={"X-Polytoken-Event": "stop", "X-Polytoken-Session": "sess-x"},
            )
            assert resp.status == 200
            assert "scheduled" in await resp.text()
            await self._drain(app)
            assert bot._post_calls == []

    async def test_stop_pending_interrogative_posts(self) -> None:
        # Blocked on a question: the turn is technically in flight, but the
        # session IS waiting for the user → ping.
        bot = FakeBot()
        reg = _NotifyRegistry(known_sessions=())
        sup = _FakeSupervisor(
            state={"turn_in_flight": True, "pending_interrogatives": [{"interrogative_id": "i1"}]}
        )
        client, app = await self._client(bot, reg, supervisor=sup)
        async with client:
            resp = await client.post(
                "/v1/notify",
                json={},
                headers={"X-Polytoken-Event": "stop", "X-Polytoken-Session": "sess-x"},
            )
            assert resp.status == 200
            await self._drain(app)
            assert len(bot._post_calls) == 1

    async def test_stop_session_gone_suppressed(self) -> None:
        # The session left the registry (exec finished, TUI quit) — nothing can
        # receive input → no ping.
        bot = FakeBot()
        reg = _NotifyRegistry(known_sessions=())
        client, app = await self._client(bot, reg, supervisor=_FakeSupervisor(info=None))
        async with client:
            resp = await client.post(
                "/v1/notify",
                json={},
                headers={"X-Polytoken-Event": "stop", "X-Polytoken-Session": "sess-x"},
            )
            assert resp.status == 200
            await self._drain(app)
            assert bot._post_calls == []

    async def test_stop_registry_error_fails_open(self) -> None:
        bot = FakeBot()
        reg = _NotifyRegistry(known_sessions=())
        sup = _FakeSupervisor(list_exc=RuntimeError("sessions listing failed"))
        client, app = await self._client(bot, reg, supervisor=sup)
        async with client:
            resp = await client.post(
                "/v1/notify",
                json={},
                headers={"X-Polytoken-Event": "stop", "X-Polytoken-Session": "sess-x"},
            )
            assert resp.status == 200
            await self._drain(app)
            assert len(bot._post_calls) == 1

    async def test_stop_state_error_fails_open(self) -> None:
        bot = FakeBot()
        reg = _NotifyRegistry(known_sessions=())
        sup = _FakeSupervisor(state_exc=RuntimeError("connection refused"))
        client, app = await self._client(bot, reg, supervisor=sup)
        async with client:
            resp = await client.post(
                "/v1/notify",
                json={},
                headers={"X-Polytoken-Event": "stop", "X-Polytoken-Session": "sess-x"},
            )
            assert resp.status == 200
            await self._drain(app)
            assert len(bot._post_calls) == 1

    async def test_stop_without_supervisor_fails_open(self) -> None:
        bot = FakeBot()
        reg = _NotifyRegistry(known_sessions=())
        client, app = await self._client(bot, reg, supervisor=None)
        async with client:
            resp = await client.post(
                "/v1/notify",
                json={},
                headers={"X-Polytoken-Event": "stop", "X-Polytoken-Session": "sess-x"},
            )
            assert resp.status == 200
            await self._drain(app)
            assert len(bot._post_calls) == 1

    async def test_stop_for_bridge_session_suppressed(self) -> None:
        # A session the bridge already renders inline → suppress (no double ping).
        bot = FakeBot()
        reg = _NotifyRegistry(known_sessions=("sess-bridge",))
        client, _app = await self._client(bot, reg, supervisor=_idle_supervisor())
        async with client:
            resp = await client.post(
                "/v1/notify",
                json={},
                headers={"X-Polytoken-Event": "stop", "X-Polytoken-Session": "sess-bridge"},
            )
            assert resp.status == 200
            assert "suppressed" in await resp.text()
            assert bot._post_calls == []

    async def test_notification_for_bridge_session_suppressed(self) -> None:
        # A notification for a bridge-driven session is rendered inline (as an
        # AttentionPing in its thread), so the channel hook ping is suppressed too.
        bot = FakeBot()
        reg = _NotifyRegistry(known_sessions=("sess-bridge",))
        client, _app = await self._client(bot, reg)
        async with client:
            resp = await client.post(
                "/v1/notify",
                json={"summary": "job completed"},
                headers={"X-Polytoken-Event": "notification", "X-Polytoken-Session": "sess-bridge"},
            )
            assert resp.status == 200
            assert "suppressed" in await resp.text()
            assert bot._post_calls == []

    async def test_non_interactive_stop_suppressed(self) -> None:
        # A stop event for a non-interactive session → suppress (no ping).
        bot = FakeBot()
        reg = _NotifyRegistry(known_sessions=())
        client, _app = await self._client(bot, reg, supervisor=_idle_supervisor())
        async with client:
            resp = await client.post(
                "/v1/notify",
                json={},
                headers={
                    "X-Polytoken-Event": "stop",
                    "X-Polytoken-Session": "sess-sub",
                    "X-Polytoken-Non-Interactive": "true",
                },
            )
            assert resp.status == 200
            assert "suppressed_non_interactive" in await resp.text()
            assert bot._post_calls == []

    async def test_non_interactive_notification_suppressed(self) -> None:
        # A notification event for a non-interactive session → suppress.
        bot = FakeBot()
        reg = _NotifyRegistry(known_sessions=())
        client, _app = await self._client(bot, reg)
        async with client:
            resp = await client.post(
                "/v1/notify",
                json={"summary": "job completed"},
                headers={
                    "X-Polytoken-Event": "notification",
                    "X-Polytoken-Session": "sess-sub",
                    "X-Polytoken-Non-Interactive": "true",
                },
            )
            assert resp.status == 200
            assert "suppressed_non_interactive" in await resp.text()
            assert bot._post_calls == []

    @pytest.mark.parametrize(
        "header_value,should_suppress",
        [
            # Truthy → suppressed
            ("true", True),
            ("1", True),
            ("yes", True),
            ("True", True),
            ("TRUE", True),
            # Falsy → verified idle → posts
            ("false", False),
            ("0", False),
            ("no", False),
            ("off", False),
            ("", False),
        ],
    )
    async def test_non_interactive_values(self, header_value: str, should_suppress: bool) -> None:
        bot = FakeBot()
        reg = _NotifyRegistry(mention="<@111> ")
        client, app = await self._client(bot, reg, supervisor=_idle_supervisor())
        headers = {
            "X-Polytoken-Event": "stop",
            "X-Polytoken-Session": "sess-x",
        }
        if header_value is not None:
            headers["X-Polytoken-Non-Interactive"] = header_value
        async with client:
            resp = await client.post("/v1/notify", json={}, headers=headers)
            assert resp.status == 200
            if should_suppress:
                assert "suppressed_non_interactive" in await resp.text()
                assert bot._post_calls == []
            else:
                assert "scheduled" in await resp.text()
                await self._drain(app)
                assert len(bot._post_calls) == 1

    async def test_non_interactive_missing_header_posts(self) -> None:
        # No X-Polytoken-Non-Interactive header at all → verified idle → posts.
        bot = FakeBot()
        reg = _NotifyRegistry(mention="<@111> ")
        client, app = await self._client(bot, reg, supervisor=_idle_supervisor())
        async with client:
            resp = await client.post(
                "/v1/notify",
                json={},
                headers={"X-Polytoken-Event": "stop", "X-Polytoken-Session": "sess-x"},
            )
            assert resp.status == 200
            assert "scheduled" in await resp.text()
            await self._drain(app)
            assert len(bot._post_calls) == 1


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

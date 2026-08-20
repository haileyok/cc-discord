"""Tests for bridge.daemon_supervisor with an injected CLI runner."""

import pytest

from bridge.daemon_supervisor import (
    DaemonSupervisor,
    DaemonSupervisorError,
    SessionInfo,
)
from bridge.polytoken_client import PolytokenClientError


def make_runner(responses: dict[tuple[str, ...], tuple[int, str, str]]):
    """Build a runner keyed by a recognizable argv suffix tuple."""
    calls: list[list[str]] = []

    async def runner(argv: list[str]) -> tuple[int, str, str]:
        calls.append(argv)
        for key, resp in responses.items():
            if tuple(argv[-len(key):]) == key:
                return resp
        return (127, "", f"unexpected argv: {argv}")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


_SESSIONS_OUT = """SESSION_ID                  PORT    PID      STARTED_AT               PROJECT_PATH
03hess-salt                 41515   3701047  2026-06-15T00:45:45Z     /home/slack
03hqrb-life                 34017   4074715  2026-06-15T03:18:35Z     /tmp/pt probe with spaces
"""


_NO_FAIL = object()


class FakeClient:
    def __init__(self, *, fail_status=_NO_FAIL):
        self.terminated = False
        self.closed = False
        self._fail_status = fail_status

    async def terminate(self):
        if self._fail_status is not _NO_FAIL:
            raise PolytokenClientError("boom", status=self._fail_status)
        self.terminated = True

    async def aclose(self):
        self.closed = True


class TestSpawn:
    async def test_spawn_parses_id_and_port(self) -> None:
        runner = make_runner(
            {("new", "--no-attach"): (0, "session_id=03abc-foo port=34521\n", "")}
        )
        sup = DaemonSupervisor(runner=runner)
        res = await sup.spawn("/work/dir")
        assert res.session_id == "03abc-foo"
        assert res.port == 34521
        # working-dir and the subcommand are present.
        argv = runner.calls[0]  # type: ignore[attr-defined]
        assert "--working-dir" in argv and "/work/dir" in argv
        assert argv[-2:] == ["new", "--no-attach"]

    async def test_spawn_includes_config_dir(self) -> None:
        runner = make_runner(
            {("new", "--no-attach"): (0, "session_id=s port=1\n", "")}
        )
        sup = DaemonSupervisor(runner=runner)
        await sup.spawn("/w", config_dir="/cfg")
        argv = runner.calls[0]  # type: ignore[attr-defined]
        assert "--config-dir" in argv and "/cfg" in argv

    async def test_spawn_nonzero_exit_raises(self) -> None:
        runner = make_runner({("new", "--no-attach"): (1, "", "boom")})
        sup = DaemonSupervisor(runner=runner)
        with pytest.raises(DaemonSupervisorError):
            await sup.spawn("/w")

    async def test_spawn_unparseable_raises(self) -> None:
        runner = make_runner({("new", "--no-attach"): (0, "nope\n", "")})
        sup = DaemonSupervisor(runner=runner)
        with pytest.raises(DaemonSupervisorError):
            await sup.spawn("/w")


class TestListSessions:
    async def test_parses_table_with_spaces_in_path(self) -> None:
        runner = make_runner({("sessions",): (0, _SESSIONS_OUT, "")})
        sup = DaemonSupervisor(runner=runner)
        rows = await sup.list_sessions()
        assert len(rows) == 2
        assert rows[0] == SessionInfo(
            "03hess-salt", 41515, 3701047, "2026-06-15T00:45:45Z", "/home/slack"
        )
        assert rows[1].project_path == "/tmp/pt probe with spaces"

    async def test_nonzero_raises(self) -> None:
        runner = make_runner({("sessions",): (2, "", "err")})
        sup = DaemonSupervisor(runner=runner)
        with pytest.raises(DaemonSupervisorError):
            await sup.list_sessions()

    async def test_find_session(self) -> None:
        runner = make_runner({("sessions",): (0, _SESSIONS_OUT, "")})
        sup = DaemonSupervisor(runner=runner)
        info = await sup.find_session("03hqrb-life")
        assert info is not None and info.port == 34017
        assert await sup.find_session("missing") is None


_MODELS_OUT = """default_model: anthropic/claude-opus-4-8
default_small_model: anthropic/claude-haiku-4-5

models:
- anthropic/claude-fable-5
  provider: anthropic/claude-fable-5
  reasoning: effort set=...
- anthropic/claude-opus-4-8 (default)
  provider: anthropic/claude-opus-4-8
- anthropic/claude-haiku-4-5 (small)
  reasoning: none
"""


class TestListModels:
    async def test_parses_model_names(self) -> None:
        runner = make_runner({("models",): (0, _MODELS_OUT, "")})
        sup = DaemonSupervisor(runner=runner)
        models = await sup.list_models()
        assert models == [
            "anthropic/claude-fable-5",
            "anthropic/claude-opus-4-8",
            "anthropic/claude-haiku-4-5",
        ]

    async def test_nonzero_raises(self) -> None:
        runner = make_runner({("models",): (3, "", "err")})
        sup = DaemonSupervisor(runner=runner)
        with pytest.raises(DaemonSupervisorError):
            await sup.list_models()


class TestTerminate:
    async def test_terminate_live_session(self) -> None:
        runner = make_runner({("sessions",): (0, _SESSIONS_OUT, "")})
        fake = FakeClient()
        sup = DaemonSupervisor(runner=runner, client_factory=lambda port: fake)
        ok = await sup.terminate("03hess-salt")
        assert ok is True
        assert fake.terminated and fake.closed

    async def test_terminate_unknown_session(self) -> None:
        runner = make_runner({("sessions",): (0, _SESSIONS_OUT, "")})
        sup = DaemonSupervisor(runner=runner, client_factory=lambda port: FakeClient())
        assert await sup.terminate("nope") is False

    async def test_terminate_dead_daemon_returns_false(self) -> None:
        runner = make_runner({("sessions",): (0, _SESSIONS_OUT, "")})
        fake = FakeClient(fail_status=None)  # transport error -> status None
        sup = DaemonSupervisor(runner=runner, client_factory=lambda port: fake)
        assert await sup.terminate("03hess-salt") is False
        assert fake.closed

    async def test_terminate_http_error_propagates(self) -> None:
        runner = make_runner({("sessions",): (0, _SESSIONS_OUT, "")})
        fake = FakeClient(fail_status=500)
        sup = DaemonSupervisor(runner=runner, client_factory=lambda port: fake)
        with pytest.raises(PolytokenClientError):
            await sup.terminate("03hess-salt")
        assert fake.closed

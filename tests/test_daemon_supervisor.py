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
03hess-salt                 41515   3701047  2026-06-15T00:45:45Z     /home/discord
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
            "03hess-salt", 41515, 3701047, "2026-06-15T00:45:45Z", "/home/discord"
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


class TestResume:
    @staticmethod
    def _sessions_row(sid: str, port: int) -> str:
        return f"{sid:<48} {port:<7} 100      2026-06-28T00:00:00Z     /w\n"

    async def test_resume_discovers_port(self) -> None:
        registered = {"yes": False}

        async def runner(argv: list[str]) -> tuple[int, str, str]:
            if argv[-1] == "sessions":
                out = "SESSION_ID PORT PID STARTED_AT PROJECT_PATH\n"
                if registered["yes"]:
                    out += self._sessions_row("04abc-resume", 41000)
                out += self._sessions_row("03other-x", 41001)
                return (0, out, "")
            return (127, "", "nope")

        launched: list[list[str]] = []

        def launcher(argv: list[str]) -> int:
            launched.append(argv)
            registered["yes"] = True  # daemon registers after launch
            return 99999

        sup = DaemonSupervisor(runner=runner, launcher=launcher)
        res = await sup.resume("04abc-resume", "/w", discover_timeout=5)
        assert res.session_id == "04abc-resume"
        assert res.port == 41000
        assert launched  # launcher fired
        # The resume argv is well-formed.
        av = launched[0]
        assert "--resume" in av and "--session-id" in av
        assert "--global-config-dir" in av and "--listen" in av
        assert "127.0.0.1:0" in av

    async def test_resume_already_running_is_idempotent(self) -> None:
        # A live session is returned without relaunching.
        async def runner(argv: list[str]) -> tuple[int, str, str]:
            out = "SESSION_ID PORT PID STARTED_AT PROJECT_PATH\n"
            out += self._sessions_row("04abc-resume", 41200)
            return (0, out, "")

        launched: list[list[str]] = []

        def launcher(argv: list[str]) -> int:
            launched.append(argv)
            return 1

        sup = DaemonSupervisor(runner=runner, launcher=launcher)
        res = await sup.resume("04abc-resume", "/w")
        assert res.port == 41200
        assert not launched  # no relaunch

    async def test_resume_launch_failure_raises(self) -> None:
        async def runner(argv: list[str]) -> tuple[int, str, str]:
            return (0, "SESSION_ID PORT PID STARTED_AT PROJECT_PATH\n", "")

        def launcher(argv: list[str]) -> None:
            return None  # launch failed

        sup = DaemonSupervisor(runner=runner, launcher=launcher)
        with pytest.raises(DaemonSupervisorError):
            await sup.resume("04abc-resume", "/w", discover_timeout=2)

    async def test_resume_discover_timeout_raises(self) -> None:
        # The resumed daemon never registers.
        async def runner(argv: list[str]) -> tuple[int, str, str]:
            return (0, "SESSION_ID PORT PID STARTED_AT PROJECT_PATH\n", "")

        def launcher(argv: list[str]) -> int:
            return 99999

        sup = DaemonSupervisor(runner=runner, launcher=launcher)
        with pytest.raises(DaemonSupervisorError, match="did not register"):
            await sup.resume("04abc-resume", "/w", discover_timeout=0.6)


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

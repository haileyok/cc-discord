"""Tests for the headless phantom zellij client + its daemon supervisor (F5).

Brandon's WSL2 dogfood found a spawned session binds but never renders until a
human attaches a terminal: with no client attached, zellij drops
`action write-chars` because the TUI blocks on terminal queries nobody answers.
The phantom client attaches a sized, headless PTY client so the session renders
and receives keystrokes. `serve` supervises it by default (BRIDGE_PHANTOM=0 to
opt out).

These tests cover config resolution, the enable/command helpers, and the
supervisor's spawn / restart / terminate-on-shutdown behaviour (via a fake
subprocess — no real zellij or PTY).
"""

import asyncio
import contextlib
import sys

import bridge.server as server
from bridge.phantom import DEFAULT_COLUMNS, DEFAULT_LINES, PhantomConfig, _phantom_config


class TestPhantomConfig:
    def test_defaults(self):
        assert _phantom_config({}) == PhantomConfig(session="meow", cols=200, rows=60)

    def test_overrides(self):
        cfg = _phantom_config(
            {"BRIDGE_ZELLIJ_SESSION": "work", "PHANTOM_COLUMNS": "120", "PHANTOM_LINES": "40"}
        )
        assert cfg == PhantomConfig(session="work", cols=120, rows=40)

    def test_bad_int_falls_back_to_default(self):
        cfg = _phantom_config({"PHANTOM_COLUMNS": "wide", "PHANTOM_LINES": ""})
        assert cfg.cols == DEFAULT_COLUMNS
        assert cfg.rows == DEFAULT_LINES


class TestPhantomGating:
    def test_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("BRIDGE_PHANTOM", raising=False)
        assert server._phantom_enabled() is True

    def test_disabled_with_zero(self, monkeypatch):
        monkeypatch.setenv("BRIDGE_PHANTOM", "0")
        assert server._phantom_enabled() is False

    def test_enabled_with_explicit_one(self, monkeypatch):
        monkeypatch.setenv("BRIDGE_PHANTOM", "1")
        assert server._phantom_enabled() is True

    def test_command_runs_package_module(self):
        # Must be `python -m bridge.phantom` so it works under uv tool install
        # (the wheel ships src/bridge, not scripts/).
        assert server._phantom_command() == [sys.executable, "-m", "bridge.phantom"]


class _FakeProc:
    """Minimal stand-in for asyncio.subprocess.Process."""

    def __init__(self, pid: int = 4242):
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False
        self._exit = asyncio.Event()

    async def wait(self) -> int:
        await self._exit.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._exit.set()

    def finish(self, rc: int = 0) -> None:
        self.returncode = rc
        self._exit.set()


async def _await_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not met within timeout")


class TestPhantomSupervisor:
    async def test_spawns_expected_command(self, monkeypatch):
        calls: list[tuple] = []
        proc = _FakeProc()

        async def fake_exec(*cmd, **kwargs):
            calls.append((cmd, kwargs))
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        task = asyncio.create_task(server._phantom_supervisor())
        try:
            await _await_until(lambda: bool(calls))
            cmd, kwargs = calls[0]
            assert list(cmd) == [sys.executable, "-m", "bridge.phantom"]
            assert kwargs["stdin"] == asyncio.subprocess.DEVNULL
            assert kwargs["stdout"] == asyncio.subprocess.DEVNULL
            assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_terminates_child_on_cancel(self, monkeypatch):
        proc = _FakeProc()

        async def fake_exec(*cmd, **kwargs):
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        task = asyncio.create_task(server._phantom_supervisor())
        await _await_until(lambda: proc.returncode is None and proc.pid == 4242)
        # give the supervisor a tick to reach `await proc.wait()`
        await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert proc.terminated is True

    async def test_restarts_after_clean_exit(self, monkeypatch):
        procs: list[_FakeProc] = []

        async def fake_exec(*cmd, **kwargs):
            p = _FakeProc(pid=1000 + len(procs))
            procs.append(p)
            return p

        # Neutralize the restart backoff so the test doesn't wait real seconds.
        real_sleep = asyncio.sleep

        async def fast_sleep(_secs):
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(asyncio, "sleep", fast_sleep)

        task = asyncio.create_task(server._phantom_supervisor())
        try:
            await _await_until(lambda: len(procs) >= 1)
            procs[0].finish(rc=0)  # clean exit -> supervisor should respawn
            await _await_until(lambda: len(procs) >= 2)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

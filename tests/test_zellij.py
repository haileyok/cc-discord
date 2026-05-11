"""Tests for ZellijManager (tab-name-based addressing on zellij ≥ 0.43)."""

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from bridge import zellij as zellij_module
from bridge.zellij import (
    ZellijError,
    ZellijManager,
    ZellijSessionMissing,
    ZellijSpawnError,
)


@dataclass
class FakeProc:
    """Minimal fake subprocess for testing."""

    returncode: int
    stdout_data: bytes
    stderr_data: bytes

    async def wait(self) -> int:
        return self.returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return (self.stdout_data, self.stderr_data)


@pytest.fixture
def patch_exec(monkeypatch):
    """Monkeypatch asyncio.create_subprocess_exec with a FakeProc queue."""
    queue: list[FakeProc] = []
    call_log: list[tuple] = []

    async def _create_subprocess_exec(*argv: str, **kwargs: Any) -> FakeProc:
        call_log.append((argv, kwargs))
        if not queue:
            raise AssertionError(f"Unexpected zellij call: {argv}")
        return queue.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_subprocess_exec)
    _create_subprocess_exec._queue = queue  # type: ignore
    _create_subprocess_exec._call_log = call_log  # type: ignore
    return _create_subprocess_exec


@pytest.fixture(autouse=True)
def fixed_session_name(monkeypatch):
    """Pin SESSION_NAME to 'bridge' and clear ZELLIJ_SESSION_NAME so tests don't
    accidentally trigger the colocated-session shortcut when run from a shell
    that's itself inside zellij."""
    monkeypatch.setattr(zellij_module, "SESSION_NAME", "bridge")
    monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)


@pytest.mark.asyncio
class TestZellijManager:
    async def test_ensure_session_alive_success(self, patch_exec):
        patch_exec._queue.append(FakeProc(returncode=0, stdout_data=b"", stderr_data=b""))
        mgr = ZellijManager()
        await mgr.ensure_session_alive()
        argv, _ = patch_exec._call_log[0]
        assert argv == ("zellij", "attach", "--create-background", "bridge")

    async def test_ensure_session_alive_idempotent(self, patch_exec):
        patch_exec._queue.append(FakeProc(returncode=0, stdout_data=b"", stderr_data=b""))
        patch_exec._queue.append(FakeProc(returncode=0, stdout_data=b"", stderr_data=b""))
        mgr = ZellijManager()
        await mgr.ensure_session_alive()
        await mgr.ensure_session_alive()
        assert len(patch_exec._call_log) == 2

    async def test_ensure_session_alive_failure(self, patch_exec):
        patch_exec._queue.append(
            FakeProc(returncode=1, stdout_data=b"", stderr_data=b"permission denied")
        )
        mgr = ZellijManager()
        with pytest.raises(ZellijSessionMissing, match="permission denied"):
            await mgr.ensure_session_alive()

    async def test_ensure_session_alive_tolerates_already_exists(self, patch_exec):
        """zellij ≥ 0.43 returns non-zero with 'Session already exists' on second attach."""
        patch_exec._queue.append(
            FakeProc(returncode=2, stdout_data=b"", stderr_data=b"Session already exists")
        )
        mgr = ZellijManager()
        await mgr.ensure_session_alive()  # should not raise

    async def test_ensure_session_alive_skips_attach_when_colocated(
        self, patch_exec, monkeypatch
    ):
        """When ZELLIJ_SESSION_NAME matches the target session, skip attach
        (zellij panics on self-attach in that case)."""
        monkeypatch.setenv("ZELLIJ_SESSION_NAME", "bridge")
        mgr = ZellijManager()
        await mgr.ensure_session_alive()
        assert patch_exec._call_log == []

    async def test_list_panes_filters_cc_prefix(self, patch_exec):
        """list_panes runs list-panes --json and returns only cc- prefixed tabs."""
        import json
        panes_data = [
            {"tab_name": "Tab #1", "exited": False},
            {"tab_name": "cc-aa429dc4", "exited": False},
            {"tab_name": "Tab #2", "exited": False},
            {"tab_name": "cc-deadbeef", "exited": True},
        ]
        patch_exec._queue.append(
            FakeProc(
                returncode=0,
                stdout_data=json.dumps(panes_data).encode(),
                stderr_data=b"",
            )
        )
        mgr = ZellijManager()
        panes = await mgr.list_panes()
        assert panes == [
            {"id": "cc-aa429dc4", "exited": False},
            {"id": "cc-deadbeef", "exited": True},
        ]
        argv, _ = patch_exec._call_log[0]
        assert argv == ("zellij", "--session", "bridge", "action", "list-panes", "--json", "--state", "--tab")

    async def test_list_panes_failure(self, patch_exec):
        """list_panes falls back to query-tab-names when list-panes fails, then fails if that also fails."""
        # First call: list-panes fails
        patch_exec._queue.append(
            FakeProc(returncode=1, stdout_data=b"", stderr_data=b"session not found")
        )
        # Second call: query-tab-names also fails
        patch_exec._queue.append(
            FakeProc(returncode=1, stdout_data=b"", stderr_data=b"session not found")
        )
        mgr = ZellijManager()
        with pytest.raises(ZellijError, match="query-tab-names failed"):
            await mgr.list_panes()

    async def test_write_to_pane_single_line(self, patch_exec):
        """No newline → focus tab + one write-chars, no Enter."""
        patch_exec._queue.append(FakeProc(returncode=0, stdout_data=b"", stderr_data=b""))  # go-to-tab-name
        patch_exec._queue.append(FakeProc(returncode=0, stdout_data=b"", stderr_data=b""))  # write-chars

        mgr = ZellijManager()
        await mgr.write_to_pane("cc-aa429dc4", "hello")

        assert len(patch_exec._call_log) == 2
        assert patch_exec._call_log[0][0] == (
            "zellij", "--session", "bridge", "action", "go-to-tab-name", "cc-aa429dc4"
        )
        assert patch_exec._call_log[1][0] == (
            "zellij", "--session", "bridge", "action", "write-chars", "hello"
        )

    async def test_write_to_pane_with_newline(self, patch_exec):
        """Trailing newline → focus + write-chars + write 13 (Enter)."""
        for _ in range(3):
            patch_exec._queue.append(FakeProc(returncode=0, stdout_data=b"", stderr_data=b""))

        mgr = ZellijManager()
        await mgr.write_to_pane("cc-aa429dc4", "hello\n")

        assert len(patch_exec._call_log) == 3
        assert patch_exec._call_log[0][0][4:] == ("go-to-tab-name", "cc-aa429dc4")
        assert patch_exec._call_log[1][0][4:] == ("write-chars", "hello")
        assert patch_exec._call_log[2][0][4:] == ("write", "13")

    async def test_write_to_pane_multiple_lines(self, patch_exec):
        """Multi-line input: focus tab + a single `action write` carrying
        bracketed-paste-begin + UTF-8 body + bracketed-paste-end + CR.
        Single dispatch avoids races between zellij's per-call delivery
        and Claude's TUI paste-mode state."""
        for _ in range(2):
            patch_exec._queue.append(FakeProc(returncode=0, stdout_data=b"", stderr_data=b""))

        mgr = ZellijManager()
        await mgr.write_to_pane("cc-aa429dc4", "hello\nworld\n")

        assert len(patch_exec._call_log) == 2
        assert patch_exec._call_log[0][0][4:] == ("go-to-tab-name", "cc-aa429dc4")
        # ESC[200~ + "hello\nworld" + ESC[201~ + CR
        expected_bytes = (
            (27, 91, 50, 48, 48, 126)  # ESC[200~
            + tuple("hello\nworld".encode("utf-8"))
            + (27, 91, 50, 48, 49, 126)  # ESC[201~
            + (13,)  # CR
        )
        assert patch_exec._call_log[1][0][4:] == ("write",) + tuple(str(b) for b in expected_bytes)

    async def test_write_to_pane_multi_line_no_trailing_newline(self, patch_exec):
        """Multi-line without trailing newline: paste-wrapped body, no CR."""
        for _ in range(2):
            patch_exec._queue.append(FakeProc(returncode=0, stdout_data=b"", stderr_data=b""))

        mgr = ZellijManager()
        await mgr.write_to_pane("cc-aa429dc4", "hello\nworld")

        assert len(patch_exec._call_log) == 2
        assert patch_exec._call_log[0][0][4:] == ("go-to-tab-name", "cc-aa429dc4")
        expected_bytes = (
            (27, 91, 50, 48, 48, 126)
            + tuple("hello\nworld".encode("utf-8"))
            + (27, 91, 50, 48, 49, 126)
        )
        assert patch_exec._call_log[1][0][4:] == ("write",) + tuple(str(b) for b in expected_bytes)

    async def test_write_to_pane_focus_failure_raises(self, patch_exec):
        patch_exec._queue.append(
            FakeProc(returncode=1, stdout_data=b"", stderr_data=b"no such tab")
        )
        mgr = ZellijManager()
        with pytest.raises(ZellijError, match="go-to-tab-name"):
            await mgr.write_to_pane("cc-missing", "hello")

    async def test_close_pane_success(self, patch_exec):
        """close_pane focuses the tab then issues close-tab."""
        patch_exec._queue.append(FakeProc(returncode=0, stdout_data=b"", stderr_data=b""))  # go-to-tab-name
        patch_exec._queue.append(FakeProc(returncode=0, stdout_data=b"", stderr_data=b""))  # close-tab

        mgr = ZellijManager()
        await mgr.close_pane("cc-aa429dc4")

        assert len(patch_exec._call_log) == 2
        assert patch_exec._call_log[0][0][4:] == ("go-to-tab-name", "cc-aa429dc4")
        assert patch_exec._call_log[1][0][4:] == ("close-tab",)

    async def test_close_pane_idempotent_when_tab_missing(self, patch_exec):
        """close_pane is silent when the tab is already gone."""
        patch_exec._queue.append(
            FakeProc(returncode=1, stdout_data=b"", stderr_data=b"no such tab")
        )
        mgr = ZellijManager()
        await mgr.close_pane("cc-aa429dc4")  # should not raise; only one call (no close-tab issued)
        assert len(patch_exec._call_log) == 1

    async def test_spawn_task_success(self, patch_exec):
        """Two subprocess calls: attach + new-tab --layout. Returns the tab name.

        spawn_task no longer separately invokes `run` — the layout file
        passed to `new-tab --layout` describes the single claude pane,
        which avoids the default shell pane that `new-tab` would
        otherwise spawn alongside.
        """
        for _ in range(2):
            patch_exec._queue.append(FakeProc(returncode=0, stdout_data=b"", stderr_data=b""))

        mgr = ZellijManager()
        tab = await mgr.spawn_task("/tmp", "cc-aa429dc4", "/tmp/layout.kdl")

        assert tab == "cc-aa429dc4"
        assert len(patch_exec._call_log) == 2
        assert patch_exec._call_log[0][0] == (
            "zellij", "attach", "--create-background", "bridge"
        )
        assert patch_exec._call_log[1][0] == (
            "zellij", "--session", "bridge", "action", "new-tab",
            "--name", "cc-aa429dc4", "--cwd", "/tmp",
            "--layout", "/tmp/layout.kdl",
        )

    async def test_spawn_task_session_already_exists_is_success(self, patch_exec):
        """attach returning 'Session already exists' is treated as success."""
        patch_exec._queue.append(
            FakeProc(returncode=2, stdout_data=b"", stderr_data=b"Session already exists")
        )
        patch_exec._queue.append(FakeProc(returncode=0, stdout_data=b"", stderr_data=b""))

        mgr = ZellijManager()
        tab = await mgr.spawn_task("/tmp", "cc-aa429dc4", "/tmp/layout.kdl")
        assert tab == "cc-aa429dc4"

    async def test_spawn_task_skips_attach_when_colocated(self, patch_exec, monkeypatch):
        """When running inside the target session, spawn_task skips the attach
        call (only the new-tab call remains)."""
        monkeypatch.setenv("ZELLIJ_SESSION_NAME", "bridge")
        patch_exec._queue.append(FakeProc(returncode=0, stdout_data=b"", stderr_data=b""))

        mgr = ZellijManager()
        tab = await mgr.spawn_task("/tmp", "cc-aa429dc4", "/tmp/layout.kdl")

        assert tab == "cc-aa429dc4"
        assert len(patch_exec._call_log) == 1
        assert patch_exec._call_log[0][0][4] == "new-tab"

    async def test_spawn_task_new_tab_failure(self, patch_exec):
        patch_exec._queue.append(FakeProc(returncode=0, stdout_data=b"", stderr_data=b""))  # attach
        patch_exec._queue.append(
            FakeProc(returncode=1, stdout_data=b"", stderr_data=b"new-tab refused")
        )
        mgr = ZellijManager()
        with pytest.raises(ZellijSpawnError, match="new-tab"):
            await mgr.spawn_task("/tmp", "cc-aa429dc4", "/tmp/layout.kdl")

    async def test_spawn_task_concurrent_serialized(self, patch_exec):
        """Concurrent spawn_task calls serialize on the session lock; each sees its own
        attach + new-tab sequence ungaroupbled."""
        # 2 calls per spawn × 2 spawns = 4 total
        for _ in range(4):
            patch_exec._queue.append(FakeProc(returncode=0, stdout_data=b"", stderr_data=b""))

        mgr = ZellijManager()
        tab1, tab2 = await asyncio.gather(
            mgr.spawn_task("/tmp", "cc-spawn1", "/tmp/l1.kdl"),
            mgr.spawn_task("/home", "cc-spawn2", "/tmp/l2.kdl"),
        )
        assert {tab1, tab2} == {"cc-spawn1", "cc-spawn2"}

        log = patch_exec._call_log
        for i, (argv, _) in enumerate(log):
            if "new-tab" in argv:
                # Each new-tab should be preceded by an attach within the
                # same lock window.
                prev_argv = log[i - 1][0]
                assert prev_argv[1] == "attach"
                tab_name = argv[argv.index("--name") + 1]
                assert tab_name in {"cc-spawn1", "cc-spawn2"}

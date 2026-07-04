"""Supervise per-task Polytoken daemon processes via the ``polytoken`` CLI.

The bridge runs one daemon per Discord task. This module owns the process
side of that: spawning a fresh headless session, listing the live session
registry, and terminating a session. The HTTP side of a session lives in
:class:`bridge.polytoken_client.PolytokenClient`.

The subprocess runner and the client factory are injectable so the supervisor
is unit-testable without a real ``polytoken`` binary or daemon.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from bridge.polytoken_client import PolytokenClient, PolytokenClientError

log = logging.getLogger(__name__)

# `polytoken new --no-attach` prints one line: `session_id=<id> port=<port>`.
_SPAWN_RE = re.compile(r"session_id=(?P<sid>\S+)\s+port=(?P<port>\d+)")

# The daemon's global config directory. `polytoken new` auto-discovers this via
# XDG, but `polytoken daemon --resume` does NOT — it must be passed explicitly
# with `--global-config-dir`, or it fails with "no config file found".
DEFAULT_GLOBAL_CONFIG_DIR = str(Path.home() / ".config" / "polytoken")

# How long to wait for a resumed daemon to register in `polytoken sessions`.
_RESUME_DISCOVER_TIMEOUT_SECS = 20.0
_RESUME_POLL_INTERVAL_SECS = 0.5

# Subprocess runner contract: (argv) -> (returncode, stdout, stderr).
Runner = Callable[[list[str]], Awaitable[tuple[int, str, str]]]
ClientFactory = Callable[[int], PolytokenClient]
# Background launcher contract: (argv) -> Popen | None. Launches a detached
# long-running process (the resumed daemon runs in the foreground, so it can't
# use the self-terminating ``Runner``). Returns the process handle so the caller
# can clean it up on a discovery timeout, or None on launch error.
Launcher = Callable[[list[str]], "subprocess.Popen[bytes] | subprocess.Popen[str] | None"]


def _default_launcher(argv: list[str]):
    """Launch ``argv`` as a detached background process and return its ``Popen``.

    ``start_new_session=True`` detaches the daemon into its own session/process
    group so it survives a bridge restart (it registers in ``polytoken sessions``
    and is re-attached by reconcile). stdout/stderr are discarded — the port is
    discovered via the session registry, not stdout parsing. Returns the Popen so
    the caller can terminate it if it fails to register (no orphan).
    """
    try:
        return subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        log.warning("failed to launch resumed daemon %r: %s", argv[0], exc)
        return None


class DaemonSupervisorError(Exception):
    """A ``polytoken`` CLI invocation failed."""


@dataclass(frozen=True)
class SpawnResult:
    session_id: str
    port: int


@dataclass(frozen=True)
class SessionInfo:
    """One row from ``polytoken sessions``."""

    session_id: str
    port: int
    pid: int
    started_at: str
    project_path: str


async def _default_runner(argv: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return (
        proc.returncode if proc.returncode is not None else -1,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


class DaemonSupervisor:
    def __init__(
        self,
        *,
        binary: str = "polytoken",
        runner: Runner | None = None,
        client_factory: ClientFactory | None = None,
        launcher: Launcher | None = None,
        global_config_dir: str | None = None,
    ) -> None:
        self._binary = binary
        self._runner = runner or _default_runner
        self._client_factory = client_factory or (lambda port: PolytokenClient(port))
        self._launcher = launcher or _default_launcher
        self._global_config_dir = global_config_dir or DEFAULT_GLOBAL_CONFIG_DIR

    # -- spawn ------------------------------------------------------------

    async def spawn(self, cwd: str, *, config_dir: str | None = None) -> SpawnResult:
        """Spawn a fresh headless daemon session rooted at ``cwd``.

        Runs ``polytoken --working-dir <cwd> [--config-dir <config_dir>]
        new --no-attach`` and parses the ``session_id=... port=...`` line.
        """
        argv = [self._binary, "--working-dir", cwd]
        if config_dir:
            argv += ["--config-dir", config_dir]
        argv += ["new", "--no-attach"]
        rc, out, err = await self._runner(argv)
        if rc != 0:
            raise DaemonSupervisorError(
                f"`polytoken new` exited {rc}: {(err or out).strip()[:500]}"
            )
        match = _SPAWN_RE.search(out)
        if not match:
            raise DaemonSupervisorError(
                f"could not parse session id/port from `polytoken new` output: {out.strip()[:500]!r}"
            )
        return SpawnResult(session_id=match.group("sid"), port=int(match.group("port")))

    # -- resume -----------------------------------------------------------

    async def resume(
        self,
        session_id: str,
        cwd: str,
        *,
        config_dir: str | None = None,
        discover_timeout: float = _RESUME_DISCOVER_TIMEOUT_SECS,
    ) -> SpawnResult:
        """Resume a previously-terminated daemon session rooted at ``cwd``.

        Runs ``polytoken daemon --resume --session-id <id> --project-dir <cwd>
        --global-config-dir <dir> --listen 127.0.0.1:0`` as a detached background
        process, then discovers its assigned port by polling ``polytoken sessions``
        until the session re-appears (the resumed daemon registers itself).

        Unlike :meth:`spawn` (which uses ``new --no-attach`` and a self-terminating
        invocation), the resumed daemon runs in the foreground and is launched via
        the injectable :attr:`_launcher` (detached via ``start_new_session``).

        Idempotent: if the session is already live, returns its existing port
        without relaunching. Raises :class:`DaemonSupervisorError` if the launcher
        fails or the session doesn't register within ``discover_timeout``.
        """
        # Pre-check: a live daemon for this session is resumed as-is.
        existing = await self.find_session(session_id)
        if existing is not None:
            log.info("resume %s: already live on port %d", session_id, existing.port)
            return SpawnResult(session_id=session_id, port=existing.port)

        gcfg = config_dir or self._global_config_dir
        argv = [
            self._binary,
            "daemon",
            "--resume",
            "--session-id",
            session_id,
            "--project-dir",
            cwd,
            "--global-config-dir",
            gcfg,
            "--listen",
            "127.0.0.1:0",
        ]
        proc = self._launcher(argv)
        if proc is None:
            raise DaemonSupervisorError(
                f"failed to launch resumed daemon for session {session_id}"
            )
        log.info("resume %s: launched pid %d, discovering port", session_id, proc.pid)

        # Poll the registry until the resumed daemon registers with a port.
        deadline = time.monotonic() + discover_timeout
        while time.monotonic() < deadline:
            info = await self.find_session(session_id)
            if info is not None:
                log.info("resume %s: registered on port %d", session_id, info.port)
                return SpawnResult(session_id=session_id, port=info.port)
            await asyncio.sleep(_RESUME_POLL_INTERVAL_SECS)
        # Discovery timed out: clean up the launched daemon so it can't orphan.
        # Best-effort HTTP terminate (it may have registered late), then kill the
        # process directly (it may never have registered).
        with contextlib.suppress(Exception):
            await self.terminate(session_id)
        with contextlib.suppress(Exception):
            proc.terminate()
        raise DaemonSupervisorError(
            f"resumed daemon for session {session_id} did not register in "
            f"`polytoken sessions` within {discover_timeout:.0f}s; cleaned up"
        )

    # -- registry ---------------------------------------------------------

    async def list_sessions(self) -> list[SessionInfo]:
        """Parse ``polytoken sessions`` into rows.

        Listing also stale-cleans dead registry entries as a side effect, so
        the result reflects only live daemons.
        """
        rc, out, err = await self._runner([self._binary, "sessions"])
        if rc != 0:
            raise DaemonSupervisorError(
                f"`polytoken sessions` exited {rc}: {(err or out).strip()[:500]}"
            )
        return self._parse_sessions(out)

    @staticmethod
    def _parse_sessions(out: str) -> list[SessionInfo]:
        rows: list[SessionInfo] = []
        for line in out.splitlines():
            line = line.rstrip()
            if not line or line.startswith("SESSION_ID"):
                continue
            # First four columns are whitespace-free; project_path is last and
            # may contain spaces, so cap the split at four.
            parts = line.split(None, 4)
            if len(parts) < 4:
                continue
            sid, port_s, pid_s = parts[0], parts[1], parts[2]
            started_at = parts[3]
            project_path = parts[4] if len(parts) >= 5 else ""
            try:
                port, pid = int(port_s), int(pid_s)
            except ValueError:
                log.warning("skipping unparseable `sessions` row: %r", line)
                continue
            rows.append(
                SessionInfo(
                    session_id=sid,
                    port=port,
                    pid=pid,
                    started_at=started_at,
                    project_path=project_path,
                )
            )
        return rows

    async def list_models(self) -> list[str]:
        """Parse ``polytoken models`` into the selectable model registry keys.

        Returns the base model names (the ``- <name>`` lines), stripping any
        trailing `` (default)`` / `` (small)`` annotation. Used for the
        ``/model`` autocomplete; the call is config-level, not per-session.
        """
        rc, out, err = await self._runner([self._binary, "models"])
        if rc != 0:
            raise DaemonSupervisorError(
                f"`polytoken models` exited {rc}: {(err or out).strip()[:500]}"
            )
        return self._parse_models(out)

    @staticmethod
    def _parse_models(out: str) -> list[str]:
        names: list[str] = []
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                name = stripped[2:].split()[0]
                if name and name not in names:
                    names.append(name)
        return names

    def client_for(self, port: int) -> PolytokenClient:
        """Build a client for a registry-listed session (e.g. the notify gate)."""
        return self._client_factory(port)

    async def find_session(self, session_id: str) -> SessionInfo | None:
        """Return the live registry entry for ``session_id``, or ``None``."""
        for info in await self.list_sessions():
            if info.session_id == session_id:
                return info
        return None

    # -- terminate --------------------------------------------------------

    async def terminate(self, session_id: str) -> bool:
        """Gracefully terminate a session via its daemon ``POST /terminate``.

        Returns ``True`` if a live session was found and asked to terminate,
        ``False`` if the session was already gone from the registry. HTTP
        failures against a registry-listed session propagate.
        """
        info = await self.find_session(session_id)
        if info is None:
            return False
        client = self._client_factory(info.port)
        try:
            await client.terminate()
        except PolytokenClientError as exc:
            # A connection-refused here means the daemon died between the
            # registry read and the call; treat it as already-gone.
            if exc.status is None:
                log.info("terminate %s: daemon already unreachable (%s)", session_id, exc)
                return False
            raise
        finally:
            await client.aclose()
        return True

    def binary_available(self) -> bool:
        """True if the ``polytoken`` binary resolves on PATH (for `cli doctor`)."""
        return shutil.which(self._binary) is not None

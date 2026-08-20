"""Supervise per-task Polytoken daemon processes via the ``polytoken`` CLI.

The bridge runs one daemon per Slack task. This module owns the process
side of that: spawning a fresh headless session, listing the live session
registry, and terminating a session. The HTTP side of a session lives in
:class:`bridge.polytoken_client.PolytokenClient`.

The subprocess runner and the client factory are injectable so the supervisor
is unit-testable without a real ``polytoken`` binary or daemon.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from bridge.polytoken_client import PolytokenClient, PolytokenClientError

log = logging.getLogger(__name__)

# `polytoken new --no-attach` prints one line: `session_id=<id> port=<port>`.
_SPAWN_RE = re.compile(r"session_id=(?P<sid>\S+)\s+port=(?P<port>\d+)")

# Subprocess runner contract: (argv) -> (returncode, stdout, stderr).
Runner = Callable[[list[str]], Awaitable[tuple[int, str, str]]]
ClientFactory = Callable[..., PolytokenClient]


class DaemonSupervisorError(Exception):
    """A ``polytoken`` CLI invocation failed."""


@dataclass(frozen=True)
class SpawnResult:
    session_id: str
    port: int
    credential_file_path: str | None = None


@dataclass(frozen=True)
class SessionInfo:
    """One row from ``polytoken sessions --format json``.

    ``credential_file_path`` is the daemon's private bearer-token file.  It is
    intentionally carried only in runtime objects; task persistence stores the
    session id and re-discovers this path from the registry on startup.
    """

    session_id: str
    port: int
    pid: int
    started_at: str
    project_path: str
    credential_file_path: str | None = None


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
    ) -> None:
        self._binary = binary
        self._runner = runner or _default_runner
        self._client_factory = client_factory or (
            lambda port, credential_file_path=None: PolytokenClient(
                port, credential_file_path=credential_file_path
            )
        )

    # -- spawn ------------------------------------------------------------

    async def spawn(self, cwd: str, *, config_dir: str | None = None) -> SpawnResult:
        """Spawn a fresh headless daemon session rooted at ``cwd``.

        Runs ``polytoken --working-dir <cwd> [--config-dir <config_dir>]
        new --no-attach``, parses the ``session_id=... port=...`` line, then
        re-reads the JSON session registry to discover the private credential
        file emitted for the new daemon.
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
        session_id = match.group("sid")
        info = await self.find_session(session_id)
        if info is None:
            raise DaemonSupervisorError(
                f"new daemon {session_id!r} was not found in `polytoken sessions --format json`"
            )
        if not info.credential_file_path:
            raise DaemonSupervisorError(
                f"new daemon {session_id!r} has no credential file in the session registry"
            )
        return SpawnResult(
            session_id=session_id,
            port=int(match.group("port")),
            credential_file_path=info.credential_file_path,
        )

    # -- registry ---------------------------------------------------------

    async def list_sessions(self) -> list[SessionInfo]:
        """Parse the JSON ``polytoken sessions`` registry into rows.

        Listing also stale-cleans dead registry entries as a side effect, so
        the result reflects only live daemons.  A one-time fallback to the old
        human table keeps older Polytoken installations and test doubles
        usable; 0.6 deployments always take the JSON path above.
        """
        argv = [self._binary, "sessions", "--format", "json"]
        rc, out, err = await self._runner(argv)
        if rc == 0:
            return self._parse_sessions(out)

        # Polytoken before the JSON registry may reject --format.  Preserve a
        # narrow compatibility fallback without making the table the primary
        # contract.
        legacy_rc, legacy_out, legacy_err = await self._runner([self._binary, "sessions"])
        if legacy_rc == 0:
            return self._parse_sessions(legacy_out)
        raise DaemonSupervisorError(
            f"`polytoken sessions --format json` exited {rc}: {(err or out).strip()[:500]}"
        )

    @staticmethod
    def _parse_sessions(out: str) -> list[SessionInfo]:
        """Parse 0.6 JSON rows, with a compatibility parser for old tables."""
        stripped = out.lstrip()
        try:
            payload = json.loads(out)
        except json.JSONDecodeError as exc:
            if stripped.startswith(("[", "{")):
                raise DaemonSupervisorError(
                    "`polytoken sessions --format json` returned invalid JSON"
                ) from exc
            return DaemonSupervisor._parse_sessions_table(out)
        if not isinstance(payload, list):
            raise DaemonSupervisorError("`polytoken sessions --format json` did not return a list")

        rows: list[SessionInfo] = []
        for item in payload:
            if not isinstance(item, Mapping):
                log.warning("skipping non-object `sessions --format json` row")
                continue
            try:
                session_id = str(item["session_id"])
                port = int(item["port"])
                pid = int(item["pid"])
                started_at = str(item["started_at"])
                project_path = str(item["project_path"])
            except (KeyError, TypeError, ValueError):
                log.warning("skipping malformed `sessions --format json` row")
                continue
            if not session_id or not project_path:
                log.warning("skipping incomplete `sessions --format json` row")
                continue
            credential = item.get("credential_file_path")
            if credential is not None and not isinstance(credential, str):
                log.warning("skipping `sessions --format json` row with invalid credential path")
                continue
            rows.append(
                SessionInfo(
                    session_id=session_id,
                    port=port,
                    pid=pid,
                    started_at=started_at,
                    project_path=project_path,
                    credential_file_path=credential or None,
                )
            )
        return rows

    @staticmethod
    def _parse_sessions_table(out: str) -> list[SessionInfo]:
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
        if info.credential_file_path:
            client = self._client_factory(info.port, info.credential_file_path)
        else:
            # Compatibility with pre-auth test doubles/table registries.
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

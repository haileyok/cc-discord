"""Async wrapper around the zellij CLI for managing per-task tabs in a session.

zellij 0.43 removed `list-panes`, `--pane-id` targeting, and `send-keys`; 0.44
restored `list-panes` (now JSON-capable). We target each task by tab name
(e.g. `cc-aa429dc4`), focus the tab via `go-to-tab-name` before issuing
`write-chars`, and submit Enter via raw `action write 13`. On 0.44+ we use
`list-panes --json --state --tab` to detect exited panes inside live tabs;
on 0.43 we fall back to `query-tab-names` (which can only see tabs, not
their pane state).
"""

import asyncio
import json
import logging
import os
import subprocess

logger = logging.getLogger(__name__)

# Session name is configurable via env var so users can colocate bridge tasks
# with their existing zellij session (e.g. the one their SSH RemoteCommand
# attaches to). Defaults to `cc-bridge-worker` to match the bridge's purpose.
SESSION_NAME = os.environ.get("BRIDGE_ZELLIJ_SESSION", "cc-bridge-worker")


def _session_already_exists(stderr: str) -> bool:
    """Whether stderr from `zellij attach --create-background` indicates the
    session is already alive (zellij ≥ 0.43 reports this as a non-zero exit)."""
    s = stderr.lower()
    return "already exists" in s or "already running" in s


def _running_inside_target_session() -> bool:
    """Whether the current process is running inside the configured session.

    zellij sets `ZELLIJ_SESSION_NAME` for processes spawned inside a session,
    and refuses `zellij attach --create-background <name>` when <name> matches
    the current session (panic at commands.rs: "trying to attach to the current
    session"). When colocated, the session is alive by definition — skip the
    attach call entirely.
    """
    return os.environ.get("ZELLIJ_SESSION_NAME") == SESSION_NAME


class ZellijError(Exception):
    """Base for all zellij-wrapper errors."""


class ZellijSessionMissing(ZellijError):
    """Raised when the bridge session can't be created or attached."""


class ZellijSpawnError(ZellijError):
    """Raised when a new task tab can't be created or its claude pane can't run."""


class ZellijManager:
    """Async-friendly wrapper around the zellij CLI.

    Each bridge task is one named tab in the configured session. Tabs are named
    `cc-<task_id_prefix>` and identified by that name throughout the API
    surface (the legacy `pane_id` parameter names now carry tab names).
    """

    def __init__(self, executable: str = "zellij") -> None:
        self._executable = executable
        self._session_lock = asyncio.Lock()

    async def _run_unlocked(
        self,
        *argv: str,
        env: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, str, str]:
        """Run a subprocess command with timeout. Caller must hold _session_lock
        if serialization is required."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await proc.wait()
                except Exception:
                    pass
                raise ZellijError(f"Command timed out: {' '.join(argv)}") from None

            stdout, stderr = await proc.communicate()
            return (proc.returncode, stdout.decode(), stderr.decode())
        except ZellijError:
            raise
        except Exception as e:
            raise ZellijError(f"Subprocess error: {e}") from e

    async def _run(
        self,
        *argv: str,
        env: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, str, str]:
        """Run a subprocess command, acquiring _session_lock for serialization."""
        async with self._session_lock:
            return await self._run_unlocked(*argv, env=env, timeout=timeout)

    async def ensure_session_alive(self) -> None:
        """Idempotently ensure the configured zellij session exists.

        zellij ≥ 0.43 returns non-zero with stderr "Session already exists" when
        the session is already running; treat that as success. If we're running
        inside the target session itself, skip the attach (zellij panics on
        self-attach).
        """
        if _running_inside_target_session():
            return
        returncode, _, stderr = await self._run(
            self._executable, "attach", "--create-background", SESSION_NAME
        )
        if returncode != 0 and not _session_already_exists(stderr):
            raise ZellijSessionMissing(f"Failed to create/attach session: {stderr}")

    async def list_panes(self) -> list[dict]:
        """List bridge-owned task tabs in the session.

        Returns a list of dicts shaped `{"id": tab_name, "exited": bool}` for
        each tab whose name starts with `cc-`. The "id" field carries the tab
        name (preserving the historical `pane_id` contract).

        On zellij 0.44+ we use `list-panes --json --state --tab` and report
        `exited` truthfully: a task tab is considered exited iff every one
        of its panes has exited. (Each task layout produces exactly one
        pane, so in practice the AND folds to that single pane's value.)

        On older zellij (or any failure of the new path) we fall back to
        `query-tab-names`, in which case `exited` is always False because
        the legacy command has no pane-state info. Death is inferred from
        the tab disappearing entirely in the legacy path.
        """
        rc, stdout, stderr = await self._run(
            self._executable, "--session", SESSION_NAME,
            "action", "list-panes", "--json", "--state", "--tab",
        )
        if rc == 0:
            try:
                panes = json.loads(stdout)
            except json.JSONDecodeError as e:
                raise ZellijError(f"list-panes JSON parse failed: {e}") from e
            if not isinstance(panes, list):
                raise ZellijError(
                    f"list-panes JSON not a list: {type(panes).__name__}"
                )
            by_tab: dict[str, bool] = {}
            for pane in panes:
                if not isinstance(pane, dict):
                    continue
                tab_name = pane.get("tab_name")
                if not isinstance(tab_name, str) or not tab_name.startswith("cc-"):
                    continue
                ex = bool(pane.get("exited", False))
                # AND across panes in the same tab: tab is exited only when
                # all panes are. seed with True so the first pane sets state.
                by_tab[tab_name] = by_tab.get(tab_name, True) and ex
            return [{"id": n, "exited": e} for n, e in by_tab.items()]

        # Legacy fallback for zellij ≤ 0.43 (or transient list-panes failure).
        logger.info(
            "list-panes failed (rc=%d, stderr=%r); falling back to query-tab-names",
            rc, stderr.strip()[:120],
        )
        rc2, stdout2, stderr2 = await self._run(
            self._executable, "--session", SESSION_NAME, "action", "query-tab-names"
        )
        if rc2 != 0:
            raise ZellijError(f"query-tab-names failed: {stderr2}")
        names = [line.strip() for line in stdout2.splitlines() if line.strip()]
        return [{"id": n, "exited": False} for n in names if n.startswith("cc-")]

    async def write_to_pane(self, pane_id: str, text: str) -> None:
        """Type text into the task tab named `pane_id`.

        Multi-line text is wrapped in bracketed paste so Claude's TUI
        treats embedded LFs as content rather than Enter. The whole
        sequence — paste-begin + UTF-8 body + paste-end + optional CR —
        is dispatched in a single `action write` so zellij delivers it
        atomically; per-segment write-chars + interleaved write calls
        race with the TUI's paste-mode state and lose trailing content.

        Single-line bodies that start with `-` are also routed through
        the paste path, because zellij's `action write-chars` parses
        argv-style and silently drops args that look like flags — a
        free-text reply like `-y` or `--help` would otherwise vanish.

        Control bytes are stripped from the body before sending. This
        defends against a Discord message containing the bracketed-paste
        terminator (ESC[201~) — without the strip, a hostile message
        could close paste mode mid-body and have its tail interpreted
        as keypresses by the receiving TUI. Tabs and newlines are
        preserved (newlines are the whole point of paste-mode).
        """
        submit = text.endswith("\n")
        body = text[:-1] if submit else text
        # Drop C0/C1 control bytes (other than \n and \t) so a hostile
        # message can't terminate bracketed paste early or smuggle in
        # CSI/OSC/DCS sequences. ESC (\x1b) is the entry to all of
        # these and is also stripped — we add our own ESC[200~/ESC[201~
        # framing back below.
        body = "".join(
            ch for ch in body
            if ch in ("\n", "\t") or (ord(ch) >= 0x20 and ord(ch) != 0x7f)
        )
        # Force paste-mode for content that starts with `-` so write-chars
        # argparse doesn't eat it as a flag (see docstring).
        multiline = "\n" in body or body.startswith("-")

        async with self._session_lock:
            # 5s rather than the default 10s — these zellij actions are
            # near-instant when the session is healthy. When no client is
            # attached to the session, zellij can take a couple seconds
            # to respond to `go-to-tab-name`, hence not 3s. tasks.py adds
            # a single retry on top so a single transient hiccup doesn't
            # drop the user's message.
            rc, _, stderr = await self._run_unlocked(
                self._executable, "--session", SESSION_NAME,
                "action", "go-to-tab-name", pane_id,
                timeout=5.0,
            )
            if rc != 0:
                raise ZellijError(f"go-to-tab-name {pane_id!r} failed: {stderr}")

            if multiline:
                # ESC [ 2 0 0 ~ … body … ESC [ 2 0 1 ~ [CR]
                bytes_out: list[int] = [27, 91, 50, 48, 48, 126]
                bytes_out.extend(body.encode("utf-8"))
                bytes_out.extend([27, 91, 50, 48, 49, 126])
                if submit:
                    bytes_out.append(13)
                logger.info(
                    "relay → pane single write (%d bytes, paste-wrapped, submit=%s)",
                    len(bytes_out), submit,
                )
                await self._action_write_bytes(*bytes_out)
            else:
                if body:
                    logger.info(
                        "write-chars single-line (%d chars): %r",
                        len(body),
                        body[:120] + ("…" if len(body) > 120 else ""),
                    )
                    rc, _, stderr = await self._run_unlocked(
                        self._executable, "--session", SESSION_NAME,
                        "action", "write-chars", body,
                    )
                    if rc != 0:
                        raise ZellijError(f"write-chars failed: {stderr}")
                if submit:
                    await self._action_write_bytes(13)

    async def _action_write_bytes(self, *byte_vals: int) -> None:
        """Send raw bytes to the focused pane via `zellij action write`.

        `action write` accepts space-separated decimal byte values and emits
        them as a single contiguous write to the pane's stdin.
        """
        rc, _, stderr = await self._run_unlocked(
            self._executable, "--session", SESSION_NAME,
            "action", "write", *(str(b) for b in byte_vals),
        )
        if rc != 0:
            raise ZellijError(f"write {byte_vals!r} failed: {stderr}")

    async def send_keys(self, pane_id: str, *byte_vals: int) -> None:
        """Focus the named tab and dispatch the given raw byte values to it
        in a single `action write` call. Used for sending control keys like
        Tab (9), Enter (13), or hot-key digits to TUI prompts.
        """
        async with self._session_lock:
            rc, _, stderr = await self._run_unlocked(
                self._executable, "--session", SESSION_NAME,
                "action", "go-to-tab-name", pane_id,
                timeout=3.0,
            )
            if rc != 0:
                raise ZellijError(f"go-to-tab-name {pane_id!r} failed: {stderr}")
            await self._action_write_bytes(*byte_vals)

    async def close_pane(self, pane_id: str) -> None:
        """Close the task tab named `pane_id`. Best-effort: idempotent on
        missing tab, swallows timeouts/errors so callers (e.g. /kill) can
        still complete bridge-side cleanup when zellij is unresponsive.
        """
        async with self._session_lock:
            try:
                rc, _, stderr = await self._run_unlocked(
                    self._executable, "--session", SESSION_NAME,
                    "action", "go-to-tab-name", pane_id,
                    timeout=3.0,
                )
            except ZellijError as e:
                logger.warning(
                    "close_pane %s: go-to-tab-name failed (%s); continuing",
                    pane_id, e,
                )
                return
            if rc != 0:
                logger.info(
                    "close_pane %s: tab not found, treating as already closed",
                    pane_id,
                )
                return
            try:
                rc, _, stderr = await self._run_unlocked(
                    self._executable, "--session", SESSION_NAME,
                    "action", "close-tab",
                    timeout=3.0,
                )
            except ZellijError as e:
                logger.warning(
                    "close_pane %s: close-tab failed (%s); continuing",
                    pane_id, e,
                )
                return
            if rc != 0:
                logger.info("close-tab %s: %s", pane_id, stderr)

    async def spawn_task(
        self,
        cwd: str,
        pane_name: str,
        layout_path: str,
    ) -> str:
        """Spawn a new task tab named `pane_name` whose contents are
        described by the zellij KDL layout at `layout_path`. The layout
        is generated by tasks._write_task_layout and invokes
        `env K=V ... claude <args>` as the tab's only pane — no default
        shell pane.

        Acquires the session lock for the duration so concurrent spawns
        don't race on tab focus.

        Returns the tab name (`pane_name`).

        Raises ZellijSpawnError on any failure.
        """
        async with self._session_lock:
            if not _running_inside_target_session():
                rc, _, stderr = await self._run_unlocked(
                    self._executable, "attach", "--create-background", SESSION_NAME
                )
                if rc != 0 and not _session_already_exists(stderr):
                    raise ZellijSpawnError(f"Failed to create/attach session: {stderr}")

            rc, _, stderr = await self._run_unlocked(
                self._executable, "--session", SESSION_NAME,
                "action", "new-tab",
                "--name", pane_name,
                "--cwd", cwd,
                "--layout", layout_path,
            )
            if rc != 0:
                raise ZellijSpawnError(f"new-tab {pane_name!r} failed: {stderr}")

            return pane_name

#!/usr/bin/env python3
"""Phantom zellij client for headless cc-discord-bridge operation.

Allocates a PTY at a sane size and attaches a zellij client to the bridge's
session through it. Stdin/stdout/stderr are routed to /dev/null. The point is
to give the zellij session a real terminal client at a reasonable size so TUIs
spawned inside it (notably Claude Code v2.1.x) actually render and receive
keystrokes: ``zellij action write-chars`` is dropped on the floor when zellij
has no client attached, because the TUI is stuck waiting on terminal queries
(DA1/DA2/cursor-position) that nobody answers. Without this, a Discord- or
otherwise-headless-driven session binds but never renders — the pane reports a
bogus screen size and relayed prompts vanish until a human attaches a terminal.

Run standalone (`python -m bridge.phantom`) or let ``serve`` supervise it
(default; opt out with ``BRIDGE_PHANTOM=0``).

Env vars read:
  BRIDGE_ZELLIJ_SESSION   zellij session to attach to (default: meow)
  PHANTOM_COLUMNS         pty width  (default: 200)
  PHANTOM_LINES           pty height (default: 60)
"""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import struct
import sys
import termios
from dataclasses import dataclass

DEFAULT_SESSION = "meow"
DEFAULT_COLUMNS = 200
DEFAULT_LINES = 60


@dataclass(frozen=True)
class PhantomConfig:
    """Resolved phantom settings (session name + PTY dimensions)."""

    session: str
    cols: int
    rows: int


def _phantom_config(env: dict[str, str] | None = None) -> PhantomConfig:
    """Resolve the phantom config from the environment.

    A missing or non-integer PHANTOM_COLUMNS/PHANTOM_LINES falls back to the
    default rather than crashing the client (and, when supervised, the daemon).
    """
    env = os.environ if env is None else env

    def _int(name: str, default: int) -> int:
        raw = env.get(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    return PhantomConfig(
        session=env.get("BRIDGE_ZELLIJ_SESSION", DEFAULT_SESSION),
        cols=_int("PHANTOM_COLUMNS", DEFAULT_COLUMNS),
        rows=_int("PHANTOM_LINES", DEFAULT_LINES),
    )


def main() -> int:
    cfg = _phantom_config()

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", cfg.rows, cfg.cols, 0, 0))

    pid = os.fork()
    if pid == 0:
        # Child: become session leader, claim slave as controlling tty, exec zellij.
        os.setsid()
        try:
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        except OSError:
            pass
        os.dup2(slave, 0)
        os.dup2(slave, 1)
        os.dup2(slave, 2)
        os.close(master)
        if slave > 2:
            os.close(slave)
        os.environ["TERM"] = os.environ.get("TERM", "xterm-256color")
        os.environ["COLUMNS"] = str(cfg.cols)
        os.environ["LINES"] = str(cfg.rows)
        os.execvp("zellij", ["zellij", "attach", cfg.session])

    # Parent: drain master to /dev/null so the kernel buffer never fills.
    os.close(slave)
    while True:
        try:
            data = os.read(master, 4096)
        except OSError as e:
            if e.errno in (errno.EIO,):
                break
            raise
        if not data:
            break

    os.close(master)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


if __name__ == "__main__":
    sys.exit(main())

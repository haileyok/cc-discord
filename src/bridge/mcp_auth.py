"""Private credential shared by the live bridge and local stdio MCP proxy."""
from __future__ import annotations

import os
import secrets
import stat
import tempfile
from pathlib import Path

MCP_TOKEN_FILE = Path(os.environ.get(
    "BRIDGE_MCP_TOKEN_FILE",
    str(Path.home() / ".config" / "claude-slack-bridge" / "mcp-token"),
)).expanduser()


class McpTokenError(RuntimeError):
    pass


def _validate(path: Path) -> str:
    if path.is_symlink():
        raise McpTokenError("MCP token file must not be a symlink")
    try:
        info = path.stat()
    except OSError as exc:
        raise McpTokenError("cannot access MCP token file") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise McpTokenError("MCP token file must be a regular 0600 file")
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise McpTokenError("cannot read MCP token file") from exc
    if len(value) < 43 or any(char.isspace() for char in value):
        raise McpTokenError("MCP token file is malformed")
    return value


def load_or_create_mcp_token(path: Path = MCP_TOKEN_FILE) -> str:
    """Atomically create a high-entropy token, or validate the existing one."""
    if path.exists() or path.is_symlink():
        return _validate(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    value = secrets.token_urlsafe(32)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            fd = -1
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return _validate(path)
        return _validate(path)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary.unlink(missing_ok=True)


__all__ = ["MCP_TOKEN_FILE", "McpTokenError", "load_or_create_mcp_token"]

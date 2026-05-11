import json
import logging
import os
import stat
import warnings
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SECRETS_DIR = Path.home() / ".config" / "cc-bridge"
SECRETS_FILE = SECRETS_DIR / "secrets.json"
REQUIRED_KEYS = ("DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID")

_OLD_SECRETS_DIR = Path.home() / ".config" / "claude-discord-bridge"
_OLD_SECRETS_FILE = _OLD_SECRETS_DIR / "secrets.json"


class SecretsError(RuntimeError):
    """Raised when secrets file is invalid or missing."""

    pass


@dataclass(frozen=True)
class Secrets:
    bot_token: str
    channel_id: int


def _resolve_secrets_path() -> Path:
    """Return the secrets file path, falling back to the old location with a warning."""
    override = os.environ.get("BRIDGE_SECRETS_PATH")
    if override:
        return Path(override).expanduser()
    if SECRETS_FILE.exists():
        return SECRETS_FILE
    if _OLD_SECRETS_FILE.exists():
        warnings.warn(
            f"Loading secrets from deprecated path {_OLD_SECRETS_FILE}. "
            f"Move to {SECRETS_FILE} to silence this warning.",
            DeprecationWarning,
            stacklevel=3,
        )
        return _OLD_SECRETS_FILE
    return SECRETS_FILE  # default (will error on read if missing)


def load_secrets(path: Path | None = None) -> Secrets:
    """Load secrets from JSON file.

    Reads the secrets file, validates required keys are present and non-empty,
    and coerces DISCORD_CHANNEL_ID to int.

    If path is None, resolves the path with fallback to old location.

    Raises SecretsError if the file is missing, unreadable, malformed JSON,
    missing keys, or has non-int channel ID. Error messages point users at
    'cc-bridge init'.
    """
    if path is None:
        path = _resolve_secrets_path()

    if not path.exists():
        raise SecretsError(
            f"Secrets file not found at {path}. "
            f"Run 'cc-bridge init' to create it."
        )

    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise SecretsError(
            f"Secrets file at {path} contains invalid JSON: {e}. "
            f"Run 'cc-bridge init' to recreate it."
        ) from e
    except OSError as e:
        raise SecretsError(
            f"Cannot read secrets file at {path}: {e}. "
            f"Run 'cc-bridge init' to recreate it."
        ) from e

    # Validate required keys are present and non-empty
    bot_token = data.get("DISCORD_BOT_TOKEN", "").strip()
    if not bot_token:
        raise SecretsError(
            f"DISCORD_BOT_TOKEN missing or empty in {path}. "
            f"Run 'cc-bridge init' to set it."
        )

    channel_id_val = data.get("DISCORD_CHANNEL_ID")
    if channel_id_val is None or channel_id_val == "":
        raise SecretsError(
            f"DISCORD_CHANNEL_ID missing or empty in {path}. "
            f"Run 'cc-bridge init' to set it."
        )

    try:
        channel_id = int(channel_id_val)
    except (ValueError, TypeError) as e:
        raise SecretsError(
            f"DISCORD_CHANNEL_ID must be a number; got {channel_id_val!r} in {path}. "
            f"Run 'cc-bridge init' to fix it."
        ) from e

    logger.info("loaded secrets from %s", path)
    return Secrets(bot_token=bot_token, channel_id=channel_id)


def write_secrets(secrets: Secrets, path: Path = SECRETS_FILE) -> None:
    """Write secrets to a 0600 JSON file.

    Creates parent directories with 0700 mode. Opens the secrets file
    via `os.open` with mode 0o600 baked in, so the bot token never
    exists on disk world-readable — closes the umask-window TOCTOU
    that `write_text` + `chmod` left open.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)

    data = {
        "DISCORD_BOT_TOKEN": secrets.bot_token,
        "DISCORD_CHANNEL_ID": secrets.channel_id,
    }
    payload = json.dumps(data, indent=2)

    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
    except BaseException:
        # If write failed mid-stream, the file may exist with partial
        # contents under 0600 already — that's fine, but ensure we don't
        # leak the fd if fdopen raises before taking ownership.
        try:
            os.close(fd)
        except OSError:
            pass
        raise

    # Defensive: if the file pre-existed with looser perms, our O_CREAT
    # didn't change them. Force 0600 now.
    path.chmod(0o600)


def secrets_file_perms(path: Path = SECRETS_FILE) -> int | None:
    """Return the file's mode bits, or None if the file doesn't exist."""
    if not path.exists():
        return None
    return stat.S_IMODE(path.stat().st_mode)

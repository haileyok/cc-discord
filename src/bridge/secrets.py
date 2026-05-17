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
    channel_id: str | int  # str for Mattermost, int for Discord
    # Discord-specific: None for Mattermost
    platform: str = "discord"
    # Mattermost-specific
    server_url: str | None = None
    allowed_user_ids: list[str] | None = None
    slash_command_tokens: list[str] | None = None


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

    Reads the secrets file and validates required keys based on platform.
    Supports both old Discord-only format and new platform-aware format.

    If path is None, resolves the path with fallback to old location.

    Raises SecretsError if the file is missing, unreadable, malformed JSON,
    or missing required keys. Error messages point users at 'cc-bridge init'.
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

    # Determine platform: from 'platform' field or infer from old format
    platform = data.get("platform", "discord").lower()

    # Handle old Discord-only format (DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID)
    if platform == "discord" and "DISCORD_BOT_TOKEN" in data:
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

        logger.info("loaded secrets from %s (old Discord format)", path)
        return Secrets(bot_token=bot_token, channel_id=channel_id, platform="discord")

    # New platform-aware format
    bot_token = data.get("bot_token", "").strip()
    if not bot_token:
        raise SecretsError(
            f"bot_token missing or empty in {path}. "
            f"Run 'cc-bridge init' to set it."
        )

    channel_id_val = data.get("channel_id")
    if channel_id_val is None or channel_id_val == "":
        raise SecretsError(
            f"channel_id missing or empty in {path}. "
            f"Run 'cc-bridge init' to set it."
        )

    # Validate platform-specific fields
    if platform == "discord":
        try:
            channel_id = int(channel_id_val)
        except (ValueError, TypeError) as e:
            raise SecretsError(
                f"channel_id must be a number for Discord; got {channel_id_val!r} in {path}. "
                f"Run 'cc-bridge init' to fix it."
            ) from e
    elif platform == "mattermost":
        channel_id = str(channel_id_val)
        server_url = data.get("server_url", "").strip()
        if not server_url:
            raise SecretsError(
                f"server_url missing or empty in {path}. "
                f"Run 'cc-bridge init' to set it."
            )
        allowed_user_ids = data.get("allowed_user_ids")
        raw_tokens = data.get("slash_command_tokens") or data.get("slash_command_token")
        if isinstance(raw_tokens, str):
            slash_command_tokens = [raw_tokens]
        elif isinstance(raw_tokens, list):
            slash_command_tokens = raw_tokens
        else:
            slash_command_tokens = None
        logger.info("loaded secrets from %s (Mattermost format)", path)
        return Secrets(
            bot_token=bot_token,
            channel_id=channel_id,
            platform="mattermost",
            server_url=server_url,
            allowed_user_ids=allowed_user_ids,
            slash_command_tokens=slash_command_tokens,
        )
    else:
        raise SecretsError(
            f"Unknown platform '{platform}' in {path}. "
            f"Valid values: discord, mattermost. Run 'cc-bridge init' to fix it."
        )

    logger.info("loaded secrets from %s", path)
    return Secrets(bot_token=bot_token, channel_id=channel_id, platform=platform)


def write_secrets(secrets: Secrets, path: Path = SECRETS_FILE) -> None:
    """Write secrets to a 0600 JSON file.

    Creates parent directories with 0700 mode. Opens the secrets file
    via `os.open` with mode 0o600 baked in, so the bot token never
    exists on disk world-readable — closes the umask-window TOCTOU
    that `write_text` + `chmod` left open.

    Supports both new platform-aware format and legacy Discord-only format.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)

    # Use new platform-aware format
    data = {
        "platform": secrets.platform,
        "bot_token": secrets.bot_token,
        "channel_id": secrets.channel_id,
    }

    # Add platform-specific fields
    if secrets.platform == "mattermost":
        data["server_url"] = secrets.server_url
        if secrets.allowed_user_ids:
            data["allowed_user_ids"] = secrets.allowed_user_ids
        if secrets.slash_command_tokens:
            data["slash_command_tokens"] = secrets.slash_command_tokens
    elif secrets.platform == "discord":
        # For Discord, also keep the old field names for compatibility
        data["DISCORD_BOT_TOKEN"] = secrets.bot_token
        data["DISCORD_CHANNEL_ID"] = secrets.channel_id

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

"""Secure Slack credentials and bridge configuration.

The bridge deliberately has one configuration namespace and one on-disk
location.  In particular, this module never probes, imports, or migrates a
Discord-era configuration file: an old file is simply not a valid Slack
configuration and ``load_secrets`` reports the missing Slack fields.
"""
from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

SECRETS_DIR = Path.home() / ".config" / "claude-slack-bridge"
SECRETS_FILE = SECRETS_DIR / "secrets.json"
REQUIRED_KEYS = (
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_TEAM_ID",
    "SLACK_HOME_CHANNEL_ID",
    "SLACK_OWNER_USER_ID",
)


class SecretsError(RuntimeError):
    """Raised when the Slack secrets file is invalid or missing."""


@dataclass(frozen=True)
class Secrets:
    """Validated Slack credentials and the bridge's trusted conversation.

    ``channel_id`` and ``bot_token`` remain available as read-only aliases for
    the provider-neutral server/task code during the adapter migration.  They
    do not represent additional configuration fields and are never serialized.
    """

    bot_token: str
    app_token: str
    team_id: str
    home_channel_id: str
    owner_user_id: str

    @property
    def channel_id(self) -> str:
        """Compatibility alias for the configured private home channel."""
        return self.home_channel_id


def _config_hint(path: Path) -> str:
    return f"Run 'claude-slack-bridge init' to create or repair {path}."


def _required_text(data: Mapping[str, Any], key: str, path: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        value = "" if value is None else str(value)
    value = value.strip()
    if not value:
        raise SecretsError(f"{key} missing or empty in {path}. {_config_hint(path)}")
    return value


def _load_data(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        raise SecretsError(
            f"Slack secrets file not found at {path}. {_config_hint(path)}"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SecretsError(
            f"Slack secrets file at {path} contains invalid JSON: {exc}. "
            f"{_config_hint(path)}"
        ) from exc
    except OSError as exc:
        raise SecretsError(
            f"Cannot read Slack secrets file at {path}: {exc}. {_config_hint(path)}"
        ) from exc
    if not isinstance(data, Mapping):
        raise SecretsError(
            f"Slack secrets file at {path} must contain a JSON object. "
            f"{_config_hint(path)}"
        )
    return data


def load_secrets(path: Path = SECRETS_FILE) -> Secrets:
    """Load and validate the Slack-only JSON configuration.

    The bot token must be an ``xoxb-`` token and the Socket Mode app token must
    be an ``xapp-`` token.  IDs are treated as opaque Slack identifiers so this
    loader remains usable with Slack-compatible fixtures and future ID forms.
    Extra keys are ignored for forward compatibility, but no legacy Discord
    key is consulted or used as a fallback.
    """
    data = _load_data(path)
    bot_token = _required_text(data, "SLACK_BOT_TOKEN", path)
    app_token = _required_text(data, "SLACK_APP_TOKEN", path)
    team_id = _required_text(data, "SLACK_TEAM_ID", path)
    home_channel_id = _required_text(data, "SLACK_HOME_CHANNEL_ID", path)
    owner_user_id = _required_text(data, "SLACK_OWNER_USER_ID", path)

    if not bot_token.startswith("xoxb-"):
        raise SecretsError(
            f"SLACK_BOT_TOKEN must start with 'xoxb-' in {path}. {_config_hint(path)}"
        )
    if not app_token.startswith("xapp-"):
        raise SecretsError(
            f"SLACK_APP_TOKEN must start with 'xapp-' in {path}. {_config_hint(path)}"
        )

    logger.info("loaded Slack secrets from %s", path)
    return Secrets(
        bot_token=bot_token,
        app_token=app_token,
        team_id=team_id,
        home_channel_id=home_channel_id,
        owner_user_id=owner_user_id,
    )


def write_secrets(secrets: Secrets, path: Path = SECRETS_FILE) -> None:
    """Write Slack credentials to a 0600 JSON file in a 0700 directory."""
    values = {
        "SLACK_BOT_TOKEN": secrets.bot_token,
        "SLACK_APP_TOKEN": secrets.app_token,
        "SLACK_TEAM_ID": secrets.team_id,
        "SLACK_HOME_CHANNEL_ID": secrets.home_channel_id,
        "SLACK_OWNER_USER_ID": secrets.owner_user_id,
    }
    for key, value in values.items():
        if not isinstance(value, str) or not value.strip():
            raise SecretsError(f"cannot write empty {key}")
    if not secrets.bot_token.startswith("xoxb-"):
        raise SecretsError("SLACK_BOT_TOKEN must start with 'xoxb-'")
    if not secrets.app_token.startswith("xapp-"):
        raise SecretsError("SLACK_APP_TOKEN must start with 'xapp-'")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    payload = json.dumps(values, indent=2, sort_keys=True) + "\n"

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    path.chmod(0o600)


def secrets_file_perms(path: Path = SECRETS_FILE) -> int | None:
    """Return mode bits for an existing secrets file, otherwise ``None``."""
    if not path.exists():
        return None
    return stat.S_IMODE(path.stat().st_mode)


def secrets_dir_perms(path: Path = SECRETS_DIR) -> int | None:
    """Return mode bits for the secrets directory, otherwise ``None``."""
    if not path.exists():
        return None
    return stat.S_IMODE(path.stat().st_mode)

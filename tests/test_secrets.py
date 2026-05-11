import json
import stat
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest

from bridge.secrets import (
    Secrets,
    SecretsError,
    load_secrets,
    write_secrets,
    secrets_file_perms,
    _resolve_secrets_path,
)


def test_secrets_error_is_runtime_error():
    """SecretsError is a subclass of RuntimeError."""
    assert issubclass(SecretsError, RuntimeError)


class TestWriteAndRead:
    """Round-trip: write then read."""

    def test_write_and_read_roundtrip(self, tmp_path: Path) -> None:
        """Writing a valid Secrets object then reading it back round-trips."""
        secrets_file = tmp_path / "secrets.json"
        original = Secrets(bot_token="test_token", channel_id=12345)

        write_secrets(original, path=secrets_file)
        loaded = load_secrets(path=secrets_file)

        assert loaded.bot_token == "test_token"
        assert loaded.channel_id == 12345

    def test_write_creates_parent_dirs(self, tmp_path: Path) -> None:
        """write_secrets creates parent directories if they don't exist."""
        secrets_file = tmp_path / "subdir1" / "subdir2" / "secrets.json"
        secrets = Secrets(bot_token="token", channel_id=999)

        write_secrets(secrets, path=secrets_file)

        assert secrets_file.exists()

    def test_write_sets_mode_0600(self, tmp_path: Path) -> None:
        """After write_secrets, the file's mode is exactly 0o600."""
        secrets_file = tmp_path / "secrets.json"
        secrets = Secrets(bot_token="token", channel_id=999)

        write_secrets(secrets, path=secrets_file)

        perms = stat.S_IMODE(secrets_file.stat().st_mode)
        assert perms == 0o600

    def test_write_sets_dir_mode_0700(self, tmp_path: Path) -> None:
        """After write_secrets, the parent directory's mode is 0o700."""
        secrets_file = tmp_path / "config" / "secrets.json"
        secrets = Secrets(bot_token="token", channel_id=999)

        write_secrets(secrets, path=secrets_file)

        parent_perms = stat.S_IMODE(secrets_file.parent.stat().st_mode)
        assert parent_perms == 0o700


class TestLoadSecrets:
    """load_secrets error handling."""

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """Missing file raises SecretsError."""
        secrets_file = tmp_path / "does_not_exist.json"

        with pytest.raises(SecretsError):
            load_secrets(path=secrets_file)

    def test_malformed_json_raises(self, tmp_path: Path) -> None:
        """Malformed JSON raises SecretsError."""
        secrets_file = tmp_path / "secrets.json"
        secrets_file.write_text("{this is not valid json")

        with pytest.raises(SecretsError):
            load_secrets(path=secrets_file)

    def test_missing_bot_token_raises(self, tmp_path: Path) -> None:
        """Missing DISCORD_BOT_TOKEN key raises SecretsError."""
        secrets_file = tmp_path / "secrets.json"
        secrets_file.write_text(json.dumps({"DISCORD_CHANNEL_ID": 12345}))

        with pytest.raises(SecretsError):
            load_secrets(path=secrets_file)

    def test_missing_channel_id_raises(self, tmp_path: Path) -> None:
        """Missing DISCORD_CHANNEL_ID key raises SecretsError."""
        secrets_file = tmp_path / "secrets.json"
        secrets_file.write_text(json.dumps({"DISCORD_BOT_TOKEN": "token"}))

        with pytest.raises(SecretsError):
            load_secrets(path=secrets_file)

    def test_non_numeric_channel_id_raises(self, tmp_path: Path) -> None:
        """Non-numeric DISCORD_CHANNEL_ID raises SecretsError."""
        secrets_file = tmp_path / "secrets.json"
        secrets_file.write_text(
            json.dumps({
                "DISCORD_BOT_TOKEN": "token",
                "DISCORD_CHANNEL_ID": "not_a_number"
            })
        )

        with pytest.raises(SecretsError):
            load_secrets(path=secrets_file)

    def test_empty_bot_token_raises(self, tmp_path: Path) -> None:
        """Empty DISCORD_BOT_TOKEN raises SecretsError."""
        secrets_file = tmp_path / "secrets.json"
        secrets_file.write_text(
            json.dumps({
                "DISCORD_BOT_TOKEN": "",
                "DISCORD_CHANNEL_ID": 12345
            })
        )

        with pytest.raises(SecretsError):
            load_secrets(path=secrets_file)

    def test_empty_channel_id_string_raises(self, tmp_path: Path) -> None:
        """Empty string for DISCORD_CHANNEL_ID raises SecretsError."""
        secrets_file = tmp_path / "secrets.json"
        secrets_file.write_text(
            json.dumps({
                "DISCORD_BOT_TOKEN": "token",
                "DISCORD_CHANNEL_ID": ""
            })
        )

        with pytest.raises(SecretsError):
            load_secrets(path=secrets_file)

    def test_extra_keys_accepted(self, tmp_path: Path) -> None:
        """Valid file with extra unknown keys is accepted (forward-compat)."""
        secrets_file = tmp_path / "secrets.json"
        secrets_file.write_text(
            json.dumps({
                "DISCORD_BOT_TOKEN": "token",
                "DISCORD_CHANNEL_ID": 12345,
                "FUTURE_FEATURE": "something"
            })
        )

        loaded = load_secrets(path=secrets_file)
        assert loaded.bot_token == "token"
        assert loaded.channel_id == 12345


class TestSecretsFilePerms:
    """secrets_file_perms returns mode or None."""

    def test_existing_file_returns_mode(self, tmp_path: Path) -> None:
        """If file exists, return its mode bits."""
        secrets_file = tmp_path / "secrets.json"
        write_secrets(Secrets(bot_token="token", channel_id=123), path=secrets_file)

        perms = secrets_file_perms(path=secrets_file)
        assert perms == 0o600

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        """If file doesn't exist, return None."""
        secrets_file = tmp_path / "does_not_exist.json"

        perms = secrets_file_perms(path=secrets_file)
        assert perms is None


class TestBackwardCompatibility:
    """Test backward-compatible fallback from old path to new path."""

    def test_resolve_secrets_path_new_path_preferred(self, tmp_path: Path, monkeypatch) -> None:
        """_resolve_secrets_path prefers new path if it exists."""
        new_path = tmp_path / "cc-bridge" / "secrets.json"
        old_path = tmp_path / "claude-discord-bridge" / "secrets.json"

        new_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.parent.mkdir(parents=True, exist_ok=True)

        new_path.touch()
        old_path.touch()

        # Mock the home directory paths
        with patch("bridge.secrets.SECRETS_FILE", new_path):
            with patch("bridge.secrets._OLD_SECRETS_FILE", old_path):
                result = _resolve_secrets_path()
                assert result == new_path

    def test_resolve_secrets_path_fallback_with_warning(self, tmp_path: Path, monkeypatch) -> None:
        """_resolve_secrets_path falls back to old path with deprecation warning."""
        new_path = tmp_path / "cc-bridge" / "secrets.json"
        old_path = tmp_path / "claude-discord-bridge" / "secrets.json"

        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.touch()

        # Mock the home directory paths
        with patch("bridge.secrets.SECRETS_FILE", new_path):
            with patch("bridge.secrets._OLD_SECRETS_FILE", old_path):
                with warnings.catch_warnings(record=True) as w:
                    warnings.simplefilter("always")
                    result = _resolve_secrets_path()
                    assert result == old_path
                    assert len(w) == 1
                    assert issubclass(w[0].category, DeprecationWarning)
                    assert "deprecated path" in str(w[0].message)

    def test_load_secrets_with_fallback(self, tmp_path: Path, monkeypatch) -> None:
        """load_secrets works with fallback to old path."""
        new_path = tmp_path / "cc-bridge" / "secrets.json"
        old_path = tmp_path / "claude-discord-bridge" / "secrets.json"

        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_text(json.dumps({
            "DISCORD_BOT_TOKEN": "token",
            "DISCORD_CHANNEL_ID": 12345
        }))

        # Mock the home directory paths
        with patch("bridge.secrets.SECRETS_FILE", new_path):
            with patch("bridge.secrets._OLD_SECRETS_FILE", old_path):
                with warnings.catch_warnings(record=True) as w:
                    warnings.simplefilter("always")
                    loaded = load_secrets()
                    assert loaded.bot_token == "token"
                    assert loaded.channel_id == 12345
                    assert len(w) == 1
                    assert issubclass(w[0].category, DeprecationWarning)

    def test_resolve_secrets_path_env_override(self, tmp_path: Path, monkeypatch) -> None:
        """_resolve_secrets_path respects BRIDGE_SECRETS_PATH env var."""
        custom_path = tmp_path / "custom" / "secrets.json"
        custom_path.parent.mkdir(parents=True, exist_ok=True)
        custom_path.touch()

        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(custom_path))

        with patch("bridge.secrets.SECRETS_FILE", tmp_path / "cc-bridge" / "secrets.json"):
            with patch("bridge.secrets._OLD_SECRETS_FILE", tmp_path / "claude-discord-bridge" / "secrets.json"):
                result = _resolve_secrets_path()
                assert result == custom_path

    def test_state_path_updated(self) -> None:
        """State database path is updated to cc-bridge."""
        from bridge.state import DEFAULT_DB_PATH
        assert "cc-bridge" in str(DEFAULT_DB_PATH)
        assert "claude-discord-bridge" not in str(DEFAULT_DB_PATH)

    def test_task_settings_path_updated(self) -> None:
        """Task settings directory is updated to cc-bridge."""
        from bridge.tasks import TASK_SETTINGS_DIR
        assert "cc-bridge" in str(TASK_SETTINGS_DIR)
        assert "claude-discord-bridge" not in str(TASK_SETTINGS_DIR)

    def test_attachments_path_updated(self) -> None:
        """Attachments directory is updated to cc-bridge."""
        from bridge.tasks import ATTACHMENTS_DIR
        assert "cc-bridge" in str(ATTACHMENTS_DIR)
        assert "claude-discord-bridge" not in str(ATTACHMENTS_DIR)

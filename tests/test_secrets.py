import json
import os
import stat
from pathlib import Path

import bridge.secrets as secrets_module

import pytest

from bridge.secrets import (
    REQUIRED_KEYS,
    Secrets,
    SecretsError,
    load_secrets,
    secrets_dir_perms,
    secrets_file_perms,
    write_secrets,
)


def valid_secrets() -> Secrets:
    return Secrets(
        bot_token="xoxb-test-token",
        app_token="xapp-test-token",
        team_id="T012345",
        home_channel_id="C012345",
        owner_user_id="U012345",
    )


def test_secrets_error_is_runtime_error() -> None:
    assert issubclass(SecretsError, RuntimeError)


def test_round_trip_writes_only_slack_fields(tmp_path: Path) -> None:
    path = tmp_path / "config" / "secrets.json"
    write_secrets(valid_secrets(), path=path)
    loaded = load_secrets(path=path)
    assert loaded == valid_secrets()
    assert set(json.loads(path.read_text())) == set(REQUIRED_KEYS)
    assert "DISCORD_BOT_TOKEN" not in path.read_text()


def test_write_creates_private_directory_and_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "secrets.json"
    write_secrets(valid_secrets(), path=path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert secrets_file_perms(path) == 0o600
    assert secrets_dir_perms(path.parent) == 0o700


def test_overwrite_keeps_old_path_private_until_atomic_replace_and_cleans_temp(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "nested" / "secrets.json"
    path.parent.mkdir()
    path.write_text("old-secret")
    path.chmod(0o644)
    observed: dict[str, int] = {}
    real_replace = os.replace

    def checked_replace(source, destination):
        observed["temporary_mode"] = stat.S_IMODE(Path(source).stat().st_mode)
        observed["old_mode"] = stat.S_IMODE(Path(destination).stat().st_mode)
        real_replace(source, destination)

    monkeypatch.setattr(secrets_module.os, "replace", checked_replace)
    write_secrets(valid_secrets(), path=path)
    assert observed == {"temporary_mode": 0o600, "old_mode": 0o644}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.glob(f".{path.name}.*")) == []


@pytest.mark.parametrize("missing", REQUIRED_KEYS)
def test_each_required_key_is_required(tmp_path: Path, missing: str) -> None:
    path = tmp_path / "secrets.json"
    data = {
        "SLACK_BOT_TOKEN": "xoxb-test-token",
        "SLACK_APP_TOKEN": "xapp-test-token",
        "SLACK_TEAM_ID": "T012345",
        "SLACK_HOME_CHANNEL_ID": "C012345",
        "SLACK_OWNER_USER_ID": "U012345",
    }
    del data[missing]
    path.write_text(json.dumps(data))
    with pytest.raises(SecretsError, match=missing):
        load_secrets(path)


def test_discord_only_file_is_not_used_as_fallback(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"DISCORD_BOT_TOKEN": "token", "DISCORD_CHANNEL_ID": 1}))
    with pytest.raises(SecretsError, match="SLACK_BOT_TOKEN"):
        load_secrets(path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("SLACK_BOT_TOKEN", "token", "xoxb-"),
        ("SLACK_APP_TOKEN", "token", "xapp-"),
    ],
)
def test_token_prefixes_are_validated(tmp_path: Path, field: str, value: str, message: str) -> None:
    path = tmp_path / "secrets.json"
    data = {
        "SLACK_BOT_TOKEN": valid_secrets().bot_token,
        "SLACK_APP_TOKEN": valid_secrets().app_token,
        "SLACK_TEAM_ID": valid_secrets().team_id,
        "SLACK_HOME_CHANNEL_ID": valid_secrets().home_channel_id,
        "SLACK_OWNER_USER_ID": valid_secrets().owner_user_id,
    }
    data[field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(SecretsError, match=message):
        load_secrets(path)


def test_extra_keys_are_accepted(tmp_path: Path) -> None:
    path = tmp_path / "secrets.json"
    data = {
        "SLACK_BOT_TOKEN": valid_secrets().bot_token,
        "SLACK_APP_TOKEN": valid_secrets().app_token,
        "SLACK_TEAM_ID": valid_secrets().team_id,
        "SLACK_HOME_CHANNEL_ID": valid_secrets().home_channel_id,
        "SLACK_OWNER_USER_ID": valid_secrets().owner_user_id,
        "FUTURE_FEATURE": "allowed",
    }
    path.write_text(json.dumps(data))
    assert load_secrets(path).team_id == "T012345"


def test_write_rejects_invalid_token_prefix(tmp_path: Path) -> None:
    bad = valid_secrets().__class__(
        bot_token="not-xoxb", app_token=valid_secrets().app_token,
        team_id=valid_secrets().team_id, home_channel_id=valid_secrets().home_channel_id,
        owner_user_id=valid_secrets().owner_user_id,
    )
    with pytest.raises(SecretsError, match="xoxb"):
        write_secrets(bad, tmp_path / "secrets.json")


def test_missing_perms_path_returns_none(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    assert secrets_file_perms(missing) is None
    assert secrets_dir_perms(missing) is None

"""Focused tests for the Slack operational CLI."""
from __future__ import annotations

import json
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from bridge.cli import cli
from bridge.secrets import Secrets, write_secrets


def make_secrets() -> Secrets:
    return Secrets(
        bot_token="xoxb-test-token",
        app_token="xapp-test-token",
        team_id="T012345",
        home_channel_id="C012345",
        owner_user_id="U012345",
    )


class ReadyBot:
    instances: list["ReadyBot"] = []
    should_start = True

    def __init__(self, token: str, *, team_id: str, owner_user_id: str,
                 home_channel_id: str, app_token: str, **kwargs) -> None:
        self.token = token
        self.team_id = team_id
        self.owner_user_id = owner_user_id
        self.home_channel_id = home_channel_id
        self.app_token = app_token
        self.is_ready = False
        self.posts: list[str] = []
        self.__class__.instances.append(self)

    async def start(self) -> None:
        if self.should_start:
            self.is_ready = True
        else:
            raise RuntimeError("Slack unavailable")

    async def close(self) -> None:
        self.is_ready = False

    async def post(self, text: str, *args, **kwargs) -> list[str]:
        self.posts.append(text)
        return ["1.000"]

    def health(self) -> dict:
        return {
            "bot_connected": self.is_ready,
            "socket_mode_connected": self.is_ready,
            "team_id": self.team_id,
            "bot_user_id": "U-BOT",
        }


def _mock_subprocess(*, which="/usr/local/bin/polytoken", version_rc=0,
                     spawn_out="session_id=s1 port=33333", spawn_rc=0):
    def run_fn(cmd, *args, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        if "--version" in cmd:
            result.returncode = version_rc
            result.stdout = "polytoken 0.1.20\n"
        elif "new" in cmd:
            result.returncode = spawn_rc
            result.stdout = spawn_out
        elif "is-active" in cmd:
            # Legacy runtime is inactive in the healthy fixture.
            result.returncode = 1
        return result
    return which, run_fn


def _health_mock(*, bot_connected=True, socket_mode_connected=True):
    response = MagicMock()
    response.status = 200
    response.read.return_value = json.dumps({
        "bot_connected": bot_connected,
        "socket_mode_connected": socket_mode_connected,
    }).encode()
    response.close.return_value = None
    return response


class TestInitCommand:
    def test_init_writes_slack_secrets_and_validates_bot(self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(path))
        ReadyBot.instances.clear()
        monkeypatch.setattr("bridge.cli.Bot", ReadyBot)
        result = CliRunner().invoke(
            cli, ["init"],
            input="xoxb-test-token\nxapp-test-token\nT012345\nC012345\nU012345\n",
        )
        assert result.exit_code == 0, result.output
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        data = json.loads(path.read_text())
        assert set(data) == {
            "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_TEAM_ID",
            "SLACK_HOME_CHANNEL_ID", "SLACK_OWNER_USER_ID",
        }
        assert ReadyBot.instances[0].team_id == "T012345"
        assert any("init succeeded" in message for message in ReadyBot.instances[0].posts)

    def test_init_rejects_bad_token_before_bot_start(self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(path))
        monkeypatch.setattr("bridge.cli.Bot", ReadyBot)
        result = CliRunner().invoke(
            cli, ["init"], input="legacy-token\nxapp-test-token\nT1\nC1\nU1\n"
        )
        assert result.exit_code == 1
        assert "xoxb-" in result.output

    def test_init_existing_file_can_be_declined(self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(path))
        write_secrets(make_secrets(), path)
        result = CliRunner().invoke(cli, ["init"], input="n\n")
        assert result.exit_code == 1

    def test_init_reports_startup_failure(self, tmp_path: Path, monkeypatch) -> None:
        path = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(path))
        ReadyBot.should_start = False
        monkeypatch.setattr("bridge.cli.Bot", ReadyBot)
        result = CliRunner().invoke(
            cli, ["init"], input="xoxb-test-token\nxapp-test-token\nT1\nC1\nU1\n"
        )
        ReadyBot.should_start = True
        assert result.exit_code == 2
        assert "Slack startup validation failed" in result.output


class TestDoctorCommand:
    def _run(self, tmp_path: Path, monkeypatch, *, which, run_fn,
             urlopen, bot=ReadyBot):
        path = tmp_path / "secrets.json"
        state = tmp_path / "state"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(path))
        monkeypatch.setenv("BRIDGE_STATE_DIR", str(state))
        monkeypatch.setenv("BRIDGE_URL", "http://127.0.0.1:9999")
        write_secrets(make_secrets(), path)
        monkeypatch.setattr("bridge.cli.Bot", bot)
        with patch("urllib.request.urlopen", urlopen), \
             patch("bridge.cli.shutil.which", return_value=which), \
             patch("bridge.cli.subprocess.run", side_effect=run_fn):
            return CliRunner().invoke(cli, ["doctor"])

    def test_doctor_all_ok(self, tmp_path, monkeypatch) -> None:
        which, run = _mock_subprocess()
        result = self._run(
            tmp_path, monkeypatch, which=which, run_fn=run,
            urlopen=lambda *args, **kwargs: _health_mock(),
        )
        assert result.exit_code == 0, result.output
        assert "[ok] Slack startup" in result.output
        assert "[ok] Polytoken smoke" in result.output
        assert "[ok] storage" in result.output

    def test_doctor_bad_slack_startup_fails(self, tmp_path, monkeypatch) -> None:
        class BadBot(ReadyBot):
            async def start(self):
                raise RuntimeError("home channel membership failed")
        which, run = _mock_subprocess()
        result = self._run(
            tmp_path, monkeypatch, which=which, run_fn=run,
            urlopen=lambda *args, **kwargs: _health_mock(), bot=BadBot,
        )
        assert result.exit_code == 1
        assert "[fail] Slack startup" in result.output

    def test_doctor_polytoken_missing(self, tmp_path, monkeypatch) -> None:
        _, run = _mock_subprocess()
        result = self._run(
            tmp_path, monkeypatch, which=None, run_fn=run,
            urlopen=lambda *args, **kwargs: _health_mock(),
        )
        assert result.exit_code == 1
        assert "[fail] Polytoken CLI" in result.output

    def test_doctor_spawn_fails(self, tmp_path, monkeypatch) -> None:
        which, run = _mock_subprocess(spawn_out="garbage", spawn_rc=1)
        result = self._run(
            tmp_path, monkeypatch, which=which, run_fn=run,
            urlopen=lambda *args, **kwargs: _health_mock(),
        )
        assert result.exit_code == 1
        assert "[fail] Polytoken smoke" in result.output

    def test_doctor_health_unreachable(self, tmp_path, monkeypatch) -> None:
        which, run = _mock_subprocess()
        def urlopen(request, *args, **kwargs):
            if "/v1/health" in getattr(request, "full_url", str(request)):
                raise OSError("refused")
            return _health_mock()
        result = self._run(tmp_path, monkeypatch, which=which, run_fn=run, urlopen=urlopen)
        assert result.exit_code == 1
        assert "[fail] daemon health" in result.output

    def test_doctor_bad_secret_mode_fails(self, tmp_path, monkeypatch) -> None:
        which, run = _mock_subprocess()
        path = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(path))
        monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
        write_secrets(make_secrets(), path)
        path.chmod(0o644)
        with patch("bridge.cli.Bot", ReadyBot), \
             patch("urllib.request.urlopen", lambda *a, **k: _health_mock()), \
             patch("bridge.cli.shutil.which", return_value=which), \
             patch("bridge.cli.subprocess.run", side_effect=run):
            result = CliRunner().invoke(cli, ["doctor"])
        assert result.exit_code == 1
        assert "secrets file permissions" in result.output


class TestServeCommand:
    def test_serve_help_has_host_and_port(self) -> None:
        result = CliRunner().invoke(cli, ["serve", "--help"])
        assert result.exit_code == 0
        assert "--host" in result.output
        assert "--port" in result.output

    def test_serve_without_slack_secrets_exits_2(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(tmp_path / "missing.json"))
        result = CliRunner().invoke(cli, ["serve"])
        assert result.exit_code == 2
        assert "invalid or unreadable Slack configuration" in result.output

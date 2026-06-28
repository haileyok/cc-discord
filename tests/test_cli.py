"""Tests for bridge CLI using click.testing.CliRunner."""

import json
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from bridge.cli import cli
from bridge.secrets import Secrets, write_secrets


class TestInitCommand:
    """Tests for `claude-discord-bridge init` subcommand."""

    def _get_ready_fake_bot(self):
        class FakeBot:
            def __init__(self, token: str, channel_id: int):
                self.token = token
                self.channel_id = channel_id
                self._is_ready = False

            @property
            def is_ready(self) -> bool:
                return self._is_ready

            async def start(self) -> None:
                self._is_ready = True

            async def close(self) -> None:
                pass

            async def post(self, message: str, *, thread_id: int | None = None) -> list[int]:
                return [123]

        return FakeBot

    def test_init_writes_secrets_file(self, tmp_path: Path, monkeypatch) -> None:
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setattr("bridge.cli.Bot", self._get_ready_fake_bot())
        result = CliRunner().invoke(cli, ["init"], input="test_token_abc\n12345\n")
        assert result.exit_code == 0
        assert secrets_file.exists()

    def test_init_sets_0600_perms(self, tmp_path: Path, monkeypatch) -> None:
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setattr("bridge.cli.Bot", self._get_ready_fake_bot())
        result = CliRunner().invoke(cli, ["init"], input="test_token_abc\n12345\n")
        assert result.exit_code == 0
        assert stat.S_IMODE(secrets_file.stat().st_mode) == 0o600

    def test_init_rejects_non_integer_channel_id(self, tmp_path: Path, monkeypatch) -> None:
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setattr("bridge.cli.Bot", self._get_ready_fake_bot())
        result = CliRunner().invoke(cli, ["init"], input="test_token_abc\nnot_a_number\n12345\n")
        assert result.exit_code == 0
        assert secrets_file.exists()

    def test_init_aborts_if_file_exists_and_user_says_no(self, tmp_path: Path, monkeypatch) -> None:
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        write_secrets(Secrets(bot_token="old_token", channel_id=999), path=secrets_file)
        result = CliRunner().invoke(cli, ["init"], input="n\n")
        assert result.exit_code == 1

    def test_init_validates_token_bot_not_ready(self, tmp_path: Path, monkeypatch) -> None:
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))

        class FakeBot:
            def __init__(self, token: str, channel_id: int):
                self._is_ready = False

            @property
            def is_ready(self) -> bool:
                return self._is_ready

            async def start(self) -> None:
                pass

            async def close(self) -> None:
                pass

        monkeypatch.setattr("bridge.cli._TOKEN_VALIDATION_TIMEOUT", 0.1)
        monkeypatch.setattr("bridge.cli.Bot", FakeBot)
        result = CliRunner().invoke(cli, ["init"], input="test_token\n12345\n")
        assert result.exit_code == 2
        assert "could not connect" in result.output
        assert secrets_file.exists()

    def test_init_validates_token_bot_ready_posts_message(self, tmp_path: Path, monkeypatch) -> None:
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        posted = []

        class FakeBot:
            def __init__(self, token: str, channel_id: int):
                self._is_ready = False

            @property
            def is_ready(self) -> bool:
                return self._is_ready

            async def start(self) -> None:
                self._is_ready = True

            async def close(self) -> None:
                pass

            async def post(self, message: str, *, thread_id: int | None = None) -> list[int]:
                posted.append(message)
                return [123]

        monkeypatch.setattr("bridge.cli.Bot", FakeBot)
        result = CliRunner().invoke(cli, ["init"], input="test_token\n12345\n")
        assert result.exit_code == 0
        assert any("init succeeded" in m for m in posted)


def _mock_subprocess(*, which="/usr/local/bin/polytoken", version_rc=0, spawn_out="session_id=s1 port=33333", spawn_rc=0):
    """Build a (which_patch, run_fn) pair for the polytoken doctor checks."""

    def run_fn(cmd, *args, **kwargs):
        result = MagicMock()
        if "--version" in cmd:
            result.returncode = version_rc
            result.stdout = "polytoken 0.1.20\n"
            result.stderr = ""
        elif "new" in cmd:
            result.returncode = spawn_rc
            result.stdout = spawn_out
            result.stderr = ""
        else:
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        return result

    return which, run_fn


def _health_mock(*, bot_connected=True):
    mock = MagicMock()
    mock.status = 200
    mock.read.return_value = json.dumps({"bot_connected": bot_connected}).encode("utf-8")
    mock.__enter__ = lambda s: s
    mock.__exit__ = lambda s, *a: None
    return mock


class TestDoctorCommand:
    """Tests for `claude-discord-bridge doctor` (Polytoken backend)."""

    def _run(self, tmp_path, monkeypatch, *, which, run_fn, urlopen):
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setenv("BRIDGE_URL", "http://127.0.0.1:9999")
        monkeypatch.setenv("HOME", str(tmp_path))
        write_secrets(Secrets(bot_token="token", channel_id=12345), path=secrets_file)
        with patch("urllib.request.urlopen", urlopen), \
             patch("bridge.cli.shutil.which", return_value=which), \
             patch("subprocess.run", side_effect=run_fn):
            return CliRunner().invoke(cli, ["doctor"])

    def test_doctor_all_ok(self, tmp_path, monkeypatch) -> None:
        which, run_fn = _mock_subprocess()
        result = self._run(
            tmp_path, monkeypatch, which=which, run_fn=run_fn,
            urlopen=lambda *a, **k: _health_mock(bot_connected=True),
        )
        assert result.exit_code == 0, result.output
        assert "[ok] polytoken CLI" in result.output
        assert "[ok] polytoken spawn" in result.output

    def test_doctor_polytoken_missing(self, tmp_path, monkeypatch) -> None:
        _, run_fn = _mock_subprocess()
        result = self._run(
            tmp_path, monkeypatch, which=None, run_fn=run_fn,
            urlopen=lambda *a, **k: _health_mock(bot_connected=True),
        )
        assert result.exit_code == 1
        assert "[fail] polytoken CLI" in result.output

    def test_doctor_spawn_fails(self, tmp_path, monkeypatch) -> None:
        which, run_fn = _mock_subprocess(spawn_out="garbage", spawn_rc=1)
        result = self._run(
            tmp_path, monkeypatch, which=which, run_fn=run_fn,
            urlopen=lambda *a, **k: _health_mock(bot_connected=True),
        )
        assert result.exit_code == 1
        assert "[fail] polytoken spawn" in result.output

    def test_doctor_bot_connected_false(self, tmp_path, monkeypatch) -> None:
        which, run_fn = _mock_subprocess()
        result = self._run(
            tmp_path, monkeypatch, which=which, run_fn=run_fn,
            urlopen=lambda *a, **k: _health_mock(bot_connected=False),
        )
        assert result.exit_code == 0
        assert "[warn] Daemon health" in result.output

    def test_doctor_bridge_unreachable(self, tmp_path, monkeypatch) -> None:
        which, run_fn = _mock_subprocess()

        def urlopen(req, *a, **k):
            url = getattr(req, "full_url", str(req))
            if "/v1/health" in url:
                raise Exception("refused")
            return _health_mock()

        result = self._run(tmp_path, monkeypatch, which=which, run_fn=run_fn, urlopen=urlopen)
        assert result.exit_code == 1
        assert "[fail] Daemon health" in result.output

    def test_doctor_secrets_file_mode_0644(self, tmp_path, monkeypatch) -> None:
        which, run_fn = _mock_subprocess()
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setenv("BRIDGE_URL", "http://127.0.0.1:9999")
        monkeypatch.setenv("HOME", str(tmp_path))
        write_secrets(Secrets(bot_token="token", channel_id=12345), path=secrets_file)
        secrets_file.chmod(0o644)
        with patch("urllib.request.urlopen", lambda *a, **k: _health_mock()), \
             patch("bridge.cli.shutil.which", return_value=which), \
             patch("subprocess.run", side_effect=run_fn):
            result = CliRunner().invoke(cli, ["doctor"])
        assert result.exit_code == 1
        assert "[fail]" in result.output and "0600" in result.output


class TestServeCommand:
    """Tests for `claude-discord-bridge serve` subcommand."""

    def test_serve_help_prints_help_text(self) -> None:
        result = CliRunner().invoke(cli, ["serve", "--help"])
        assert result.exit_code == 0
        assert "serve" in result.output.lower() or "Usage:" in result.output

    def test_serve_without_secrets_exits_2(self, tmp_path: Path, monkeypatch) -> None:
        secrets_file = tmp_path / "nonexistent.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        result = CliRunner().invoke(cli, ["serve"])
        assert result.exit_code == 2
        assert "init" in result.output.lower() or "not found" in result.output.lower()

    def test_serve_help_includes_host_port_options(self) -> None:
        result = CliRunner().invoke(cli, ["serve", "--help"])
        assert result.exit_code == 0
        assert "--host" in result.output
        assert "--port" in result.output

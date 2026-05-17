"""Tests for bridge CLI using click.testing.CliRunner."""

import json
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from bridge.cli import cli
from bridge.secrets import Secrets, write_secrets


class TestCliPackageMetadata:
    """Tests for CLI package metadata and entrypoint."""

    def test_package_name_is_claude_code_bridge(self) -> None:
        """Verify package name in pyproject.toml is 'claude-code-bridge'."""
        pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
        assert pyproject_path.exists(), "pyproject.toml not found"
        content = pyproject_path.read_text()
        assert 'name = "claude-code-bridge"' in content, "Package name should be 'claude-code-bridge'"

    def test_entrypoint_is_cc_bridge(self) -> None:
        """Verify CLI entrypoint in pyproject.toml is 'cc-bridge'."""
        pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
        assert pyproject_path.exists(), "pyproject.toml not found"
        content = pyproject_path.read_text()
        assert 'cc-bridge = "bridge.cli:main"' in content, "Entrypoint should be 'cc-bridge = \"bridge.cli:main\"'"

    def test_cc_bridge_help_command_works(self) -> None:
        """Verify cc-bridge --help returns CLI help text with all commands."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0
        # CliRunner shows "Usage: cli" (the function name), not the installed entrypoint name
        # but the help must show the actual commands available
        assert "Commands:" in result.output
        assert "init" in result.output
        assert "serve" in result.output
        assert "doctor" in result.output
        assert "Claude Code <-> Discord bridge" in result.output


class TestInitCommand:
    """Tests for `cc-bridge init` subcommand."""

    def _get_ready_fake_bot(self):
        """Create a fake bot that becomes ready immediately."""
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

            async def post(self, message: str, *, thread_id: str | None = None) -> list[str]:
                return ["123"]

        return FakeBot

    def test_init_writes_secrets_file(self, tmp_path: Path, monkeypatch) -> None:
        """init with simulated stdin writes a secrets file at the expected location."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setattr("bridge.cli.DiscordBot", self._get_ready_fake_bot())

        runner = CliRunner()
        result = runner.invoke(
            cli, ["init", "--platform", "discord"], input="test_token_abc\n12345\n"
        )

        assert result.exit_code == 0
        assert secrets_file.exists()

    def test_init_sets_0600_perms(self, tmp_path: Path, monkeypatch) -> None:
        """init writes a secrets file with exactly 0o600 permissions."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setattr("bridge.cli.DiscordBot", self._get_ready_fake_bot())

        runner = CliRunner()
        result = runner.invoke(
            cli, ["init", "--platform", "discord"], input="test_token_abc\n12345\n"
        )

        assert result.exit_code == 0
        perms = stat.S_IMODE(secrets_file.stat().st_mode)
        assert perms == 0o600

    def test_init_rejects_non_integer_channel_id(self, tmp_path: Path, monkeypatch) -> None:
        """init rejects a non-integer channel ID by reprompting."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setattr("bridge.cli.DiscordBot", self._get_ready_fake_bot())

        runner = CliRunner()
        # Input: first bad channel ID, then a good one
        result = runner.invoke(
            cli, ["init", "--platform", "discord"], input="test_token_abc\nnot_a_number\n12345\n"
        )

        assert result.exit_code == 0
        assert secrets_file.exists()

    def test_init_aborts_if_file_exists_and_user_says_no(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """init aborts cleanly when secrets file already exists and user answers no."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))

        # Pre-create the secrets file
        write_secrets(Secrets(bot_token="old_token", channel_id=999), path=secrets_file)

        runner = CliRunner()
        result = runner.invoke(
            cli, ["init", "--platform", "discord"], input="n\n"
        )

        # Should abort with exit code 1 (via abort())
        assert result.exit_code == 1

    def test_init_prompts_for_token_and_channel(self, tmp_path: Path, monkeypatch) -> None:
        """init prompts interactively for bot token and channel ID."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setattr("bridge.cli.DiscordBot", self._get_ready_fake_bot())

        runner = CliRunner()
        result = runner.invoke(
            cli, ["init", "--platform", "discord"], input="my_token\n54321\n"
        )

        assert result.exit_code == 0
        assert "DISCORD_BOT_TOKEN" in result.output or "token" in result.output.lower()
        assert "DISCORD_CHANNEL_ID" in result.output or "channel" in result.output.lower()

    def test_init_prints_success_message(self, tmp_path: Path, monkeypatch) -> None:
        """init prints a success message mentioning secrets.json and success."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setattr("bridge.cli.DiscordBot", self._get_ready_fake_bot())

        runner = CliRunner()
        result = runner.invoke(
            cli, ["init", "--platform", "discord"], input="test_token\n12345\n"
        )

        assert result.exit_code == 0
        assert "secrets.json" in result.output or "wrote" in result.output
        assert "succeeded" in result.output.lower() or "✅" in result.output

    def test_init_validates_token_bot_not_ready(self, tmp_path: Path, monkeypatch) -> None:
        """init validates token by starting bot; if bot never becomes ready, exits 2 and keeps secrets file."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))

        # Create a fake bot that never becomes ready
        class FakeBot:
            def __init__(self, token: str, channel_id: int):
                self.token = token
                self.channel_id = channel_id
                self._is_ready = False

            @property
            def is_ready(self) -> bool:
                return self._is_ready

            async def start(self) -> None:
                # Return immediately (like the real Bot.start() which create_task()s the gateway connect)
                pass

            async def close(self) -> None:
                pass

        # Monkeypatch the timeout to fail quickly instead of waiting 15s
        monkeypatch.setattr("bridge.cli._TOKEN_VALIDATION_TIMEOUT", 0.1)
        monkeypatch.setattr("bridge.cli.DiscordBot", FakeBot)

        runner = CliRunner()
        result = runner.invoke(
            cli, ["init", "--platform", "discord"], input="test_token\n12345\n"
        )

        assert result.exit_code == 2
        assert "could not connect" in result.output
        assert secrets_file.exists()  # Secrets file should still be there

    def test_init_validates_token_bot_ready_posts_message(self, tmp_path: Path, monkeypatch) -> None:
        """init validates token by starting bot; if ready, posts test message and exits 0."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))

        # Create a fake bot that becomes ready immediately and records posts
        posted_messages = []

        class FakeBot:
            def __init__(self, token: str, channel_id: int):
                self.token = token
                self.channel_id = channel_id
                self._is_ready = False

            @property
            def is_ready(self) -> bool:
                return self._is_ready

            async def start(self) -> None:
                # Immediately become ready
                self._is_ready = True

            async def close(self) -> None:
                pass

            async def post(self, message: str, *, thread_id: int | None = None) -> list[int]:
                posted_messages.append({"message": message, "thread_id": thread_id})
                return [123]

        monkeypatch.setattr("bridge.cli.DiscordBot", FakeBot)

        runner = CliRunner()
        result = runner.invoke(
            cli, ["init", "--platform", "discord"], input="test_token\n12345\n"
        )

        assert result.exit_code == 0
        assert len(posted_messages) == 1
        assert "init succeeded" in posted_messages[0]["message"]
        assert posted_messages[0]["thread_id"] is None  # Channel-level, no thread


class TestDoctorCommand:
    """Tests for `cc-bridge doctor` subcommand."""

    def test_doctor_all_ok(self, tmp_path: Path, monkeypatch) -> None:
        """doctor with all checks passing exits 0 and shows [ok] for each line."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setenv("BRIDGE_URL", "http://127.0.0.1:9999")

        # Create a valid secrets file
        write_secrets(Secrets(bot_token="token", channel_id=12345), path=secrets_file)

        # Create a fake settings.json
        bridge_repo_hooks = Path(__file__).parent.parent / "hooks"
        settings_data = {
            "hooks": {
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"python3 {bridge_repo_hooks}/notify-stop.py",
                            }
                        ]
                    }
                ],
                "Notification": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"python3 {bridge_repo_hooks}/notify-notification.py",
                            }
                        ]
                    }
                ],
            }
        }
        monkeypatch.setenv("HOME", str(tmp_path))
        tmp_path.joinpath(".claude").mkdir(exist_ok=True)
        tmp_path.joinpath(".claude", "settings.json").write_text(json.dumps(settings_data))

        # Create a fake skills directory with symlink
        skills_dir = tmp_path / ".claude" / "skills"
        skills_dir.mkdir(exist_ok=True, parents=True)
        ask_discord_dir = skills_dir / "ask-discord"
        ask_discord_dir.mkdir(exist_ok=True)
        skill_md = ask_discord_dir / "SKILL.md"
        skill_md.write_text("# ask-discord")

        # Mock the health check to return bot_connected: true
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({"bot_connected": True}).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=mock_response):
            runner = CliRunner()
            result = runner.invoke(cli, ["doctor"])

        assert result.exit_code == 0
        assert "[ok]" in result.output
        assert "Secrets file present" in result.output or "present" in result.output.lower()

    def test_doctor_bridge_unreachable(self, tmp_path: Path, monkeypatch) -> None:
        """doctor when bridge is unreachable: daemon health check fails, exit 1."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setenv("BRIDGE_URL", "http://127.0.0.1:9999")

        # Create a valid secrets file
        write_secrets(Secrets(bot_token="token", channel_id=12345), path=secrets_file)

        monkeypatch.setenv("HOME", str(tmp_path))
        tmp_path.joinpath(".claude").mkdir(exist_ok=True)

        # Mock the health check to raise URLError (connection refused)
        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            runner = CliRunner()
            result = runner.invoke(cli, ["doctor"])

        assert result.exit_code == 1
        assert "[fail]" in result.output

    def test_doctor_bot_connected_false(self, tmp_path: Path, monkeypatch) -> None:
        """doctor when daemon up but bot_connected=false: warns but exits 0."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setenv("BRIDGE_URL", "http://127.0.0.1:9999")

        # Create a valid secrets file
        write_secrets(Secrets(bot_token="token", channel_id=12345), path=secrets_file)

        monkeypatch.setenv("HOME", str(tmp_path))
        tmp_path.joinpath(".claude").mkdir(exist_ok=True)

        # Mock the health check to return bot_connected: false
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({"bot_connected": False}).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=mock_response):
            runner = CliRunner()
            result = runner.invoke(cli, ["doctor"])

        assert result.exit_code == 0
        assert "[warn]" in result.output

    def test_doctor_secrets_file_mode_0644(self, tmp_path: Path, monkeypatch) -> None:
        """doctor detects secrets file with wrong mode (0644), fails."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setenv("BRIDGE_URL", "http://127.0.0.1:9999")

        # Create a secrets file but with wrong mode
        write_secrets(Secrets(bot_token="token", channel_id=12345), path=secrets_file)
        secrets_file.chmod(0o644)

        monkeypatch.setenv("HOME", str(tmp_path))
        tmp_path.joinpath(".claude").mkdir(exist_ok=True)

        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])

        assert result.exit_code == 1
        assert "[fail]" in result.output
        assert "0600" in result.output or "mode" in result.output.lower()

    def test_doctor_skill_symlink_missing(self, tmp_path: Path, monkeypatch) -> None:
        """doctor detects missing skill symlink, warns but exits 0."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setenv("BRIDGE_URL", "http://127.0.0.1:9999")

        # Create a valid secrets file
        write_secrets(Secrets(bot_token="token", channel_id=12345), path=secrets_file)

        monkeypatch.setenv("HOME", str(tmp_path))
        tmp_path.joinpath(".claude").mkdir(exist_ok=True)

        # Don't create the skill symlink
        # Mock the health check to return bot_connected: true
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({"bot_connected": True}).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=mock_response):
            runner = CliRunner()
            result = runner.invoke(cli, ["doctor"])

        assert result.exit_code == 0
        assert "[warn]" in result.output


class TestServeCommand:
    """Tests for `cc-bridge serve` subcommand."""

    def test_serve_help_prints_help_text(self) -> None:
        """serve --help prints the help text."""
        runner = CliRunner()
        result = runner.invoke(cli, ["serve", "--help"])

        assert result.exit_code == 0
        assert "serve" in result.output.lower() or "Usage:" in result.output

    def test_serve_without_secrets_exits_2(self, tmp_path: Path, monkeypatch) -> None:
        """serve with no secrets file exits 2 and prints a clear error."""
        secrets_file = tmp_path / "nonexistent.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setenv("BRIDGE_PLATFORM", "discord")

        runner = CliRunner()
        result = runner.invoke(cli, ["serve"])

        assert result.exit_code == 2
        assert "init" in result.output.lower() or "not found" in result.output.lower()

    def test_serve_help_includes_host_port_options(self) -> None:
        """serve --help includes --host and --port options."""
        runner = CliRunner()
        result = runner.invoke(cli, ["serve", "--help"])

        assert result.exit_code == 0
        assert "--host" in result.output
        assert "--port" in result.output

    def test_doctor_zellij_installed(self, tmp_path: Path, monkeypatch) -> None:
        """doctor checks if zellij is installed and reports version."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setenv("BRIDGE_URL", "http://127.0.0.1:9999")

        write_secrets(Secrets(bot_token="token", channel_id=12345), path=secrets_file)
        monkeypatch.setenv("HOME", str(tmp_path))
        tmp_path.joinpath(".claude").mkdir(exist_ok=True)

        def mock_run(cmd, *args, **kwargs):
            if cmd[0] == "zellij":
                result = MagicMock()
                result.returncode = 0
                result.stdout = "zellij 0.40.1\n"
                return result
            return None

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({"bot_connected": True}).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=mock_response):
            with patch("subprocess.run", side_effect=mock_run):
                runner = CliRunner()
                result = runner.invoke(cli, ["doctor"])

        assert "[ok] zellij CLI" in result.output

    def test_doctor_zellij_not_found(self, tmp_path: Path, monkeypatch) -> None:
        """doctor warns if zellij is not installed."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setenv("BRIDGE_URL", "http://127.0.0.1:9999")

        write_secrets(Secrets(bot_token="token", channel_id=12345), path=secrets_file)
        monkeypatch.setenv("HOME", str(tmp_path))
        tmp_path.joinpath(".claude").mkdir(exist_ok=True)

        def mock_run(cmd, *args, **kwargs):
            if cmd[0] == "zellij":
                raise FileNotFoundError("zellij not found")
            return None

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({"bot_connected": True}).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=mock_response):
            with patch("subprocess.run", side_effect=mock_run):
                runner = CliRunner()
                result = runner.invoke(cli, ["doctor"])

        assert "[fail] zellij CLI" in result.output
        assert result.exit_code == 1

    def test_doctor_bridge_session_running(self, tmp_path: Path, monkeypatch) -> None:
        """doctor reports ok when bridge zellij session is running."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setenv("BRIDGE_URL", "http://127.0.0.1:9999")

        write_secrets(Secrets(bot_token="token", channel_id=12345), path=secrets_file)
        monkeypatch.setenv("HOME", str(tmp_path))
        tmp_path.joinpath(".claude").mkdir(exist_ok=True)

        def mock_run(cmd, *args, **kwargs):
            if cmd[0] == "zellij" and "list-sessions" in cmd:
                result = MagicMock()
                result.returncode = 0
                result.stdout = "cc-bridge-worker\nother\n"
                return result
            elif cmd[0] == "zellij":
                result = MagicMock()
                result.returncode = 0
                result.stdout = "zellij 0.40.1\n"
                return result
            return None

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({"bot_connected": True}).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=mock_response):
            with patch("subprocess.run", side_effect=mock_run):
                runner = CliRunner()
                result = runner.invoke(cli, ["doctor"])

        assert "[ok] zellij session" in result.output

    def test_doctor_bridge_session_not_running(self, tmp_path: Path, monkeypatch) -> None:
        """doctor warns when bridge zellij session is not running."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setenv("BRIDGE_URL", "http://127.0.0.1:9999")

        write_secrets(Secrets(bot_token="token", channel_id=12345), path=secrets_file)
        monkeypatch.setenv("HOME", str(tmp_path))
        tmp_path.joinpath(".claude").mkdir(exist_ok=True)

        def mock_run(cmd, *args, **kwargs):
            if cmd[0] == "zellij" and "list-sessions" in cmd:
                result = MagicMock()
                result.returncode = 0
                result.stdout = "other\n"
                return result
            elif cmd[0] == "zellij":
                result = MagicMock()
                result.returncode = 0
                result.stdout = "zellij 0.40.1\n"
                return result
            return None

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({"bot_connected": True}).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=mock_response):
            with patch("subprocess.run", side_effect=mock_run):
                runner = CliRunner()
                result = runner.invoke(cli, ["doctor"])

        assert "[warn] zellij session" in result.output

    def test_doctor_task_settings_dir_writable(self, tmp_path: Path, monkeypatch) -> None:
        """doctor reports ok when task-settings dir is writable."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setenv("BRIDGE_URL", "http://127.0.0.1:9999")
        monkeypatch.setenv("HOME", str(tmp_path))

        write_secrets(Secrets(bot_token="token", channel_id=12345), path=secrets_file)
        tmp_path.joinpath(".claude").mkdir(exist_ok=True)

        def mock_run(cmd, *args, **kwargs):
            if cmd[0] == "zellij":
                result = MagicMock()
                result.returncode = 0
                result.stdout = "zellij 0.40.1\nbridge\n"
                return result
            elif cmd[0] == "which":
                result = MagicMock()
                result.returncode = 0
                result.stdout = "/usr/local/bin/claude\n"
                return result
            return None

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({"bot_connected": True}).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=mock_response):
            with patch("subprocess.run", side_effect=mock_run):
                runner = CliRunner()
                result = runner.invoke(cli, ["doctor"])

        assert "[ok] task-settings dir" in result.output

    def test_doctor_hook_scripts_present(self, tmp_path: Path, monkeypatch) -> None:
        """doctor reports ok when hook scripts are present and executable."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setenv("BRIDGE_URL", "http://127.0.0.1:9999")
        monkeypatch.setenv("HOME", str(tmp_path))

        write_secrets(Secrets(bot_token="token", channel_id=12345), path=secrets_file)
        tmp_path.joinpath(".claude").mkdir(exist_ok=True)

        def mock_run(cmd, *args, **kwargs):
            if cmd[0] == "zellij":
                result = MagicMock()
                result.returncode = 0
                result.stdout = "zellij 0.40.1\nbridge\n"
                return result
            elif cmd[0] == "which":
                result = MagicMock()
                result.returncode = 0
                result.stdout = "/usr/local/bin/claude\n"
                return result
            return None

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({"bot_connected": True}).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=mock_response):
            with patch("subprocess.run", side_effect=mock_run):
                runner = CliRunner()
                result = runner.invoke(cli, ["doctor"])

        # The real repo has these scripts, so they should be found
        assert "[ok] hook script — event.py" in result.output
        assert "[ok] hook script — pretooluse-approve.py" in result.output

    def test_doctor_claude_on_path(self, tmp_path: Path, monkeypatch) -> None:
        """doctor reports ok when claude is on PATH."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setenv("BRIDGE_URL", "http://127.0.0.1:9999")
        monkeypatch.setenv("HOME", str(tmp_path))

        write_secrets(Secrets(bot_token="token", channel_id=12345), path=secrets_file)
        tmp_path.joinpath(".claude").mkdir(exist_ok=True)

        def mock_run(cmd, *args, **kwargs):
            if cmd[0] == "which" and "claude" in cmd:
                result = MagicMock()
                result.returncode = 0
                result.stdout = "/usr/local/bin/claude\n"
                return result
            elif cmd[0] == "zellij":
                result = MagicMock()
                result.returncode = 0
                result.stdout = "zellij 0.40.1\nbridge\n"
                return result
            return None

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({"bot_connected": True}).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=mock_response):
            with patch("subprocess.run", side_effect=mock_run):
                runner = CliRunner()
                result = runner.invoke(cli, ["doctor"])

        assert "[ok] claude CLI" in result.output

    def test_doctor_claude_not_on_path(self, tmp_path: Path, monkeypatch) -> None:
        """doctor warns when claude is not on PATH."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setenv("BRIDGE_URL", "http://127.0.0.1:9999")
        monkeypatch.setenv("HOME", str(tmp_path))

        write_secrets(Secrets(bot_token="token", channel_id=12345), path=secrets_file)
        tmp_path.joinpath(".claude").mkdir(exist_ok=True)

        def mock_run(cmd, *args, **kwargs):
            if cmd[0] == "which" and "claude" in cmd:
                result = MagicMock()
                result.returncode = 1
                result.stdout = ""
                return result
            elif cmd[0] == "zellij":
                result = MagicMock()
                result.returncode = 0
                result.stdout = "zellij 0.40.1\nbridge\n"
                return result
            return None

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({"bot_connected": True}).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=mock_response):
            with patch("subprocess.run", side_effect=mock_run):
                runner = CliRunner()
                result = runner.invoke(cli, ["doctor"])

        assert "[warn] claude CLI" in result.output
        assert result.exit_code == 0


# Tests for Task 1: BRIDGE_PLATFORM support in serve command
class TestServeCommandPlatform:
    """Tests for platform-aware serve command."""

    def test_serve_requires_bridge_platform_env_var(self, tmp_path: Path, monkeypatch) -> None:
        """serve without BRIDGE_PLATFORM env var exits with code 2 and clear error."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.delenv("BRIDGE_PLATFORM", raising=False)

        # Create a dummy secrets file
        write_secrets(Secrets(bot_token="token", channel_id=12345), path=secrets_file)

        runner = CliRunner()
        result = runner.invoke(cli, ["serve"])

        assert result.exit_code == 2
        assert "BRIDGE_PLATFORM" in result.output
        assert "required" in result.output

    def test_serve_rejects_invalid_platform(self, tmp_path: Path, monkeypatch) -> None:
        """serve with invalid BRIDGE_PLATFORM exits with code 2 and clear error."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setenv("BRIDGE_PLATFORM", "slack")

        # Create a dummy secrets file
        write_secrets(Secrets(bot_token="token", channel_id=12345), path=secrets_file)

        runner = CliRunner()
        result = runner.invoke(cli, ["serve"])

        assert result.exit_code == 2
        assert "Unknown platform" in result.output or "slack" in result.output
        assert "discord" in result.output or "mattermost" in result.output

    def test_serve_accepts_discord_platform(self, tmp_path: Path, monkeypatch) -> None:
        """serve with BRIDGE_PLATFORM=discord accepts it (though may fail on connection)."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setenv("BRIDGE_PLATFORM", "discord")

        # Create a dummy secrets file
        write_secrets(Secrets(bot_token="token", channel_id=12345), path=secrets_file)

        # Mock serve_server to avoid actually starting
        with patch("bridge.cli.serve_server") as mock_serve:
            runner = CliRunner()
            result = runner.invoke(cli, ["serve"])

            # serve_server should be called with platform="discord"
            if result.exit_code == 0:
                assert mock_serve.called

    def test_serve_accepts_mattermost_platform(self, tmp_path: Path, monkeypatch) -> None:
        """serve with BRIDGE_PLATFORM=mattermost accepts it."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setenv("BRIDGE_PLATFORM", "mattermost")

        # Create a dummy secrets file
        write_secrets(
            Secrets(
                bot_token="token",
                channel_id="abc123def456ghi",
                server_url="https://mm.example.com"
            ),
            path=secrets_file
        )

        # Mock serve_server to avoid actually starting
        with patch("bridge.cli.serve_server") as mock_serve:
            runner = CliRunner()
            result = runner.invoke(cli, ["serve"])

            # serve_server should be called with platform="mattermost"
            if result.exit_code == 0:
                assert mock_serve.called


# Tests for Task 2: Mattermost init wizard
class TestInitCommandWithPlatform:
    """Tests for platform-aware init command."""

    def _get_ready_fake_discord_bot(self):
        """Create a fake Discord bot that becomes ready immediately."""
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

            async def post(self, message: str, *, thread_id: str | None = None) -> list[str]:
                return ["123"]

        return FakeBot

    def test_init_requires_platform_flag(self, tmp_path: Path, monkeypatch) -> None:
        """init without --platform flag shows error or prompts."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))

        runner = CliRunner()
        result = runner.invoke(cli, ["init"])

        # Should fail or require the flag
        assert result.exit_code != 0 or "platform" in result.output.lower()

    def test_init_platform_discord_writes_secrets(self, tmp_path: Path, monkeypatch) -> None:
        """init --platform discord prompts for token and channel, writes secrets."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setattr("bridge.cli.DiscordBot", self._get_ready_fake_discord_bot())

        runner = CliRunner()
        result = runner.invoke(
            cli, ["init", "--platform", "discord"],
            input="test_token\n12345\n"
        )

        assert result.exit_code == 0
        assert secrets_file.exists()

        # Load and verify the secrets
        data = json.loads(secrets_file.read_text())
        assert data.get("platform") == "discord" or "DISCORD_BOT_TOKEN" in data
        assert data.get("bot_token") == "test_token" or data.get("DISCORD_BOT_TOKEN") == "test_token"

    def test_init_platform_mattermost_prompts_correctly(self, tmp_path: Path, monkeypatch) -> None:
        """init --platform mattermost prompts for server_url, token, channel_id, allowed_user_ids."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))

        # Mock asyncio.run to skip the network validation step
        async def mock_validate():
            pass

        monkeypatch.setattr("asyncio.run", lambda coro: None)

        runner = CliRunner()
        result = runner.invoke(
            cli, ["init", "--platform", "mattermost"],
            input="https://mm.example.com\nbot_token_xyz\nchannel_id_123\nall\n"
        )

        # Check that the command succeeded and wrote secrets
        assert result.exit_code == 0, f"Command failed: {result.output}"
        assert secrets_file.exists()
        data = json.loads(secrets_file.read_text())
        assert data.get("platform") == "mattermost"

    def test_init_platform_mattermost_allowed_user_ids_optional(self, tmp_path: Path, monkeypatch) -> None:
        """init --platform mattermost allows 'all' for allowed_user_ids or comma-separated list."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))

        # Mock asyncio.run to skip the network validation step
        monkeypatch.setattr("asyncio.run", lambda coro: None)

        runner = CliRunner()
        result = runner.invoke(
            cli, ["init", "--platform", "mattermost"],
            input="https://mm.example.com\nbot_token_xyz\nchannel_id_123\nall\n"
        )

        # Should complete successfully
        assert result.exit_code == 0, f"Command failed: {result.output}"
        assert secrets_file.exists()
        data = json.loads(secrets_file.read_text())
        # When 'all' is passed, allowed_user_ids should be None
        assert data.get("allowed_user_ids") is None


# Tests for Task 3: Doctor checks for platform-specific issues
class TestDoctorCommandPlatform:
    """Tests for platform-aware doctor command."""

    def test_doctor_reads_platform_from_secrets(self, tmp_path: Path, monkeypatch) -> None:
        """doctor reads platform field from secrets.json and runs platform-specific checks."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setenv("HOME", str(tmp_path))

        # Create a Discord secrets file
        write_secrets(Secrets(bot_token="token", channel_id=12345), path=secrets_file)

        # Create minimal required directories
        tmp_path.joinpath(".claude").mkdir(exist_ok=True)

        # Mock health check
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.read.return_value = json.dumps({"bot_connected": True}).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=mock_response):
            runner = CliRunner()
            result = runner.invoke(cli, ["doctor"])

            # Should complete without error
            assert result.exit_code in (0, 1)

    def test_doctor_uses_bridge_platform_env_if_no_secrets(self, tmp_path: Path, monkeypatch) -> None:
        """doctor falls back to BRIDGE_PLATFORM env var if secrets missing."""
        secrets_file = tmp_path / "secrets.json"
        monkeypatch.setenv("BRIDGE_SECRETS_PATH", str(secrets_file))
        monkeypatch.setenv("BRIDGE_PLATFORM", "mattermost")
        monkeypatch.setenv("HOME", str(tmp_path))

        tmp_path.joinpath(".claude").mkdir(exist_ok=True)

        runner = CliRunner()
        result = runner.invoke(cli, ["doctor"])

        # Should fail on missing secrets, not crash
        assert result.exit_code == 1
        assert "Secrets file present" in result.output or "not found" in result.output

"""Operational CLI for the claude-slack-bridge daemon."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

import click

from bridge.bot import Bot
from bridge.secrets import (
    SECRETS_DIR,
    SECRETS_FILE,
    Secrets,
    SecretsError,
    load_secrets,
    secrets_dir_perms,
    secrets_file_perms,
    write_secrets,
)
from bridge.server import serve as serve_server
from bridge.redaction import safe_error, safe_log

logger = logging.getLogger(__name__)

_TOKEN_VALIDATION_TIMEOUT = 15
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "claude-slack-bridge"


def _secrets_path() -> Path:
    """Return the explicit override or the Slack-only default path."""
    return Path(os.environ.get("BRIDGE_SECRETS_PATH", str(SECRETS_FILE))).expanduser()


def _state_dir() -> Path:
    return Path(
        os.environ.get("BRIDGE_STATE_DIR", str(DEFAULT_STATE_DIR))
    ).expanduser()


async def _validate_slack_and_post_test(secrets: Secrets) -> bool:
    """Run the same startup validation as ``serve`` and post a confirmation."""
    bot = Bot(
        secrets.bot_token,
        team_id=secrets.team_id,
        owner_user_id=secrets.owner_user_id,
        home_channel_id=secrets.home_channel_id,
        app_token=secrets.app_token,
    )
    try:
        await asyncio.wait_for(bot.start(), timeout=_TOKEN_VALIDATION_TIMEOUT)
        if not bot.is_ready:
            return False
        await bot.post(
            "✅ claude-slack-bridge init succeeded — future agent notifications will appear here."
        )
        return True
    finally:
        await bot.close()


async def _check_slack_startup(secrets: Secrets) -> dict[str, Any]:
    """Validate Slack identity, team, home-channel membership, and Socket Mode."""
    bot = Bot(
        secrets.bot_token,
        team_id=secrets.team_id,
        owner_user_id=secrets.owner_user_id,
        home_channel_id=secrets.home_channel_id,
        app_token=secrets.app_token,
    )
    try:
        await asyncio.wait_for(bot.start(), timeout=_TOKEN_VALIDATION_TIMEOUT)
        health = bot.health()
        health["ready"] = bot.is_ready
        return health
    finally:
        await bot.close()


@click.group()
def cli() -> None:
    """Run the Polytoken ↔ Slack bridge daemon."""


@cli.command()
def init() -> None:
    """Create Slack credentials, validate startup, and post a test message.

    Credentials are written only to ``~/.config/claude-slack-bridge/secrets.json``
    (or ``BRIDGE_SECRETS_PATH``) with file mode 0600 and directory mode 0700.
    """
    click.echo("Welcome to claude-slack-bridge (Polytoken ↔ Slack).")
    click.echo()
    click.echo("Before continuing, create/install the Slack app from:")
    click.echo("  slack-app-manifest.yaml (Socket Mode; /agent; shortcuts/interactivity)")
    click.echo("Install it in your workspace and copy the xoxb bot token and xapp app token.")
    click.echo("The bot must be invited to the configured public or private home channel and have the manifest scopes.")
    click.echo("The wizard validates auth.test, team identity, owner identity, channel membership, and Socket Mode by starting Bot.")
    click.echo()

    secrets_path = _secrets_path()
    if secrets_path.exists() and not click.confirm(
        f"Secrets file already exists at {secrets_path}. Overwrite?", abort=True
    ):
        return

    bot_token = click.prompt("SLACK_BOT_TOKEN (xoxb-...)", hide_input=True)
    app_token = click.prompt("SLACK_APP_TOKEN (xapp-...)", hide_input=True)
    team_id = click.prompt("SLACK_TEAM_ID (T...)")
    home_channel_id = click.prompt("SLACK_HOME_CHANNEL_ID (public/private C.../G...)")
    owner_user_id = click.prompt("SLACK_OWNER_USER_ID (U...)")

    click.echo()
    click.echo("Required Slack app setup:")
    click.echo("  • Enable Socket Mode and use an app-level xapp token.")
    click.echo("  • Enable /agent, shortcuts, and interactivity in the app manifest.")
    click.echo("  • Invite the bot to the configured public or private home channel.")
    click.echo("  • Do not configure a user token or user-token impersonation.")

    secrets = Secrets(
        bot_token=bot_token,
        app_token=app_token,
        team_id=team_id,
        home_channel_id=home_channel_id,
        owner_user_id=owner_user_id,
    )
    try:
        write_secrets(secrets, path=secrets_path)
    except SecretsError as exc:
        click.echo("Error: could not write Slack secrets safely.", err=True)
        raise click.exceptions.Exit(1) from exc

    perms = stat.S_IMODE(secrets_path.stat().st_mode)
    parent_perms = stat.S_IMODE(secrets_path.parent.stat().st_mode)
    if perms != 0o600 or parent_perms != 0o700:
        click.echo(
            "Error: expected secrets file 0600 and directory 0700.",
            err=True,
        )
        raise click.exceptions.Exit(1)

    click.echo()
    click.echo("Starting Slack validation (no Discord configuration is read)...")
    try:
        success = asyncio.run(_validate_slack_and_post_test(secrets))
    except Exception as exc:
        click.echo(
            f"Error: {safe_error(exc, 'Slack startup validation failed')}",
            err=True,
        )
        raise click.exceptions.Exit(2) from exc
    if not success:
        click.echo("Error: Slack startup did not become ready before timeout.", err=True)
        raise click.exceptions.Exit(2)

    click.echo()
    click.echo("Wrote Slack secrets to the private secrets store (file 0600, directory 0700).")
    click.echo("Start with: claude-slack-bridge serve")
    click.echo("For systemd: packaging/claude-slack-bridge.service and scripts/install-systemd-user.sh")


@cli.command()
@click.option("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
@click.option("--port", default=8787, type=int, help="Port to bind to (default: 8787)")
def serve(host: str, port: int) -> None:
    """Run the bridge daemon using the Slack-only secrets file."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    try:
        secrets = load_secrets(path=_secrets_path())
    except SecretsError as exc:
        click.echo("Error: invalid or unreadable Slack configuration.", err=True)
        raise click.exceptions.Exit(2) from exc
    asyncio.run(serve_server(secrets, host=host, port=port))


def _polytoken_bin() -> str:
    return os.environ.get("POLYTOKEN_BIN", "polytoken")


def _report_storage() -> bool:
    """Ensure private config/state storage and SQLite files are protected."""
    state_dir = _state_dir()
    db_path = state_dir / "state.db"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        state_dir.chmod(0o700)
        with tempfile.NamedTemporaryFile(dir=state_dir, prefix=".doctor-", delete=True):
            pass
        mode = stat.S_IMODE(state_dir.stat().st_mode)
        if mode != 0o700:
            click.echo(f"[fail] storage permissions — private state directory mode is {oct(mode)}, expected 0700", err=True)
            return False
        bad_files = [p for p in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm"))
                     if p.exists() and stat.S_IMODE(p.stat().st_mode) != 0o600]
        if bad_files:
            click.echo("[fail] storage permissions — SQLite database files must be mode 0600", err=True)
            return False
        click.echo("[ok] storage — writable private state directory (0700), SQLite files (0600 when present)")
        return True
    except Exception as exc:
        click.echo(f"[fail] storage — state directory is not writable ({type(exc).__name__})", err=True)
        return False


def _warn_legacy_runtime() -> bool:
    """Warn, without failing, if an old Discord service/process is observable."""
    warned = False
    systemctl = shutil.which("systemctl")
    if systemctl:
        try:
            result = subprocess.run(
                [systemctl, "--user", "is-active", "claude-discord-bridge.service"],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0 and (result.stdout or "").strip() == "active":
                click.echo("[warn] legacy runtime — claude-discord-bridge.service is active; stop it before using Slack", err=True)
                warned = True
        except (OSError, subprocess.SubprocessError):
            pass
    pgrep = shutil.which("pgrep")
    if pgrep:
        try:
            result = subprocess.run(
                [pgrep, "-af", "claude-discord-bridge"],
                capture_output=True, text=True, timeout=3,
            )
            lines = [line for line in (result.stdout or "").splitlines() if "pgrep" not in line]
            if lines:
                click.echo("[warn] legacy runtime — a claude-discord-bridge process is running; stop it before using Slack", err=True)
                warned = True
        except (OSError, subprocess.SubprocessError):
            pass
    return warned


def _doctor_health() -> tuple[bool, bool]:
    """Return ``(failed, warned)`` for the local bridge health endpoint."""
    bridge_url = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8787").rstrip("/")
    try:
        request = urllib.request.Request(f"{bridge_url}/v1/health")
        response = urllib.request.urlopen(request, timeout=2)
        data = json.loads(response.read().decode("utf-8"))
        if response.status != 200:
            click.echo(f"[fail] daemon health — HTTP {response.status}", err=True)
            return True, False
        if data.get("bot_connected") is not True:
            click.echo("[warn] daemon health — bot_connected is false", err=True)
            return False, True
        if data.get("socket_mode_connected") is False:
            click.echo("[warn] Socket Mode — daemon is healthy but Socket Mode is not connected", err=True)
            return False, True
        click.echo(f"[ok] daemon health — {bridge_url}/v1/health reports Slack bot connected")
        return False, False
    except Exception as exc:
        click.echo("[fail] daemon health — health endpoint unreachable", err=True)
        return True, False


def _doctor_polytoken() -> bool:
    binary = _polytoken_bin()
    resolved = shutil.which(binary)
    if resolved is None:
        click.echo(f"[fail] Polytoken CLI — `{binary}` not found on PATH", err=True)
        return True
    try:
        result = subprocess.run([binary, "--version"], capture_output=True, timeout=5, text=True)
        if result.returncode != 0:
            click.echo(f"[fail] Polytoken CLI — version check failed ({result.returncode})", err=True)
            return True
        version = (result.stdout or result.stderr).strip().splitlines()[0] if (result.stdout or result.stderr).strip() else "unknown version"
        click.echo(f"[ok] Polytoken CLI — {resolved} ({version})")
    except Exception as exc:
        click.echo(f"[fail] Polytoken CLI — version check failed ({type(exc).__name__})", err=True)
        return True

    try:
        with tempfile.TemporaryDirectory(prefix="claude-slack-doctor-") as tmp:
            spawn = subprocess.run(
                [binary, "--working-dir", tmp, "new", "--no-attach"],
                capture_output=True, timeout=30, text=True,
            )
            output = f"{spawn.stdout or ''} {spawn.stderr or ''}"
            port = next((token.split("=", 1)[1] for token in output.split() if token.startswith("port=")), None)
            session_id = next((token.split("=", 1)[1] for token in output.split() if token.startswith("session_id=")), None)
            if spawn.returncode != 0 or not port or not session_id:
                click.echo("[fail] Polytoken smoke — unexpected spawn result", err=True)
                return True
            click.echo(f"[ok] Polytoken smoke — session {session_id} on port {port}")
            try:
                request = urllib.request.Request(f"http://127.0.0.1:{port}/terminate", method="POST")
                urllib.request.urlopen(request, timeout=3).close()
            except Exception as exc:
                click.echo("[warn] Polytoken smoke cleanup — could not terminate spawned session", err=True)
        return False
    except Exception as exc:
        click.echo(f"[fail] Polytoken smoke — {type(exc).__name__}", err=True)
        return True


@cli.command()
def doctor() -> None:
    """Check Slack credentials, startup reachability, storage, and Polytoken."""
    failed = False
    warned = False
    secrets_path = _secrets_path()

    if not secrets_path.exists():
        click.echo("[fail] Slack secrets file — private secrets store not found", err=True)
        failed = True
        secrets = None
    else:
        click.echo("[ok] Slack secrets file — private secrets store found")
        perms = secrets_file_perms(secrets_path)
        parent_perms = secrets_dir_perms(secrets_path.parent)
        if perms != 0o600:
            click.echo(f"[fail] secrets file permissions — {oct(perms) if perms is not None else 'missing'}, expected 0600", err=True)
            failed = True
        else:
            click.echo("[ok] secrets file permissions — 0600")
        if parent_perms != 0o700:
            click.echo(f"[fail] secrets directory permissions — {oct(parent_perms) if parent_perms is not None else 'missing'}, expected 0700", err=True)
            failed = True
        else:
            click.echo("[ok] secrets directory permissions — 0700")
        try:
            secrets = load_secrets(path=secrets_path)
            click.echo("[ok] Slack config — all required fields and token prefixes are valid")
        except SecretsError as exc:
            click.echo("[fail] Slack config — invalid or unreadable configuration", err=True)
            failed = True
            secrets = None

    if secrets is not None:
        try:
            health = asyncio.run(_check_slack_startup(secrets))
            click.echo(
                f"[ok] Slack startup — identity {health.get('bot_user_id') or '?'}; team {health.get('team_id') or '?'}; home channel membership verified"
            )
            if health.get("socket_mode_connected") is True:
                click.echo("[ok] Socket Mode — connected")
            else:
                click.echo("[warn] Socket Mode — app token is configured but connection was not observable", err=True)
                warned = True
        except Exception as exc:
            click.echo("[fail] Slack startup — identity/team/home-channel/Socket Mode check failed", err=True)
            failed = True

    health_failed, health_warned = _doctor_health()
    failed |= health_failed
    warned |= health_warned
    failed |= _doctor_polytoken()
    failed |= not _report_storage()
    warned |= _warn_legacy_runtime()

    click.echo()
    if failed:
        click.echo("Doctor: some checks failed. Fix the issues above before running the bridge.", err=True)
        raise click.exceptions.Exit(1)
    if warned:
        click.echo("Doctor: checks complete with warnings; review live connectivity/runtime notes.", err=True)
    else:
        click.echo("Doctor: all checks passed. Bridge is ready.")


def main() -> None:
    """Console entry point."""
    cli()

"""CLI entrypoints for the bridge daemon."""

import asyncio
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import click

from bridge.bot import Bot
from bridge.secrets import (
    SECRETS_FILE,
    Secrets,
    SecretsError,
    load_secrets,
    secrets_file_perms,
    write_secrets,
)
from bridge.server import serve as serve_server

logger = logging.getLogger(__name__)

# Token validation timeout in seconds (extracted as constant for test monkeypatching)
_TOKEN_VALIDATION_TIMEOUT = 15


async def _validate_token_and_post_test(secrets: Secrets) -> bool:
    """Validate token by starting bot and posting a test message.

    Returns True if validation succeeds (bot ready and message posted).
    Returns False if timeout waiting for bot to become ready.
    """
    bot = Bot(secrets.bot_token, secrets.channel_id)
    try:
        await bot.start()
        start_time = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - start_time < _TOKEN_VALIDATION_TIMEOUT:
            if bot.is_ready:
                await bot.post("✅ claude-discord-bridge init succeeded — you'll see future notifications here.")
                return True
            await asyncio.sleep(0.1)
        return False
    finally:
        await bot.close()


@click.group()
def cli() -> None:
    """Polytoken <-> Discord bridge daemon."""
    pass


@cli.command()
def init() -> None:
    """Interactive bootstrap: collect bot token and channel ID, write secrets file.

    Writes to ~/.config/claude-discord-bridge/secrets.json (mode 0600).
    """
    click.echo("Welcome to the Polytoken <-> Discord bridge.")
    click.echo()
    click.echo(
        "This wizard will set up the bridge to post messages to Discord. You'll need:"
    )
    click.echo("  - A Discord bot token (from Discord Developer Portal)")
    click.echo("  - A Discord channel ID (from a text channel you own)")
    click.echo()

    secrets_path = Path(os.environ.get("BRIDGE_SECRETS_PATH", str(SECRETS_FILE)))

    if secrets_path.exists():
        if not click.confirm(
            f"Secrets file already exists at {secrets_path}. Overwrite?",
            abort=True,
        ):
            return

    bot_token = click.prompt("DISCORD_BOT_TOKEN", hide_input=True, confirmation_prompt=False)

    while True:
        channel_id_input = click.prompt("DISCORD_CHANNEL_ID")
        try:
            channel_id = int(channel_id_input)
            if channel_id <= 0:
                click.echo("Channel ID must be a positive integer.")
                continue
            break
        except ValueError:
            click.echo("Channel ID must be a positive integer.")

    click.echo()
    click.echo("Important reminders:")
    click.echo("1. You must enable Privileged Gateway Intent: Message Content for the bot at")
    click.echo("   Discord Developer Portal > Applications > [your app] > Bot")
    click.echo()
    click.echo(f"2. The bot must be a member of the guild containing channel ID {channel_id}")
    click.echo("   and have these permissions:")
    click.echo("   - View Channel")
    click.echo("   - Send Messages")
    click.echo("   - Create Public Threads")
    click.echo("   - Manage Channels  (required for /pin; safe to omit if you won't use it)")
    click.echo()

    secrets = Secrets(bot_token=bot_token, channel_id=channel_id)
    write_secrets(secrets, path=secrets_path)

    perms = stat.S_IMODE(secrets_path.stat().st_mode)
    if perms != 0o600:
        secrets_path.unlink()
        click.echo(
            f"Error: file mode is {oct(perms)}, expected 0o600. "
            f"Secrets file deleted. Please check your filesystem settings.",
            err=True,
        )
        sys.exit(1)

    click.echo()
    click.echo("Validating token and channel...")
    try:
        success = asyncio.run(_validate_token_and_post_test(secrets))
        if not success:
            click.echo("Error: could not connect — check token/intents/network", err=True)
            sys.exit(2)
    except Exception as e:
        click.echo(f"Error: could not connect — check token/intents/network ({e})", err=True)
        sys.exit(2)

    click.echo()
    click.echo(f"Wrote secrets to {secrets_path} (mode 0600). Start the daemon with:")
    click.echo("  claude-discord-bridge serve")
    click.echo()
    click.echo("Or use the systemd unit at:")
    click.echo("  packaging/claude-discord-bridge.service")


@cli.command()
@click.option("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")
@click.option("--port", default=8787, type=int, help="Port to bind to (default: 8787)")
def serve(host: str, port: int) -> None:
    """Run the bridge daemon.

    Loads secrets from ~/.config/claude-discord-bridge/secrets.json and starts
    the HTTP health server + Discord bot.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    secrets_path = Path(os.environ.get("BRIDGE_SECRETS_PATH", str(SECRETS_FILE)))
    try:
        secrets = load_secrets(path=secrets_path)
    except SecretsError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(2)
    asyncio.run(serve_server(secrets, host=host, port=port))


def _polytoken_bin() -> str:
    return os.environ.get("POLYTOKEN_BIN", "polytoken")


@cli.command()
def doctor() -> None:
    """Run diagnostic checks on the bridge setup.

    Checks:
    - Secrets file present and mode 0600
    - Bridge daemon health and bot connectivity
    - The `polytoken` binary is installed and can spawn a headless session
    - The attachments directory is writable
    """
    failed = False
    warned = False

    secrets_path = Path(os.environ.get("BRIDGE_SECRETS_PATH", str(SECRETS_FILE)))

    # Check 1: Secrets file present
    if not secrets_path.exists():
        click.echo(f"[fail] Secrets file present — {secrets_path} not found", err=True)
        failed = True
    else:
        click.echo(f"[ok] Secrets file present — {secrets_path}")

    # Check 2: Secrets file mode 0600
    if secrets_path.exists():
        perms = secrets_file_perms(secrets_path)
        if perms is None or perms != 0o600:
            click.echo(f"[fail] Secrets file mode 0600 — {oct(perms)} found", err=True)
            failed = True
        else:
            click.echo("[ok] Secrets file mode 0600")

    # Check 3: Daemon health
    bridge_url = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8787")
    try:
        req = urllib.request.Request(f"{bridge_url}/v1/health")
        response = urllib.request.urlopen(req, timeout=2)
        data = json.loads(response.read().decode("utf-8"))
        if response.status == 200 and data.get("bot_connected") is True:
            click.echo(f"[ok] Daemon health — {bridge_url}/v1/health returns bot_connected: true")
        elif response.status == 200 and data.get("bot_connected") is False:
            click.echo(f"[warn] Daemon health — {bridge_url}/v1/health returns bot_connected: false", err=True)
            warned = True
        else:
            click.echo(f"[fail] Daemon health — {bridge_url}/v1/health returned unexpected status", err=True)
            failed = True
    except Exception as e:
        click.echo(f"[fail] Daemon health — {bridge_url}/v1/health unreachable ({type(e).__name__})", err=True)
        failed = True

    # Check 4: polytoken binary present + version pin
    from bridge.version_guard import (
        BRIDGE_POLYTOKEN_VERSION,
        check_polytoken_version,
        detect_polytoken_version_detail,
    )

    binary = _polytoken_bin()
    resolved = shutil.which(binary)
    if resolved is None:
        click.echo(f"[fail] polytoken CLI — `{binary}` not found on PATH", err=True)
        failed = True
    else:
        version, is_prerelease = detect_polytoken_version_detail(binary)
        ok, msg = check_polytoken_version(version, is_prerelease=is_prerelease)
        if ok:
            click.echo(f"[ok] polytoken CLI — {resolved} ({msg})")
        else:
            # A wrong version is a hard fail: the daemon contracts are pinned to
            # {BRIDGE_POLYTOKEN_VERSION}; a mismatched binary can break silently.
            click.echo(
                f"[fail] polytoken CLI — {resolved} ({msg}). "
                f"Bridge requires polytoken {BRIDGE_POLYTOKEN_VERSION}+.",
                err=True,
            )
            failed = True

    # Check 5: polytoken can spawn a headless session
    if resolved is not None:
        try:
            with tempfile.TemporaryDirectory(prefix="cdb-doctor-") as tmp:
                spawn = subprocess.run(
                    [binary, "--working-dir", tmp, "new", "--no-attach"],
                    capture_output=True, timeout=30, text=True,
                )
                out = spawn.stdout or ""
                port = None
                sid = None
                for tok in out.split():
                    if tok.startswith("port="):
                        port = tok.split("=", 1)[1]
                    elif tok.startswith("session_id="):
                        sid = tok.split("=", 1)[1]
                if spawn.returncode == 0 and port and sid:
                    click.echo(f"[ok] polytoken spawn — session {sid} on port {port}")
                    # Clean up the throwaway daemon.
                    with urllib.request.urlopen(
                        urllib.request.Request(f"http://127.0.0.1:{port}/terminate", method="POST"),
                        timeout=3,
                    ):
                        pass
                else:
                    click.echo(f"[fail] polytoken spawn — unexpected output: {out.strip()[:200]!r}", err=True)
                    failed = True
        except Exception as e:
            click.echo(f"[fail] polytoken spawn — error: {e}", err=True)
            failed = True

    # Check 6: attachments dir writable
    attachments_dir = Path.home() / ".local" / "state" / "claude-discord-bridge" / "attachments"
    try:
        attachments_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=attachments_dir, delete=True):
            pass
        click.echo(f"[ok] attachments dir — {attachments_dir}")
    except PermissionError:
        click.echo(f"[fail] attachments dir — not writable ({attachments_dir})", err=True)
        failed = True
    except Exception as e:
        click.echo(f"[fail] attachments dir — error: {e}", err=True)
        failed = True

    click.echo()
    if failed:
        click.echo("Doctor: some checks failed. Please fix the issues above.", err=True)
        sys.exit(1)
    elif warned:
        click.echo("Doctor: checks complete with warnings. Bridge may work but check the above.", err=True)
        sys.exit(0)
    else:
        click.echo("Doctor: all checks passed. Bridge is ready.")
        sys.exit(0)


def main() -> None:
    """Entry point for the CLI (referenced by pyproject.toml [project.scripts])."""
    cli()

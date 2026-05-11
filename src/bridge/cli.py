"""CLI entrypoints for the bridge daemon."""

import asyncio
import json
import logging
import os
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import click

import bridge
from bridge.bot import Bot
from bridge.secrets import SECRETS_FILE, SecretsError, load_secrets, write_secrets, Secrets, secrets_file_perms
from bridge.server import serve as serve_server
from bridge.zellij import SESSION_NAME as ZELLIJ_SESSION_NAME

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

        # Wait up to _TOKEN_VALIDATION_TIMEOUT seconds for bot to become ready
        start_time = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - start_time < _TOKEN_VALIDATION_TIMEOUT:
            if bot.is_ready:
                # Post confirmation message to channel root (no thread)
                await bot.post("✅ cc-bridge init succeeded — you'll see future notifications here.")
                return True
            await asyncio.sleep(0.1)

        # Timeout reached
        return False
    finally:
        await bot.close()


@click.group()
def cli() -> None:
    """Claude Code <-> Discord bridge daemon."""
    pass


@cli.command()
def init() -> None:
    """Interactive bootstrap: collect bot token and channel ID, write secrets file.

    Prompts for:
    - DISCORD_BOT_TOKEN (hidden input)
    - DISCORD_CHANNEL_ID (validated as positive integer)

    Writes to ~/.config/cc-bridge/secrets.json (mode 0600).
    """
    click.echo("Welcome to the Claude Code <-> Discord bridge.")
    click.echo()
    click.echo(
        "This wizard will set up the bridge to post messages to Discord. "
        "You'll need:"
    )
    click.echo("  - A Discord bot token (from Discord Developer Portal)")
    click.echo("  - A Discord channel ID (from a text channel you own)")
    click.echo()

    # Resolve secrets path from env var for testability
    secrets_path = Path(os.environ.get("BRIDGE_SECRETS_PATH", str(SECRETS_FILE)))

    # Check if file already exists
    if secrets_path.exists():
        if not click.confirm(
            f"Secrets file already exists at {secrets_path}. Overwrite?",
            abort=True,
        ):
            return

    # Prompt for bot token (hidden)
    bot_token = click.prompt(
        "DISCORD_BOT_TOKEN", hide_input=True, confirmation_prompt=False
    )

    # Prompt for channel ID (with validation)
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

    # Print reminders before writing
    click.echo()
    click.echo("Important reminders:")
    click.echo(
        "1. You must enable Privileged Gateway Intent: Message Content for the bot at"
    )
    click.echo("   Discord Developer Portal > Applications > [your app] > Bot")
    click.echo()
    click.echo(
        "2. The bot must be a member of the guild containing channel ID {0}".format(
            channel_id
        )
    )
    click.echo("   and have these permissions:")
    click.echo("   - View Channel")
    click.echo("   - Send Messages")
    click.echo("   - Create Public Threads")
    click.echo()

    # Write secrets
    secrets = Secrets(bot_token=bot_token, channel_id=channel_id)
    write_secrets(secrets, path=secrets_path)

    # Verify mode
    perms = stat.S_IMODE(secrets_path.stat().st_mode)
    if perms != 0o600:
        # Delete the dangling permissive file before failing
        secrets_path.unlink()
        click.echo(
            f"Error: file mode is {oct(perms)}, expected 0o600. "
            f"Secrets file deleted. Please check your filesystem settings.",
            err=True
        )
        sys.exit(1)

    # Validate token and channel by connecting to Discord
    click.echo()
    click.echo("Validating token and channel...")
    try:
        success = asyncio.run(_validate_token_and_post_test(secrets))
        if not success:
            click.echo(
                "Error: could not connect — check token/intents/network",
                err=True
            )
            sys.exit(2)
    except Exception as e:
        click.echo(
            f"Error: could not connect — check token/intents/network ({e})",
            err=True
        )
        sys.exit(2)

    click.echo()
    click.echo(
        f"Wrote secrets to {secrets_path} (mode 0600). "
        "Start the daemon with:"
    )
    click.echo("  cc-bridge serve")
    click.echo()
    click.echo(
        "Or use the systemd unit at:"
    )
    click.echo("  packaging/cc-bridge.service")


@cli.command()
@click.option(
    "--host",
    default="127.0.0.1",
    help="Host to bind to (default: 127.0.0.1)",
)
@click.option(
    "--port",
    default=8787,
    type=int,
    help="Port to bind to (default: 8787)",
)
def serve(host: str, port: int) -> None:
    """Run the bridge daemon.

    Loads secrets from ~/.config/cc-bridge/secrets.json and starts
    the HTTP server + Discord bot.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Resolve secrets path from env var for testability
    secrets_path = Path(os.environ.get("BRIDGE_SECRETS_PATH", str(SECRETS_FILE)))

    try:
        secrets = load_secrets(path=secrets_path)
    except SecretsError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(2)

    asyncio.run(serve_server(secrets, host=host, port=port))


@cli.command()
def doctor() -> None:
    """Run diagnostic checks on the bridge setup.

    Checks:
    - Secrets file present and mode 0600
    - Bridge daemon health and bot connectivity
    - Settings.json hooks point to bridge scripts
    - Skill symlink setup

    Exit 0 if all checks pass (ok) or warn. Exit 1 if any check fails.
    """
    failed = False
    warned = False

    # Resolve secrets path from env var for testability
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
        health_url = f"{bridge_url}/v1/health"
        req = urllib.request.Request(health_url)
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

    # Check 4: Settings.json hooks
    settings_path = Path.home() / ".claude" / "settings.json"
    if settings_path.exists():
        try:
            settings_data = json.loads(settings_path.read_text())
            hooks = settings_data.get("hooks", {})

            # Compute expected hook paths at runtime
            expected_hooks_dir = Path(bridge.__file__).parent.parent.parent / "hooks"
            expected_stop_path = str(expected_hooks_dir / "notify-stop.py")
            expected_notif_path = str(expected_hooks_dir / "notify-notification.py")

            hooks_ok = True

            # Check Stop matcher
            stop_found = False
            for stop_matcher in hooks.get("Stop", []):
                for hook_spec in stop_matcher.get("hooks", []):
                    cmd = hook_spec.get("command", "")
                    if expected_stop_path in cmd:
                        stop_found = True
                        break

            if not stop_found:
                click.echo("[fail] Settings.json hooks — Stop matcher missing or incorrect", err=True)
                hooks_ok = False

            # Check Notification matcher
            notif_found = False
            for notif_matcher in hooks.get("Notification", []):
                for hook_spec in notif_matcher.get("hooks", []):
                    cmd = hook_spec.get("command", "")
                    if expected_notif_path in cmd:
                        notif_found = True
                        break

            if not notif_found:
                click.echo("[fail] Settings.json hooks — Notification matcher missing or incorrect", err=True)
                hooks_ok = False

            if hooks_ok:
                click.echo("[ok] Settings.json hooks — Stop and Notification matchers configured")
            else:
                failed = True
        except Exception as e:
            click.echo(f"[fail] Settings.json hooks — error reading {settings_path}: {e}", err=True)
            failed = True
    else:
        click.echo(f"[warn] Settings.json hooks — {settings_path} not found (skipping)", err=True)
        warned = True

    # Check 5: Skill symlink
    skill_path = Path.home() / ".claude" / "skills" / "ask-discord" / "SKILL.md"
    if skill_path.exists():
        # Check if it's a symlink to the repo or a copy with matching content
        repo_skill_path = Path(bridge.__file__).parent.parent.parent / "skills" / "SKILL.md"
        if skill_path.is_symlink():
            target = skill_path.resolve()
            if repo_skill_path.exists() and target == repo_skill_path.resolve():
                click.echo(f"[ok] Skill symlink — {skill_path} → {target}")
            else:
                click.echo(f"[warn] Skill symlink — {skill_path} symlink target mismatch", err=True)
                warned = True
        else:
            # Check if it's a copy with same content
            if repo_skill_path.exists() and skill_path.read_text() == repo_skill_path.read_text():
                click.echo(f"[ok] Skill symlink — {skill_path} (copy of {repo_skill_path})")
            else:
                click.echo(f"[warn] Skill symlink — {skill_path} exists but is not a symlink", err=True)
                warned = True
    else:
        click.echo(f"[warn] Skill symlink — {skill_path} not found", err=True)
        warned = True

    # Check 6: zellij installed
    try:
        result = subprocess.run(["zellij", "--version"], capture_output=True, timeout=5, text=True)
        if result.returncode == 0:
            version_line = result.stdout.strip().split("\n")[0] if result.stdout else "zellij"
            click.echo(f"[ok] zellij CLI — {version_line}")
        else:
            click.echo(f"[fail] zellij CLI — exited with status {result.returncode}", err=True)
            failed = True
    except FileNotFoundError:
        path_env = os.environ.get("PATH", "(not set)")
        click.echo(f"[fail] zellij CLI — not installed (PATH = {path_env})", err=True)
        failed = True
    except subprocess.TimeoutExpired:
        click.echo("[fail] zellij CLI — timeout checking version", err=True)
        failed = True
    except Exception as e:
        click.echo(f"[fail] zellij CLI — error: {e}", err=True)
        failed = True

    # Check 7: bridge zellij session exists
    try:
        result = subprocess.run(["zellij", "list-sessions"], capture_output=True, timeout=5, text=True)
        if ZELLIJ_SESSION_NAME in result.stdout:
            click.echo(f"[ok] zellij session `{ZELLIJ_SESSION_NAME}` — running")
        else:
            click.echo(
                f"[warn] zellij session `{ZELLIJ_SESSION_NAME}` — not running yet "
                "(will be created on first /start)",
                err=True,
            )
            warned = True
    except FileNotFoundError:
        click.echo(f"[warn] zellij session `{ZELLIJ_SESSION_NAME}` — cannot check (zellij not installed)", err=True)
        warned = True
    except subprocess.TimeoutExpired:
        click.echo(f"[warn] zellij session `{ZELLIJ_SESSION_NAME}` — timeout checking sessions", err=True)
        warned = True
    except Exception as e:
        click.echo(f"[warn] zellij session `{ZELLIJ_SESSION_NAME}` — error: {e}", err=True)
        warned = True

    # Check 8: task-settings dir writable
    task_settings_dir = Path.home() / ".local" / "state" / "cc-bridge" / "task-settings"
    try:
        task_settings_dir.mkdir(parents=True, exist_ok=True)
        # Try to write a temp file
        with tempfile.NamedTemporaryFile(dir=task_settings_dir, delete=True):
            pass
        click.echo(f"[ok] task-settings dir — {task_settings_dir}")
    except PermissionError:
        click.echo(f"[fail] task-settings dir — not writable ({task_settings_dir})", err=True)
        failed = True
    except Exception as e:
        click.echo(f"[fail] task-settings dir — error: {e}", err=True)
        failed = True

    # Check 9: hook scripts present and executable. Listed here:
    #   - notify-stop / notify-notification: registered in the user's global
    #     ~/.claude/settings.json (verified earlier in checks 5-6).
    #   - event / pretooluse-approve: injected per-task by `_write_task_settings`.
    hooks_dir = Path(bridge.__file__).parent.parent.parent / "hooks"
    hook_scripts = [
        "notify-stop.py",
        "notify-notification.py",
        "event.py",
        "pretooluse-approve.py",
    ]
    for script_name in hook_scripts:
        script_path = hooks_dir / script_name
        if script_path.exists() and os.access(script_path, os.X_OK):
            click.echo(f"[ok] hook script — {script_name}")
        else:
            if not script_path.exists():
                click.echo(f"[fail] hook script — {script_name} missing", err=True)
            else:
                click.echo(f"[fail] hook script — {script_name} not executable", err=True)
            failed = True

    # Check 10: claude on PATH
    try:
        result = subprocess.run(["which", "claude"], capture_output=True, timeout=2, text=True)
        if result.returncode == 0:
            claude_path = result.stdout.strip()
            click.echo(f"[ok] claude CLI — {claude_path}")
        else:
            click.echo("[warn] claude CLI — not on PATH (the daemon's spawned shells must have it)", err=True)
            warned = True
    except FileNotFoundError:
        click.echo("[warn] claude CLI — 'which' not found; cannot check PATH", err=True)
        warned = True
    except subprocess.TimeoutExpired:
        click.echo("[warn] claude CLI — timeout checking PATH", err=True)
        warned = True
    except Exception as e:
        click.echo(f"[warn] claude CLI — error: {e}", err=True)
        warned = True

    # Final summary
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

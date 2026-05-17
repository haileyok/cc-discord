# cc-bridge Multiplatform — Phase 7: CLI + Init + Doctor Updates

**Goal:** Update the CLI to support platform selection via `BRIDGE_PLATFORM` env var, add Mattermost-specific init wizard and doctor checks, and wire the serve command to instantiate the selected backend.

**Architecture:** The `serve` command reads `BRIDGE_PLATFORM` env var to choose which backend to instantiate. The `init` command gains a `--platform` flag to run platform-specific setup wizards. The `doctor` command adds Mattermost-specific health checks alongside existing Discord checks.

**Tech Stack:** Python 3.12, click (CLI framework)

**Scope:** 8 phases from original design (phase 7 of 8)

**Codebase verified:** 2026-05-10

---

## Acceptance Criteria Coverage

This phase implements and tests:

### cc-bridge-multiplatform.AC3: One-at-a-Time Runtime
- **cc-bridge-multiplatform.AC3.1 Success:** `BRIDGE_PLATFORM=discord` starts Discord backend only; Mattermost config is not required
- **cc-bridge-multiplatform.AC3.2 Success:** `BRIDGE_PLATFORM=mattermost` starts Mattermost backend only; Discord config is not required
- **cc-bridge-multiplatform.AC3.3 Success:** Missing or invalid `BRIDGE_PLATFORM` produces a clear error message

### cc-bridge-multiplatform.F3: Invalid Platform Error
- **cc-bridge-multiplatform.F3:** If `BRIDGE_PLATFORM` is set to an unknown value, daemon exits with a clear error (not a stack trace)

---

<!-- START_SUBCOMPONENT_A (tasks 1-3) -->
<!-- START_TASK_1 -->
### Task 1: Add BRIDGE_PLATFORM selection to serve command

**Verifies:** cc-bridge-multiplatform.AC3.1, cc-bridge-multiplatform.AC3.2, cc-bridge-multiplatform.AC3.3, cc-bridge-multiplatform.F3

**Files:**
- Modify: `src/bridge/cli.py` (serve command)
- Modify: `src/bridge/server.py` (platform-aware backend construction)

**Implementation:**

Update the `serve` command to read `BRIDGE_PLATFORM` and pass it through:

```python
@cli.command()
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=8787, type=int)
def serve(host: str, port: int) -> None:
    """Run the cc-bridge daemon."""
    platform = os.environ.get("BRIDGE_PLATFORM", "").lower()
    if platform not in ("discord", "mattermost"):
        if not platform:
            click.echo(
                "Error: BRIDGE_PLATFORM environment variable is required.\n"
                "Set BRIDGE_PLATFORM=discord or BRIDGE_PLATFORM=mattermost",
                err=True,
            )
        else:
            click.echo(
                f"Error: Unknown platform '{platform}'.\n"
                f"Valid values: discord, mattermost",
                err=True,
            )
        raise SystemExit(2)

    secrets = load_secrets()
    asyncio.run(serve_server(secrets, host=host, port=port, platform=platform))
```

Update `server.py`'s `serve()` function to accept `platform` parameter and construct the correct backend:

```python
async def serve(secrets: Secrets, *, host: str, port: int, platform: str) -> None:
    if platform == "discord":
        from bridge.backends.discord import DiscordBot
        bot = DiscordBot(secrets.bot_token, secrets.channel_id, ...)
    elif platform == "mattermost":
        from bridge.backends.mattermost import MattermostBot
        bot = MattermostBot(
            secrets.server_url,
            secrets.bot_token,
            secrets.channel_id,
            allowed_user_ids=secrets.allowed_user_ids,
            ...
        )
    # ... rest of setup using bot as ChatPlatform ...
```

**Testing:**

Tests must verify:
- cc-bridge-multiplatform.AC3.1: `BRIDGE_PLATFORM=discord` constructs DiscordBot
- cc-bridge-multiplatform.AC3.2: `BRIDGE_PLATFORM=mattermost` constructs MattermostBot
- cc-bridge-multiplatform.AC3.3: Missing BRIDGE_PLATFORM → clear error, exit code 2
- cc-bridge-multiplatform.F3: `BRIDGE_PLATFORM=slack` → clear error, exit code 2

Test file: `tests/test_cli.py` (extend existing)

**Verification:**

```bash
uv run pytest tests/test_cli.py -v
```

**Commit:** `feat: add BRIDGE_PLATFORM selection to serve command`

<!-- END_TASK_1 -->

<!-- START_TASK_2 -->
### Task 2: Add Mattermost init wizard

**Files:**
- Modify: `src/bridge/cli.py` (init command)
- Modify: `src/bridge/secrets.py` (Mattermost-specific secret fields)

**Implementation:**

Add `--platform` option to the `init` command:

```python
@cli.command()
@click.option(
    "--platform",
    type=click.Choice(["discord", "mattermost"]),
    required=True,
    help="Which chat platform to configure.",
)
def init(platform: str) -> None:
    """Interactive setup for cc-bridge."""
    if platform == "discord":
        _init_discord()
    elif platform == "mattermost":
        _init_mattermost()
```

The Discord init path stays identical to the current implementation (moved to `_init_discord()`).

Add `_init_mattermost()`:

```python
def _init_mattermost() -> None:
    """Mattermost-specific init wizard."""
    click.echo("=== cc-bridge Mattermost setup ===\n")

    server_url = click.prompt("Mattermost server URL (e.g., https://mm.example.com)")
    bot_token = click.prompt("Bot access token", hide_input=True)
    channel_id = click.prompt("Channel ID (26-char alphanumeric)")

    allowed_ids_raw = click.prompt(
        "Allowed user IDs (comma-separated, or 'all')",
        default="all",
    )
    allowed_user_ids = (
        None if allowed_ids_raw.strip().lower() == "all"
        else [uid.strip() for uid in allowed_ids_raw.split(",")]
    )

    secrets = {
        "platform": "mattermost",
        "server_url": server_url.rstrip("/"),
        "bot_token": bot_token,
        "channel_id": channel_id,
    }
    if allowed_user_ids:
        secrets["allowed_user_ids"] = allowed_user_ids

    # Validate connection
    click.echo("\nValidating connection...")
    try:
        import aiohttp
        async def _validate():
            async with aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {bot_token}"}
            ) as session:
                url = f"{server_url.rstrip('/')}/api/v4/users/me"
                async with session.get(url) as resp:
                    if resp.status == 401:
                        click.echo("Error: Invalid bot token (401 Unauthorized).", err=True)
                        raise SystemExit(2)
                    resp.raise_for_status()
                    me = await resp.json()
                    click.echo(f"  Authenticated as: {me.get('username', 'unknown')}")

                # Verify channel access
                chan_url = f"{server_url.rstrip('/')}/api/v4/channels/{channel_id}"
                async with session.get(chan_url) as resp:
                    if resp.status == 404:
                        click.echo(f"Error: Channel {channel_id} not found.", err=True)
                        raise SystemExit(2)
                    if resp.status == 403:
                        click.echo(f"Error: Bot lacks access to channel {channel_id}.", err=True)
                        raise SystemExit(2)
                    resp.raise_for_status()
                    chan = await resp.json()
                    click.echo(f"  Channel: {chan.get('display_name', channel_id)}")

        asyncio.run(_validate())
    except aiohttp.ClientConnectorError:
        click.echo(f"Error: Cannot connect to {server_url}.", err=True)
        raise SystemExit(2)
    except aiohttp.ClientSSLError:
        click.echo(f"Error: SSL certificate verification failed for {server_url}.", err=True)
        raise SystemExit(2)

    write_secrets(secrets)
    click.echo(f"\n✅ cc-bridge init succeeded — secrets written to {SECRETS_FILE}")
```

Update `src/bridge/secrets.py` to handle both Discord and Mattermost secret schemas:

```python
@dataclass(frozen=True)
class Secrets:
    platform: str  # "discord" or "mattermost"
    bot_token: str
    channel_id: str  # str for both (Discord converts internally)
    # Discord-specific
    # Mattermost-specific
    server_url: str | None = None
    allowed_user_ids: list[str] | None = None
```

**Testing:**

Tests must verify:
- Mattermost init prompts for correct fields
- Secrets file written with correct structure
- Discord init still works unchanged

Test file: `tests/test_cli.py` (extend)

**Verification:**

```bash
uv run pytest tests/test_cli.py -v
```

**Commit:** `feat: add Mattermost init wizard`

<!-- END_TASK_2 -->

<!-- START_TASK_3 -->
### Task 3: Add Mattermost doctor checks

**Files:**
- Modify: `src/bridge/cli.py` (doctor command)

**Implementation:**

The existing doctor command runs 10 Discord-specific checks. Add platform-aware checks:

**Shared checks** (both platforms):
1. Secrets file present
2. Secrets file mode 0600
3. Daemon health (GET /v1/health)
4. Settings.json hooks
5. zellij CLI present
6. zellij session alive
7. task-settings dir writable
8. Hook scripts present + executable
9. claude CLI on PATH

**Discord-specific checks:**
- Discord channel_id is valid integer

**Mattermost-specific checks:**
- Mattermost server reachable (GET /api/v4/system/ping)
- Bot token valid (GET /api/v4/users/me returns 200)
- Channel accessible (GET /api/v4/channels/{channel_id} returns 200)

The doctor command reads the platform from secrets.json or `BRIDGE_PLATFORM` env var to determine which checks to run.

**Testing:**

Tests must verify:
- Doctor runs platform-appropriate checks based on secrets content
- Mattermost checks report correct pass/fail status

Test file: `tests/test_cli.py` (extend)

**Verification:**

```bash
uv run pytest tests/test_cli.py -v
```

**Commit:** `feat: add Mattermost-specific doctor checks`

<!-- END_TASK_3 -->
<!-- END_SUBCOMPONENT_A -->

<!-- START_TASK_4 -->
### Task 4: Full regression verification

**Files:**
- None (verification only)

**Verification:**

```bash
uv run pytest -v
```

Expected: All tests pass.

<!-- END_TASK_4 -->

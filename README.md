# claude-slack-bridge

`claude-slack-bridge` is a localhost Python daemon that drives [Polytoken](https://github.com/) agent sessions from a private Slack workspace. It uses Slack Web API plus Socket Mode on one asyncio loop; each task owns one headless Polytoken daemon and its event stream.

> **Trust model:** this is a personal/operator-level bridge, not a multi-tenant sandbox. Anyone who can write in a routed Slack conversation can ask the trusted Polytoken daemon to use its full tool set, including `shell_exec`. Invite only people you trust with the host and the configured workspace channels.

## What it does

- `/agent` and shortcuts provide the Slack control surface for starting and managing tasks.
- Each task runs `polytoken new --no-attach` in a project directory. Prompts go to the daemon's loopback `POST /prompt`; assistant/tool activity comes from its resumable `GET /events` SSE stream.
- Replies in a task's root message thread become prompts. Structured questions are rendered to Slack; the next plain-text reply answers the pending question.
- Text, thinking, tool summaries, subagent activity, todo state, reactions, and attachments are translated to Slack messages/blocks/files.
- Incoming private files are downloaded with the bot token, bounded, stored under the bridge state directory, and passed as `@<absolute-path>` references. Agent files use `[[attach: /absolute/path]]` markers.
- Personal tasks are owner-only. Collaborative tasks invite explicit Slack participants and enforce a bounded app-to-app exchange budget (default **20** messages; set `BRIDGE_APP_EXCHANGE_BUDGET` to change it). When the cap is reached the task pauses and alerts its owner.

## Requirements

- Python 3.12 managed by [uv](https://docs.astral.sh/uv/) (`.python-version`).
- A Slack app installed in the target workspace with Socket Mode enabled. Use [`slack-app-manifest.yaml`](slack-app-manifest.yaml) as the starting manifest.
- A private Slack home channel containing the bot. The bot needs the manifest's bot scopes; no user token is needed or accepted.
- The `polytoken` executable on `PATH` (the service unit sets a conservative explicit PATH; add your install location if needed).

## Slack app setup

1. Create a Slack app from `slack-app-manifest.yaml` (or add the equivalent settings in the Slack app dashboard).
2. Enable **Socket Mode** and create an app-level token with the `connections:write` scope. Keep the resulting `xapp-...` token private.
3. Install/reinstall the app in the workspace. Copy the bot token (`xoxb-...`) and app token (`xapp-...`). Do not create or use a user token for this bridge.
4. Enable the manifest's `/agent`, global shortcut, interactivity, `message.groups`, `app_mention`, and reaction subscriptions.
5. Invite the bot to a private home channel. Record the workspace team ID, home channel ID, and the trusted owner's user ID.

The manifest intentionally enables private-channel capabilities (`groups:read`, `groups:write`, `groups:history`) and channel create/invite/archive support through the relevant channel-management scopes. Review scopes against your workspace policy before installing.

## Install and configure

```bash
git clone <repository-url> claude-slack-bridge
cd claude-slack-bridge
uv sync
uv run claude-slack-bridge init
```

`init` explains the manifest/install requirements, prompts for exactly these fields, writes them to `~/.config/claude-slack-bridge/secrets.json`, and validates by starting the real Slack `Bot`: `auth.test`, configured team, owner lookup, private home-channel membership, and Socket Mode. It posts a confirmation message only after startup succeeds.

| JSON key | Value |
|---|---|
| `SLACK_BOT_TOKEN` | `xoxb-...` bot token |
| `SLACK_APP_TOKEN` | `xapp-...` app-level Socket Mode token |
| `SLACK_TEAM_ID` | Workspace ID, usually `T...` |
| `SLACK_HOME_CHANNEL_ID` | Private home channel ID, usually `C...` or `G...` |
| `SLACK_OWNER_USER_ID` | Trusted operator's user ID, usually `U...` |

The secrets directory is mode `0700`; the JSON file is mode `0600`. `BRIDGE_SECRETS_PATH` may point to an intentionally chosen alternate file for tests/deployment. There is no automatic Discord-config fallback or migration.

## Run and deploy

Foreground (recommended while first configuring):

```bash
scripts/run-foreground.sh
# or
uv run claude-slack-bridge serve
```

User systemd (after `uv tool install .`, so the executable exists at `~/.local/bin`):

```bash
bash scripts/install-systemd-user.sh
systemctl --user daemon-reload
systemctl --user enable --now claude-slack-bridge
```

If `systemctl --user` reports `Operation not permitted`, enable user lingering (`loginctl enable-linger "$USER"` where permitted) or run the foreground script under tmux/nohup. The unit uses `claude-slack-bridge.service`; it does not replace an old Discord unit automatically.

Check setup and live connectivity:

```bash
uv run claude-slack-bridge doctor
```

`doctor` checks secrets and directory modes, Slack identity/team/private-channel membership and observable Socket Mode state, local bridge health, Polytoken version plus a throwaway headless spawn/termination smoke, private state storage, and warns if a legacy Discord unit/process is still visible. Live checks require network access and a running daemon; warnings distinguish non-observable runtime state from hard failures.

## Commands and routing

The Slack command surface is implemented by the current adapter. `/agent` is the primary entry point; task operations map to the existing registry lifecycle (start/spawn, list, stop, kill, skill, effort, model, facet, rename, stats, tasks, pin/unpin where supported). Use a root message thread for normal prompts and answers.

Slack limitations are intentional:

- Slack has message threads, not Discord-style named thread objects. A task's root message timestamp is its durable conversation key; rename changes the root message text rather than creating a separate thread name.
- A Slack message cannot be archived. Closing a collaborative task may archive its disposable private channel; the configured home channel is protected from accidental archive.
- Slack blocks and messages have provider limits, so long output is chunked and rich rendering may degrade to fallback text.
- Slack file uploads/downloads are bounded and limited to the adapter's supported file APIs. An attachment that cannot be authenticated or exceeds the configured cap is skipped/reported rather than handed to the daemon.
- Questions consume the next plain-text reply in that task thread. A reply with files/voice is treated as a new prompt and the question remains pending.
- Permission interrogatives are suppressed because Polytoken runs in bypass mode. Only user-facing clarification, confirmation, and choice questions are surfaced.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `BRIDGE_SECRETS_PATH` | `~/.config/claude-slack-bridge/secrets.json` | Explicit Slack JSON path; no legacy fallback |
| `BRIDGE_STATE_DIR` | `~/.local/state/claude-slack-bridge` | Private state/attachments root |
| `BRIDGE_URL` | `http://127.0.0.1:8787` | Local health URL used by `doctor` |
| `POLYTOKEN_BIN` | `polytoken` | Polytoken executable for smoke checks |
| `BRIDGE_PROJECT_ROOTS` | unset | Colon-separated parent paths for project selection |
| `BRIDGE_ATTACHMENT_TTL_SECS` | `604800` | Attachment cleanup TTL (7 days) |
| `BRIDGE_MAX_ATTACHMENT_BYTES` | `10485760` | Incoming attachment byte cap |
| `BRIDGE_APP_EXCHANGE_BUDGET` | `20` | Collaborative app-to-app loop cap |
| `WISPR_FLOW_API_TOKEN` | unset | Optional voice transcription backend |
| `BRIDGE_WHISPER_BIN` / `BRIDGE_WHISPER_MODEL` | `whisper` / `base` | Local voice transcription fallback |

## Security and threat model

The bridge trusts the configured Slack workspace/channel and the local host. Slack participants can control prompts, and the Polytoken daemon has bypass permissions and host tools. Do not place secrets, production credentials, or untrusted users in a routed channel; use a separate private workspace/channel when possible. The loopback daemon is unauthenticated by design, so keep the bridge host trusted and do not bind the health server publicly. Bot-token downloads are restricted to HTTPS Slack URLs and bounded sizes; file paths emitted by the agent must be absolute and existing before upload. Secrets are never logged and are protected with 0600/0700 modes.

This is not a security boundary against a malicious workspace member, compromised host account, malicious project checkout, or prompt injection in files. The bridge's `@` attachment references are not an extra sandbox: the agent already has shell access. Slack app scopes should be reviewed and reduced if a deployment does not need collaborative channel creation/invites.

## Architecture

| Component | Role |
|---|---|
| `src/bridge/bot.py` | Slack Web API and Socket Mode adapter; startup identity/channel checks, bounded retries, messages/blocks/files/reactions |
| `src/bridge/cli.py` | `init`, `serve`, and `doctor` operational commands |
| `src/bridge/secrets.py` | Slack-only secure JSON loader/writer |
| `src/bridge/server.py` | aiohttp health endpoint and message dispatcher |
| `src/bridge/tasks.py` | Task lifecycle, owner/participant routing, app-loop cap, attachment handling |
| `src/bridge/events.py` | Pure Polytoken event-to-render action translator and gap reconciliation |
| `src/bridge/polytoken_client.py` | Async per-daemon HTTP/SSE client |
| `src/bridge/daemon_supervisor.py` | Spawn/list/terminate Polytoken daemons |
| `src/bridge/state.py` | SQLite state and normalized Slack conversation records |

Each daemon survives a bridge restart when discoverable through `polytoken sessions`; the bridge reattaches live sessions and marks missing sessions crashed. SSE reconnects resume after the last sequence. A live in-stream gap is made visible and reconciles cheap state; full history replay is not guaranteed.

## Development and collaboration

```bash
uv run pytest -q
uv run pytest -q tests/test_cli.py tests/test_secrets.py
```

Use focused tests while changing an adapter. Keep provider-neutral task/domain/state contracts stable, inspect `src/bridge/bot.py` before integrating Slack changes, and do not silently restore Discord paths or secrets. Changes to Slack manifests/scopes require a corresponding README/CLAUDE update and a security review. The repository uses a shared worktree during the provider migration: preserve unrelated concurrent adapter changes and avoid drive-by edits to files owned by another phase.

## Migration from claude-discord-bridge

This is a cutover, not an in-place compatibility mode. Stop the old Discord process/unit, install the Slack app, run `claude-slack-bridge init`, and start the renamed unit. The bridge writes new configuration at `~/.config/claude-slack-bridge/secrets.json` and new state/attachments at `~/.local/state/claude-slack-bridge`; it never reads old Discord config. Existing Discord tasks/channels are not migrated because Slack conversation keys and provider semantics differ. Treat old SQLite data as disposable bookkeeping and retain it only for manual reference/backup. Run `doctor`, verify the confirmation post in the private home channel, then start new tasks.

## License

TBD.

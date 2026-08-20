# claude-slack-bridge contributor guide

This repository is the Slack adapter for a localhost bridge between Polytoken daemon sessions and a trusted private Slack workspace. Read this document before changing behavior. It records operational invariants and migration hazards that are easy to miss from individual modules.

Freshness: 2026-06-15

## Scope and local tooling

- Python is pinned to 3.12 via `.python-version`; use `uv run pytest`, not the system Python.
- The bridge runs as one Python process. `aiohttp`, Slack Web API calls, Socket Mode, task rendering, and lifecycle coordination share one asyncio event loop.
- The Polytoken executable must be on `PATH`. The systemd unit sets an explicit conservative PATH; shell PATH and systemd PATH are not assumed identical.
- The checked-in app manifest is `slack-app-manifest.yaml`. Operational names are `claude-slack-bridge`, `~/.config/claude-slack-bridge`, and `~/.local/state/claude-slack-bridge`.
- This is a shared migration worktree. Preserve unrelated concurrent changes. Inspect `src/bridge/bot.py` before integrating adapter-facing code; do not revert or duplicate its Slack API.

## Architecture

```
Slack Web API + Socket Mode
            │
            ▼
    bot.py / server.py ──> TaskRegistry (tasks.py)
                              │
                 ┌────────────┴─────────────┐
       inbound Slack thread message       outbound actions
                              │              ▲
                         /prompt       events.py translator
                              │              │
                     Polytoken daemon ← polytoken_client.py
                     one process/task       /events SSE
                              ▲
                     daemon_supervisor.py
```

`Bot.start()` validates `auth.test`, configured team, owner lookup, and the configured public or private home channel's identity/member flags. If an app token is configured it connects Socket Mode and dispatches `message`, `app_mention`, and reaction events. The `Bot` surface intentionally has provider-neutral method names consumed by `TaskRegistry`; Slack-specific behavior belongs in `bot.py`, not in task logic.

A task is keyed by `(team_id, channel_id, root_ts)` and owned by one Slack user. Personal tasks accept the owner; collaborative tasks maintain explicit participants and can create a private channel. A task runs one Polytoken daemon rooted at its project directory. Runtime ports are stored/discovered for reattachment but are not stable identities.

## Security / threat model (non-negotiable)

Authorization is based on stable actor IDs, not channel visibility. Personal tasks route only the configured owner; collaborative tasks route the owner plus explicit participants, and privileged controls remain owner-only. A public home channel may therefore have read-only observers, but it exposes all task output and attachments to them. Any actor authorized to post a routed message can cause the bypass-mode Polytoken agent to run arbitrary host commands, read/write project files, use network tools, and resolve `@<path>` references. Prompt text is proxied verbatim. Never authorize untrusted participants; never put secrets or sensitive production work in a publicly visible task.

The loopback Polytoken HTTP daemon is unauthenticated by design. Keep the health server on `127.0.0.1`; do not expose it to a network. A compromised local account, project checkout, Slack workspace member, or prompt-injected file is outside the bridge's defense boundary. Agent attachment markers are not a sandbox expansion: shell access already exists.

Secrets are Slack-only JSON fields:

- `SLACK_BOT_TOKEN` (`xoxb-`)
- `SLACK_APP_TOKEN` (`xapp-`)
- `SLACK_TEAM_ID`
- `SLACK_HOME_CHANNEL_ID`
- `SLACK_OWNER_USER_ID`

`src/bridge/secrets.py` never reads Discord keys or probes a Discord path. Writes use 0600 for the file and 0700 for its directory. Do not log token values, include them in errors, or add user-token impersonation. Keep Slack file downloads authenticated, HTTPS-only, and bounded; keep uploads bounded and limited to the adapter's supported APIs.

Manifest scopes are intentionally bot scopes for public/private channel history, files, reactions, users, and channel management. There must be no `oauth_config.scopes.user` block. If a new feature needs a scope, update the manifest, README, and threat-model review together.

## Event and routing invariants

- `subagent_handle` is the only subagent routing key. Set means the activity belongs to a live subagent block; unset means main-session aggregation. Do not infer subagents from sidecar files or text.
- `Translator` is pure and stateful only for sequence/content-block pairing. It emits typed actions; `TaskRegistry` renders them through `Bot`.
- SSE `Last-Event-ID` resume repairs dropped connections. A sequence gap on a live connection emits a visible reconcile notice and re-reads cheap `/state` data; it does not silently discard output. Full `/history` replay remains a future improvement.
- A vanished daemon is checked against `polytoken sessions`; confirmed absence marks the task crashed instead of retrying forever. Inconclusive session listing remains retryable.
- Permission interrogatives are suppressed because Polytoken runs in bypass mode. Choice/clarification/confirmation questions are posted to the task thread. The next plain-text reply is consumed as the answer; files/voice are treated as a normal prompt and the question remains pending.
- Incoming Slack files are downloaded under `~/.local/state/claude-slack-bridge/attachments/<task_id>/`, size bounded, and appended as `@<absolute-path>` tokens. Agent output `[[attach: /absolute/path]]` is stripped from text and uploaded only when the path is absolute and exists. Terminal paths clean task files; a periodic sweep removes files older than `BRIDGE_ATTACHMENT_TTL_SECS` (default seven days).
- Slack has no named thread object or archive operation. Root message timestamps are task conversation keys. Rename edits root text. Closing a disposable collaborative private channel may archive that channel; the configured home channel is protected.
- Slack provider limits require chunking and fallback text for blocks. Do not assume Discord's numeric IDs, embeds, reaction payloads, or 2,000-character rules in new Slack code.

## App-to-app loop cap

Collaborative task routing permits explicitly authorized app actors but bounds app-to-app exchanges using `BRIDGE_APP_EXCHANGE_BUDGET` (default 20). The counter is per task. Once the count exceeds the budget, the task is paused, persisted, and the owner is alerted once. Keep this cap when adding bot/app routing; never create an unbounded Slack/agent echo loop.

## Operational commands and checks

`claude-slack-bridge init` explains and assumes `slack-app-manifest.yaml`, prompts for the five Slack fields, writes secure config, then starts the real `Bot` and posts a confirmation. It does not silently read Discord config.

`claude-slack-bridge serve` loads only `BRIDGE_SECRETS_PATH` or `~/.config/claude-slack-bridge/secrets.json`, starts the health endpoint and Slack adapter, reconciles persisted tasks, then consumes daemon events.

`claude-slack-bridge doctor` checks:

1. Secrets file exists and is 0600; parent config directory is 0700.
2. All Slack fields and token prefixes validate.
3. Real Slack startup validates bot identity, workspace/team, owner, public/private home-channel membership, and observable Socket Mode state.
4. Local `/v1/health` is reachable and reports connected state.
5. `polytoken --version` and a throwaway `polytoken new --no-attach` smoke succeed; the smoke daemon is terminated.
6. `~/.local/state/claude-slack-bridge` (or `BRIDGE_STATE_DIR`) is private and writable.
7. A running `claude-discord-bridge` service/process is warned about, never silently stopped or deleted.

Network checks are live checks and can fail because Slack, Socket Mode, or the daemon is unavailable. Doctor distinguishes hard failures from warnings; a warning is not proof that production operation is healthy.

## Deployment

- `scripts/run-foreground.sh` runs `uv run claude-slack-bridge serve`; tmux/nohup is the verified fallback when user systemd is unavailable.
- `scripts/install-systemd-user.sh` installs `packaging/claude-slack-bridge.service`. The service assumes `uv tool install .` and invokes `%h/.local/bin/claude-slack-bridge`.
- Do not mix `uv run` and `uv tool install` assumptions. If systemd cannot start, inspect `systemctl --user status claude-slack-bridge` and the explicit PATH before changing code.
- The service rename is deliberately not an automatic migration. Doctor warns about old service/process names so an operator can stop them safely.

## State and migration

The provider migration uses new Slack conversation keys and new paths. Existing Discord tasks/channels are not resumable as Slack tasks. Stop the old bridge, retain old files only as an intentional backup, run the Slack app install and `init`, then start the new bridge and new tasks. There is no Discord secret fallback, path probing, or automatic SQLite row conversion. The state DB is bookkeeping and can be recreated after migration; do not claim old Discord threads were migrated.

The current shared worktree may contain transitional Discord wording in unowned provider-neutral modules while the parent agent completes the adapter cutover. Do not “fix” those files opportunistically: report exact dependencies instead. In particular, if `server.py` still constructs the old `Bot` signature or `state.py` still points at the old state path, coordinate the required parent-owned edits rather than changing them here.

## Collaboration rules

- Keep edits confined to the requested ownership set unless the parent explicitly expands it.
- Before touching `bot.py`, inspect existing Slack constructor, `start()` validation, retry policy, Socket Mode, file bounds, and normalized event types.
- Prefer pure translators and injectable fakes. Keep network calls async; never use blocking requests, sleeps, DB calls, or subprocess loops inside event handlers.
- Add focused tests for config paths, token/key rejection, permission bits, startup validation, and doctor failure/warning classification.
- Run `uv run pytest -q tests/test_cli.py tests/test_secrets.py` before broader tests. Scan owned files for old `claude-discord-bridge`, `DISCORD_*`, and Discord setup instructions before handing back.

## Owned operational files

- `src/bridge/secrets.py`: Slack credentials and secure paths.
- `src/bridge/cli.py`: init/serve/doctor.
- `tests/test_cli.py`, `tests/test_secrets.py`: focused operational tests.
- `README.md`, `CLAUDE.md`: user/contributor documentation and threat model.
- `packaging/claude-slack-bridge.service`, `scripts/*`: deployment names and startup scripts.
- `slack-app-manifest.yaml`: Socket Mode app declaration.

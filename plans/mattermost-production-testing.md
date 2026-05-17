# Mattermost Production Integration Testing

Runbook for testing cc-bridge against the live Mattermost instance on qcluster-1.

## Prerequisites

- SSH access to `qcluster-1`
- cc-bridge container running (`docker ps --filter name=cc-bridge`)
- Mattermost container running (`docker ps --filter name=mattermost`)

## Reference: Key IDs and paths

| Thing | Value |
|---|---|
| Bot user ID | `z3duh7764trajddiy59jjh4f6r` (username: `claude`) |
| qdot user ID | `t3t7mwbdu3d19ko9h6939twjka` |
| `#cc-test` channel ID | `egsyn9mn7bgg9k1dsfhqewyoka` |
| Bot token | In container at `/home/claude/.config/cc-bridge/secrets.json` |
| Mattermost internal URL | `http://mattermost:8065` (docker network, use from inside cc-bridge container) |
| Bridge health endpoint | `http://127.0.0.1:8787/v1/health` (from inside container) or `http://qcluster-1:8787/v1/health` (from LAN) |
| Hotpatch dir | `/opt/cc-bridge/patches/` on qcluster-1 |
| Compose file | `/opt/stacks/cc-bridge/compose.yml` |

## Step 0: Get a user access token

The bot ignores its own messages and filters by `allowed_user_ids`, so you must post as the qdot user. Personal access tokens are disabled by default.

```bash
# Enable PATs (idempotent)
ssh qcluster-1 "docker exec mattermost mmctl config set ServiceSettings.EnableUserAccessTokens true"

# Create a token
ssh qcluster-1 "docker exec mattermost mmctl token generate t3t7mwbdu3d19ko9h6939twjka 'cc-bridge-test' --format json"
# Save the "token" field from the output — you'll need it for all API calls below.
# Store it in a variable:
# QDOT_TOKEN=<token from output>
```

**After testing, revoke the token:**
```bash
ssh qcluster-1 "docker exec mattermost mmctl token revoke <token_id>"
```

## Step 1: Health check

```bash
ssh qcluster-1 "docker exec cc-bridge curl -s http://127.0.0.1:8787/v1/health"
# Expect: {"bot_connected": true, "channel_id": "egsyn9mn7bgg9k1dsfhqewyoka", ...}
```

If not healthy, check logs:
```bash
ssh qcluster-1 "docker logs cc-bridge --tail 30"
```

## Step 2: Test text commands

All API calls go through the cc-bridge container to reach `http://mattermost:8065`.

### !list
```bash
ssh qcluster-1 "docker exec cc-bridge curl -s -X POST 'http://mattermost:8065/api/v4/posts' \
  -H 'Authorization: Bearer $QDOT_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{\"channel_id\": \"egsyn9mn7bgg9k1dsfhqewyoka\", \"message\": \"!list\"}'"
```

### !start (the big one)
```bash
ssh qcluster-1 "docker exec cc-bridge curl -s -X POST 'http://mattermost:8065/api/v4/posts' \
  -H 'Authorization: Bearer $QDOT_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{\"channel_id\": \"egsyn9mn7bgg9k1dsfhqewyoka\", \"message\": \"!start /workspace say hello and stop\"}'"
```

### !stop / !kill (cleanup)
```bash
ssh qcluster-1 "docker exec cc-bridge curl -s -X POST 'http://mattermost:8065/api/v4/posts' \
  -H 'Authorization: Bearer $QDOT_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{\"channel_id\": \"egsyn9mn7bgg9k1dsfhqewyoka\", \"message\": \"!kill <task-id>\"}'"
```

## Step 3: Check results

### Read recent posts
```bash
ssh qcluster-1 "docker exec cc-bridge curl -s \
  'http://mattermost:8065/api/v4/channels/egsyn9mn7bgg9k1dsfhqewyoka/posts?per_page=10' \
  -H 'Authorization: Bearer <BOT_TOKEN>' \
  | python3 -c '
import sys, json
data = json.load(sys.stdin)
for pid in data[\"order\"]:
    p = data[\"posts\"][pid]
    user = p[\"user_id\"][:8]
    msg = p[\"message\"][:200]
    print(f\"{user}  {msg}\")
'"
```

The bot token is in the secrets file:
```bash
ssh qcluster-1 "docker exec cc-bridge cat /home/claude/.config/cc-bridge/secrets.json | python3 -c 'import sys,json; print(json.load(sys.stdin)[\"bot_token\"])'"
```

### Check bridge logs for hook events
```bash
ssh qcluster-1 "docker logs cc-bridge --tail 30"
```

For a successful `!start` with prompt, expect this sequence in the logs:
1. `hook event: SessionStart` — claude session bound
2. `hook event: UserPromptSubmit` — prompt received
3. `hook event: PostToolUse` (zero or more) — tool calls
4. `hook event: Stop` — claude finished
5. `hook event: SessionEnd` — session closed

If you see `task ... did not bind within 10s`, the prompt isn't reaching claude.

### Check zellij state
```bash
# List tabs
ssh qcluster-1 "docker exec cc-bridge zellij -s cc-bridge-worker action query-tab-names"

# List panes with state (check for exited, floating plugins, etc.)
ssh qcluster-1 'docker exec cc-bridge zellij -s cc-bridge-worker action list-panes --json | python3 -m json.tool'

# Check running processes
ssh qcluster-1 'docker exec cc-bridge sh -c "for f in /proc/*/cmdline; do pid=\$(basename \$(dirname \$f)); cmd=\$(cat \$f 2>/dev/null | tr \"\\0\" \" \"); [ -n \"\$cmd\" ] && echo \"PID \$pid: \$cmd\"; done 2>/dev/null | grep -v \"for f in\|sh -c\|cat \|basename\""'
```

## Step 4: Hotpatching

To test code changes without rebuilding the Docker image:

```bash
# Upload patched file
scp src/bridge/whatever.py qcluster-1:/opt/cc-bridge/patches/whatever.py

# Add bind mount to compose.yml if not already there
ssh qcluster-1 "grep 'whatever.py' /opt/stacks/cc-bridge/compose.yml"
# If missing, add a line like:
#   - /opt/cc-bridge/patches/whatever.py:/app/src/bridge/whatever.py:ro

# For mattermost backend files, the path is nested:
#   - /opt/cc-bridge/patches/mattermost/bot.py:/app/src/bridge/backends/mattermost/bot.py:ro

# Restart (restart preserves the container, down+up recreates it)
ssh qcluster-1 "sudo docker compose -f /opt/stacks/cc-bridge/compose.yml restart cc-bridge"
# OR for compose.yml changes:
ssh qcluster-1 "sudo docker compose -f /opt/stacks/cc-bridge/compose.yml down && sudo docker compose -f /opt/stacks/cc-bridge/compose.yml up -d"

# Verify patch loaded
ssh qcluster-1 "docker exec cc-bridge grep 'YourNewCode' /app/src/bridge/whatever.py"
```

### Current hotpatches (as of 2026-05-11)

Remove these once the CI image at `forgejo.buttplug.haus/qdot/cc-bridge:latest` includes commit `4090c62`:

- `mattermost/ws.py` — broad exception catch in WS loop
- `mattermost/bot.py` — MattermostMessageAdapter, channel_id property
- `server.py` — adapter wrapping in dispatch
- `command_handlers.py` — prompt passthrough to spawn_task
- `zellij.py` — floating plugin close
- `tasks.py` — -p flag in KDL layout, typing guard

## Known issues and gotchas

### zellij write-chars requires an attached client
`action write-chars` and `action write` silently succeed but don't deliver keystrokes when no terminal client is attached to the zellij session. The recommended deployment is bare-metal with a persistent zellij client attached via tmux: `tmux new -s zellij-client -d 'zellij attach cc-bridge-worker'`. Claude is spawned interactively; the initial prompt is delivered via `write_initial_prompt` after SessionStart, and follow-up messages use `write_to_pane`. Both require the attached client.

### dump-screen returns empty without an attached client
Same root cause as above. Don't rely on `dump-screen` for diagnostics in the Docker deployment — the pane content is there but not captured. Check `/proc/*/cmdline` for process state instead.

### "About Zellij" floating plugin pane
Zellij 0.44 launches this on session creation. `setup_wizard false` and `show_release_notes false` don't suppress it. The bridge closes it via `_close_floating_plugins` after a 1.5s delay. If you see it in `list-panes --json`, the delay might need increasing or the session was created before the fix was deployed.

### Typing indicator is a no-op on Mattermost
`fetch_messageable` returns a string (thread_id), not a Discord channel. The `_run_typing` guard skips the indicator silently. Mattermost doesn't have a native typing indicator API for bot users anyway.

### Container permissions
The `.env` file at `/opt/stacks/cc-bridge/.env` is owned by root (deployed via ansible with sops). `docker compose` without `sudo` fails with "permission denied" on `.env`. Always use `sudo docker compose`.

# cc-bridge

Localhost HTTP bridge between Claude Code sessions and Discord or Mattermost. Long turns ping your phone, permission prompts surface in a thread, Claude can ask questions when it's blocked, and you can drive whole Claude sessions from chat slash commands or text commands without ever attaching to the terminal.

## What it does

Runs as a small Python daemon (`aiohttp` + `discord.py`) on `127.0.0.1:8787`. Two modes, share one daemon:

**Notification mode** (you run Claude in your terminal, the bridge listens):
- **Stop hook** — Pings Discord when a Claude turn took >10 minutes. Result lands in a per-session thread.
- **Notification hook** — Permission prompts and idle states surface as `⏸ awaiting input` in the same thread.
- **`/ask-discord` skill** — Claude calls this when blocked; the question lands in the thread, the daemon waits up to 15 min for your reply, and Claude continues.

**Discord-driven mode** (`/start` from Discord spawns Claude in a zellij tab):
- Slash commands manage the lifecycle: `/start`, `/list`, `/stop`, `/kill`, `/restart`, `/skill`, `/rename`, `/stats`, `/tasks`.
- The bridge mirrors assistant text, tool use (with fenced diffs for Edit/Write), subagent activity (live-updated embed per agent), and the session's task list back to its thread.
- Discord replies in the thread relay into the pane; attachments are saved and their paths get inlined into the prompt so Claude reads them with the `Read` tool. Voice memos are auto-transcribed (Wispr Flow API or local `whisper`).
- `AskUserQuestion` and `ExitPlanMode` round-trip through Discord reactions / text replies — no need to attach to the pane to answer.
- The agent can attach files back by emitting `[[attach: /absolute/path]]` markers in its replies.

A separate webhook URL at `~/.claude/discord-notify-webhook` is used as a fallback when the daemon isn't running, so you don't lose pings if you forgot to start it.

## Prereqs

- Python 3.12 managed by [uv](https://github.com/astral-sh/uv) (the repo pins it via `.python-version`).
- **Discord** (optional): A Discord application with a bot, message-content intent enabled, invited to a guild you control, with permission to view + send messages + create public threads in one channel.
- **Mattermost** (optional): A Mattermost server with a bot account, authorized to post to at least one channel, and an API token.
- [Claude Code](https://docs.claude.com/claude-code) installed.

## Setup

### 1. Discord bot

1. https://discord.com/developers/applications → **New Application** → **Bot** tab → **Reset Token**, copy it.
2. **Privileged Gateway Intents** → enable **Message Content Intent**. Save.
3. **OAuth2 → URL Generator** → scopes: `bot` → bot permissions: `View Channels`, `Send Messages`, `Create Public Threads`, `Send Messages in Threads`, `Read Message History` → open the generated URL → invite the bot to your server.
4. In the Discord client: User Settings → Advanced → enable **Developer Mode** → right-click the target channel → **Copy Channel ID**.

### 2. Bridge daemon setup

```bash
git clone https://github.com/haileyok/cc-discord.git
cd cc-discord
uv sync
uv run cc-bridge init
```

#### Discord setup

`init` prompts for the bot token and channel ID, writes `~/.config/cc-bridge/secrets.json` at mode `0600`, validates the token by connecting to Discord (15s timeout), and posts a confirmation message to your channel. If the token's wrong it exits 2 and leaves the secrets file so you can fix and retry.

Set `BRIDGE_PLATFORM=discord` before starting the daemon (default).

#### Mattermost setup

`init` also supports Mattermost. When prompted, choose Mattermost and provide:
- **Server URL** — e.g., `https://mattermost.example.com`
- **Bot token** — Personal Access Token (PAT) with `post:channels` permission
- **Channel ID** — Channel where the bridge will post messages (you can find this in Mattermost's channel detail view)

The bridge will validate the token and channel by posting a test message. Secrets are stored at `~/.config/cc-bridge/secrets.json` (mode `0600`).

Set `BRIDGE_PLATFORM=mattermost` before starting the daemon.

### 3. Wire Claude Code hooks

The Stop and Notification hooks are referenced by absolute path from `~/.claude/settings.json`. Open it and add (or merge with existing `hooks`):

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "python3 /home/<you>/cc-discord/hooks/notify-stop.py", "async": true }
        ]
      }
    ],
    "Notification": [
      {
        "hooks": [
          { "type": "command", "command": "python3 /home/<you>/cc-discord/hooks/notify-notification.py", "async": true }
        ]
      }
    ]
  }
}
```

Replace `/home/<you>/cc-discord` with the actual repo path. Validate with `python3 -m json.tool ~/.claude/settings.json > /dev/null`.

If you already use `~/.claude/hooks/notify-long-task.sh` (or any other Stop hook), keep it on disk as rollback insurance — both hooks can coexist; this one just supersedes it.

These two hooks cover **notification mode**. **Discord-driven mode** (sessions spawned via `/start`) gets a different set of hooks injected via `claude --settings <task-scoped-path>` automatically — you don't add them to your user `settings.json`. The task-scoped settings file is generated per `/start` invocation and cleaned up when the task ends. See the `Discord-driven sessions` section below.

### 4. Install the `/ask-discord` skill

Claude Code discovers skills under `~/.claude/skills/<name>/SKILL.md`. The bridge ships the source-of-truth markdown in the repo; symlink it into place:

```bash
mkdir -p ~/.claude/skills/ask-discord
ln -sfn "$(pwd)/skills/SKILL.md" ~/.claude/skills/ask-discord/SKILL.md
```

After symlinking, run `/reload-plugins` in a Claude Code session — `/ask-discord` will appear in the slash-command picker.

### 5. Webhook fallback (optional)

Create a Discord channel webhook (channel settings → Integrations → Webhooks → New Webhook → copy URL), then write the URL to `~/.claude/discord-notify-webhook`:

```bash
echo 'https://discord.com/api/webhooks/...' > ~/.claude/discord-notify-webhook
chmod 0600 ~/.claude/discord-notify-webhook
```

When the daemon's down, the Stop and Notification hooks fall back to this webhook (channel root instead of a thread), so you still get pinged.

### 6. Start the daemon

**Foreground (simplest):**
```bash
uv run cc-bridge serve
```
Wait for `Bot ready as <name>, watching #<channel>`. Use `tmux` or `nohup` if you want it to outlive the shell.

**systemd user unit (survives reboots):**
```bash
uv tool install .                      # places `cc-bridge` at ~/.local/bin/
bash scripts/install-systemd-user.sh   # copies the unit file into ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cc-bridge
```
If `systemctl --user` errors with `Operation not permitted`, run `sudo loginctl enable-linger $USER` first.

**macOS launchd user agent (survives reboots and login):**

```bash
uv tool install .   # places `cc-bridge` at ~/.local/bin/
```

Write `~/Library/LaunchAgents/local.cc-bridge.plist`, replacing `<you>` with your home dir leaf:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>local.cc-bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/<you>/.local/bin/cc-bridge</string>
        <string>serve</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key>
    <string>/Users/<you>/Library/Logs/cc-bridge.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/<you>/Library/Logs/cc-bridge.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/<you>/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
```

Load it:
```bash
launchctl load -w ~/Library/LaunchAgents/local.cc-bridge.plist
```

`PATH` must include wherever your `zellij` and `claude` binaries live (Homebrew, cargo, nix) — launchd agents don't inherit your shell's `PATH`. Tail the log with `tail -f ~/Library/Logs/cc-bridge.log`. To stop: `launchctl unload ~/Library/LaunchAgents/local.cc-bridge.plist`.

### 7. Verify

```bash
uv run cc-bridge doctor
```
You should see `[ok]` for each check: secrets file present + 0600, daemon health, settings.json hooks, `/ask-discord` skill symlink, `zellij` installed, bridge session reachable, task-settings dir writable, hook scripts present, `claude` on PATH. `[fail]` lines tell you what to fix; `[warn]` lines are non-blocking.

## Usage — notification mode

Once the daemon is running, these surfaces work without further intervention:

| Surface | Discord | Mattermost |
|---|---|---|
| Long turn ping | Posts to thread | Posts to channel |
| Permission prompt ping | Posts to thread | Posts to channel |
| `/ask-discord` skill | Posts question, waits for reply | N/A (use slash commands instead) |
| Manual `POST /v1/notify` | `curl http://127.0.0.1:8787/v1/notify ...` | Same |
| Manual `POST /v1/ask` | Same, blocks for reply | Same |
| Manual `GET /v1/health` | `curl http://127.0.0.1:8787/v1/health` | Same |

**Discord:** Threads are named `cc · <cwd-leaf> · <session-prefix>`. Same `session_id` always routes to the same thread; different sessions get different threads. Mappings persist in SQLite at `~/.local/state/cc-bridge/state.db` and survive daemon restarts. Archived/deleted threads recreate transparently.

**Mattermost:** Messages post to the configured channel. The bridge does not create separate threads in Mattermost.

## Chat-driven sessions

Spawning Claude Code sessions directly from Discord slash commands or Mattermost text commands. Each task is one zellij tab in a shared session; the bridge injects task-scoped hooks via `claude --settings <path>` so it can mirror everything back to the chat.

### Discord slash commands

| Command | What it does |
|---|---|
| `/start cwd:<path> [prompt:<text>]` | Spawn a new Claude session in `cwd`, opens a fresh thread, optionally writes the initial prompt after bind. |
| `/list` | List active tasks with status, cwd leaf, age, and thread link. |
| `/stop [thread:<#thread>]` | Graceful stop — writes `/exit` to the pane, archives the thread on session end. |
| `/kill [thread:<#thread>]` | Force-close the pane — marks the task crashed, archives the thread. |
| `/restart [thread:<#thread>]` | Resume a stopped task via `claude --resume <session_id>`; reuses the existing pane if alive, otherwise spawns a fresh one. |
| `/skill <name> [args:<text>]` | Type `/<name> [args]` into the running session. Autocomplete shows installed user + plugin skills. |
| `/rename [name:<text>]` | Rename the thread; omit `name` to auto-generate via `claude -p` against the transcript. |
| `/stats [thread:<#thread>]` | Token / cost / context-fill stats for the task, parsed from its transcript. |
| `/tasks [thread:<#thread>]` | Show the session's `TaskCreate`/`TaskUpdate` mirror as an embed. |

Commands without an explicit `thread:` argument operate on the task whose thread you're invoking from.

### Mattermost text commands

| Command | What it does |
|---|---|
| `!start <cwd> [prompt]` | Spawn a new Claude session in `cwd`, optionally with initial prompt. |
| `!list` | List active tasks with status, cwd leaf, and age. |
| `!stop [task_id]` | Graceful stop — writes `/exit` to the pane. Omit `task_id` to stop the most recent. |
| `!kill [task_id]` | Force-close the pane — marks the task crashed. Omit `task_id` to kill the most recent. |
| `!restart [task_id]` | Resume a stopped task via `claude --resume <session_id>`. Omit `task_id` to restart the most recent. |
| `!skill <name> [args]` | Type `/<name> [args]` into the running session. |
| `!rename [name]` | Rename the task; omit `name` to auto-generate via `claude -p` against the transcript. |
| `!stats [task_id]` | Token / cost / context-fill stats for the task. |
| `!tasks [task_id]` | Show the session's `TaskCreate`/`TaskUpdate` mirror. |

Commands without a task_id argument operate on the most recently started task for that user.

### What gets mirrored to the thread

- Assistant text and `thinking` blocks at each tool boundary (deduped by entry uuid).
- Tool use as one-liner summaries, coalesced into bursts; `Edit` / `MultiEdit` / `Write` get a separate fenced-diff block; `TodoWrite` gets a checklist.
- Subagent activity rolls up into one live-edited embed per agent (yellow while running → green when finished).
- `AskUserQuestion` posts each question with reaction-based options (single- or multi-select); `ExitPlanMode` posts the plan with ✅/❌. Free-text replies in the thread also work.
- Voice memos are transcribed (Wispr Flow API if `WISPR_FLOW_API_TOKEN` is set, otherwise local `whisper` CLI) and inlined as `[voice memo] <text>` in the relayed prompt.
- Discord file attachments are saved under `~/.local/state/cc-bridge/attachments/<task_id>/` and their absolute paths are appended to the prompt, one per line.
- Token / cost / context-fill summary posts after every `Stop`.

### One-time setup

1. **Install `zellij` ≥ 0.44** (older versions have a teardown-race panic that takes down the whole session):
   ```bash
   nix-env -iA nixpkgs.zellij   # nix
   brew install zellij           # macOS
   cargo install zellij          # build from source
   ```
   Verify: `zellij --version`

2. **Pick a session name** (optional). The bridge defaults to `cc-bridge-worker`; override by exporting `BRIDGE_ZELLIJ_SESSION=<name>` before starting the daemon. To attach and watch tabs:
   ```bash
   zellij attach cc-bridge-worker
   ```

3. **State directories** are auto-created under `~/.local/state/cc-bridge/` (task-settings, attachments, the SQLite db). No manual setup needed.

4. **Optional: get `@`-mentioned when claude is stuck**. Export `BRIDGE_NOTIFY_USER_ID=<your-discord-user-id>` so AskUserQuestion / ExitPlanMode / free-text-stall prompts prefix with a mention.

### Configuration env vars

| Variable | Default | Purpose |
|---|---|---|
| `BRIDGE_PLATFORM` | `discord` | Which backend to use: `discord` or `mattermost`. |
| `BRIDGE_URL` | `http://127.0.0.1:8787` | Where hooks POST events. Override only if you run the daemon on a non-default port. |
| `BRIDGE_ZELLIJ_SESSION` | `cc-bridge-worker` | zellij session name the bridge spawns task tabs into. |
| `BRIDGE_NOTIFY_USER_ID` | _(unset)_ | Discord user id to `@`-mention on TUI-blocking prompts. (Discord only) |
| `BRIDGE_ATTACHMENT_TTL_SECS` | `604800` | TTL for attachment cleanup (default 7 days). |
| `BRIDGE_CONTEXT_LIMIT` | _(model default)_ | Override the per-model context window for `/stats` math (e.g. `1000000` for `[1m]`). |
| `WISPR_FLOW_API_TOKEN` | _(unset)_ | If set, voice memos use Wispr Flow's API; otherwise local `whisper`. |
| `BRIDGE_WHISPER_BIN` | `whisper` | Override the local-whisper binary path. |
| `BRIDGE_WHISPER_MODEL` | `base` | Whisper model size. |

### Verify setup

```bash
uv run cc-bridge doctor
```

Runs ten checks: secrets file present + 0600, daemon health, settings.json hooks (Stop/Notification), `/ask-discord` skill symlink, `zellij` installed, bridge session reachable, task-settings dir writable, all hook scripts present, `claude` on PATH.

## Architecture

Single-process Python daemon. `aiohttp.web.AppRunner` and a `ChatPlatform` backend (Discord or Mattermost) share one asyncio event loop. Per-session thread/post mapping lives in SQLite (WAL). Reply routing uses per-thread/task `asyncio.Lock` (FIFO) plus a sliding 3-second coalescing window so multi-message replies fold into one response.

The bridge abstracts across Discord and Mattermost via the `ChatPlatform` protocol, allowing both backends to be used interchangeably. Platform-specific implementations live in `src/bridge/backends/`.

| File | Role |
|---|---|
| `src/bridge/platform.py` | `ChatPlatform` protocol — abstraction for Discord and Mattermost |
| `src/bridge/server.py` | aiohttp app, endpoints `/v1/notify`, `/v1/ask`, `/v1/health`, `/v1/hook/event`, `/v1/hook/pretooluse` |
| `src/bridge/backends/discord/bot.py` | discord.py wrapper — chunked send, retries on 5xx, `on_message` dispatch, embed edits |
| `src/bridge/backends/mattermost/bot.py` | Mattermost WebSocket client + REST API — message posting, reaction handling, command routing |
| `src/bridge/threads.py` | session_id → thread_id (Discord) or post_id (Mattermost), with create-on-miss + recreate-on-404 |
| `src/bridge/listener.py` | Pending-ask state, sliding coalescing window, future lifecycle |
| `src/bridge/state.py` | aiosqlite — `sessions`, `tasks`, `approval_log` tables |
| `src/bridge/secrets.py` | 0600 JSON loader/writer for both Discord and Mattermost credentials |
| `src/bridge/cli.py` | click CLI: `init`, `serve`, `doctor` (handles both platforms) |
| `src/bridge/commands.py` | Discord slash-command tree |
| `src/bridge/backends/mattermost/commands.py` | Mattermost command handler (HTTP endpoint + text command parser) |
| `src/bridge/tasks.py` | `TaskRegistry`: Chat-driven task lifecycle, hook-event dispatch, transcript streaming, subagent block management, task-list mirror |
| `src/bridge/zellij.py` | Async wrapper around the `zellij` CLI (≥ 0.44 recommended) |
| `src/bridge/tool_summary.py` | One-liner formatter + fenced diff/code/checklist blocks per tool name |
| `src/bridge/transcript.py` | Bounded utf-8 JSONL reader for claude transcripts |
| `src/bridge/usage.py` | Token/cost/context-fill computation for `/stats` and Stop footer |
| `src/bridge/voice.py` | Audio transcription (Wispr Flow API or local `whisper` CLI) |
| `src/bridge/skills.py` | Enumerate user-level + enabled-plugin skills for `/skill` autocomplete |
| `src/bridge/approvals.py` | `ApprovalRouter` — PreToolUse and TUI-prompt round-trips via reactions/text |
| `hooks/notify-stop.py` | Standalone-mode Stop hook (long-turn ping) |
| `hooks/notify-notification.py` | Standalone-mode Notification hook (permission/idle ping) |
| `hooks/event.py` | Chat-driven mode multi-event dispatcher (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, `SubagentStop`, `Notification`, `SessionEnd`, `PreCompact`) |
| `hooks/pretooluse-approve.py` | Chat-driven mode PreToolUse approval wrapper (fail-closed, used selectively for `AskUserQuestion` / `ExitPlanMode`) |
| `skills/SKILL.md` | `/ask-discord` skill instructions for Claude (symlinked into `~/.claude/skills/ask-discord/` and `~/.claude/skills/ask-bridge/`) |

See `CLAUDE.md` for the full set of gotchas and invariants — start there before adding features.

## Development

```bash
uv run pytest -q --ignore=tests/test_zellij.py    # ~400 tests
uv run pytest -q tests/test_<module>.py
```

Tests use a `FakeBot` and in-memory SQLite, so the suite never hits real Discord. `tests/test_zellij.py` is excluded by default because the (older) tests in it can crash a live zellij session; run it deliberately in isolation if you need to.

## License

TBD.

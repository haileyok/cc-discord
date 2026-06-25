# claude-discord-bridge

Localhost HTTP bridge between Claude Code sessions and Discord. Long turns ping your phone, permission prompts surface in a thread, Claude can `/ask-discord <question>` when it's blocked, and you can drive whole Claude sessions from Discord slash commands without ever attaching to the terminal.

## What it does

Runs as a small Python daemon (`aiohttp` + `discord.py`) on `127.0.0.1:8787`. Two modes, share one daemon:

**Notification mode** (you run Claude in your terminal, the bridge listens):
- **Stop hook** — Pings Discord when a Claude turn took >10 minutes. Result lands in a per-session thread.
- **Notification hook** — Permission prompts and idle states surface as `⏸ awaiting input` in the same thread.
- **`/ask-discord` skill** — Claude calls this when blocked; the question lands in the thread, the daemon waits up to 15 min for your reply, and Claude continues.

**Discord-driven mode** (`/start` from Discord spawns Claude in a zellij tab):
- Slash commands manage the lifecycle: `/start`, `/spawn`, `/list`, `/stop`, `/kill`, `/restart`, `/skill`, `/rename`, `/stats`, `/tasks`, `/pin`, `/unpin`.
- The bridge mirrors assistant text, tool use (with fenced diffs for Edit/Write), subagent activity (live-updated embed per agent), and the session's task list back to its thread.
- Discord replies in the thread relay into the pane; attachments are saved and their paths get inlined into the prompt so Claude reads them with the `Read` tool. Voice memos are auto-transcribed (Wispr Flow API or local `whisper`).
- `AskUserQuestion` and `ExitPlanMode` round-trip through Discord reactions / text replies — no need to attach to the pane to answer.
- The agent can attach files back by emitting `[[attach: /absolute/path]]` markers in its replies.

A separate webhook URL at `~/.claude/discord-notify-webhook` is used as a fallback when the daemon isn't running, so you don't lose pings if you forgot to start it.

## Prereqs

- Python 3.12 managed by [uv](https://github.com/astral-sh/uv) (the repo pins it via `.python-version`).
- A Discord application with a bot, message-content intent enabled, invited to a guild you control, with permission to view + send messages + create public threads in one channel.
- [Claude Code](https://docs.claude.com/claude-code) installed.

## Setup

### 1. Discord bot

1. https://discord.com/developers/applications → **New Application** → **Bot** tab → **Reset Token**, copy it.
2. **Privileged Gateway Intents** → enable **Message Content Intent**. Save.
3. **OAuth2 → URL Generator** → scopes: `bot` → bot permissions: `View Channels`, `Send Messages`, `Create Public Threads`, `Send Messages in Threads`, `Read Message History` (+ `Manage Channels` if you'll use `/pin`) → open the generated URL → invite the bot to your server.
4. In the Discord client: User Settings → Advanced → enable **Developer Mode** → right-click the target channel → **Copy Channel ID**.

### 2. Bridge daemon

```bash
git clone https://github.com/haileyok/cc-discord.git claude-discord-bridge
cd claude-discord-bridge
uv sync
uv run claude-discord-bridge init
```

`init` prompts for the bot token and channel ID, writes `~/.config/claude-discord-bridge/secrets.json` at mode `0600`, validates the token by connecting to Discord (15s timeout), and posts a confirmation message to your channel. If the token's wrong it exits 2 and leaves the secrets file so you can fix and retry.

### 3. Wire Claude Code hooks

The Stop and Notification hooks are referenced by absolute path from `~/.claude/settings.json`. Open it and add (or merge with existing `hooks`):

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "python3 /home/<you>/claude-discord-bridge/hooks/notify-stop.py", "async": true }
        ]
      }
    ],
    "Notification": [
      {
        "hooks": [
          { "type": "command", "command": "python3 /home/<you>/claude-discord-bridge/hooks/notify-notification.py", "async": true }
        ]
      }
    ]
  }
}
```

Replace `/home/<you>/claude-discord-bridge` with the actual repo path. Validate with `python3 -m json.tool ~/.claude/settings.json > /dev/null`.

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
uv run claude-discord-bridge serve
```
Wait for `Bot ready as <name>, watching #<channel>`. Use `tmux` or `nohup` if you want it to outlive the shell.

**systemd user unit (survives reboots):**
```bash
uv tool install .                      # places `claude-discord-bridge` at ~/.local/bin/
bash scripts/install-systemd-user.sh   # copies the unit file into ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-discord-bridge
```
If `systemctl --user` errors with `Operation not permitted`, run `sudo loginctl enable-linger $USER` first.

**macOS launchd user agent (survives reboots and login):**

```bash
uv tool install .   # places `claude-discord-bridge` at ~/.local/bin/
```

Write `~/Library/LaunchAgents/local.claude-discord-bridge.plist`, replacing `<you>` with your home dir leaf:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>local.claude-discord-bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/<you>/.local/bin/claude-discord-bridge</string>
        <string>serve</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key>
    <string>/Users/<you>/Library/Logs/claude-discord-bridge.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/<you>/Library/Logs/claude-discord-bridge.log</string>
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
launchctl load -w ~/Library/LaunchAgents/local.claude-discord-bridge.plist
```

`PATH` must include wherever your `zellij` and `claude` binaries live (Homebrew, cargo, nix) — launchd agents don't inherit your shell's `PATH`. Tail the log with `tail -f ~/Library/Logs/claude-discord-bridge.log`. To stop: `launchctl unload ~/Library/LaunchAgents/local.claude-discord-bridge.plist`.

### 7. Verify

```bash
uv run claude-discord-bridge doctor
```
You should see `[ok]` for each check: secrets file present + 0600, daemon health, settings.json hooks, `/ask-discord` skill symlink, `zellij` installed, bridge session reachable, task-settings dir writable, hook scripts present, `claude` on PATH. `[fail]` lines tell you what to fix; `[warn]` lines are non-blocking.

## Usage — notification mode

Once the daemon is running, these surfaces work without further intervention:

| Surface | Trigger |
|---|---|
| Long turn ping | Run any Claude Code turn that takes >10 minutes |
| Permission prompt ping | Run any Claude Code action that needs your approval |
| `/ask-discord` from inside Claude | Ask Claude to use `/ask-discord` when it's blocked |
| Manual `POST /v1/notify` | `curl -X POST http://127.0.0.1:8787/v1/notify -H 'Content-Type: application/json' -d '{"session_id":"...","cwd":"...","message":"..."}'` |
| Manual `POST /v1/ask` | Same, but `/v1/ask` with a `question` field; blocks for the reply (default 15 min, capped at 60) |
| Manual `GET /v1/health` | `curl http://127.0.0.1:8787/v1/health` |

Threads are named `cc · <cwd-leaf> · <session-prefix>`. Same `session_id` always routes to the same thread; different sessions get different threads. Mappings persist in SQLite at `~/.local/state/claude-discord-bridge/state.db` and survive daemon restarts. Archived/deleted threads recreate transparently.

## Discord-driven sessions

Spawning Claude Code sessions directly from Discord slash commands. Each task is one zellij tab in a shared session; the bridge injects task-scoped hooks via `claude --settings <path>` so it can mirror everything back to a per-task thread.

### Slash commands

| Command | What it does |
|---|---|
| `/start cwd:<path> [prompt:<text>]` | Spawn a new Claude session in `cwd`, opens a fresh thread, optionally writes the initial prompt after bind. |
| `/spawn project:<picker> [prompt:<text>]` | Same as `/start` but the `project` arg is an autocompleted picker over immediate subfolders of `BRIDGE_PROJECT_ROOTS` — no typing paths. |
| `/list` | List active tasks with status, cwd leaf, age, and thread link. |
| `/stop [thread:<#thread>]` | Graceful stop — writes `/exit` to the pane, archives the thread on session end. |
| `/kill [thread:<#thread>]` | Force-close the pane — marks the task crashed, archives the thread. |
| `/restart [thread:<#thread>]` | Resume a stopped task via `claude --resume <session_id>`; reuses the existing pane if alive, otherwise spawns a fresh one. |
| `/skill <name> [args:<text>]` | Type `/<name> [args]` into the running session. Autocomplete shows installed user + plugin skills. |
| `/rename [name:<text>]` | Rename the thread; omit `name` to auto-generate via `claude -p` against the transcript. |
| `/stats [thread:<#thread>]` | Token / cost / context-fill stats for the task, parsed from its transcript. |
| `/tasks [thread:<#thread>]` | Show the session's `TaskCreate`/`TaskUpdate` mirror as an embed. |
| `/pin [name:<text>] [project:<picker>]` | Create a Discord channel bound to a cwd. Inside a task thread, inherits that thread's cwd; outside, the `project:` autocomplete picks one from `BRIDGE_PROJECT_ROOTS`. Subsequent messages in the new channel auto-spawn a Claude session if none is live. Requires `Manage Channels` permission. |
| `/unpin` | Remove the pin binding from the current channel (the channel itself is not deleted). Future messages won't auto-spawn. |

Commands without an explicit `thread:` argument operate on the task whose thread you're invoking from.

### What gets mirrored to the thread

- Assistant text and `thinking` blocks at each tool boundary (deduped by entry uuid).
- Tool use as one-liner summaries, coalesced into bursts; `Edit` / `MultiEdit` / `Write` get a separate fenced-diff block; `TodoWrite` gets a checklist.
- Subagent activity rolls up into one live-edited embed per agent (yellow while running → green when finished).
- `AskUserQuestion` posts each question with reaction-based options (single- or multi-select); `ExitPlanMode` posts the plan with ✅/❌. Free-text replies in the thread also work.
- Voice memos are transcribed (Wispr Flow API if `WISPR_FLOW_API_TOKEN` is set, otherwise local `whisper` CLI) and inlined as `[voice memo] <text>` in the relayed prompt.
- Discord file attachments are saved under `~/.local/state/claude-discord-bridge/attachments/<task_id>/` and their absolute paths are appended to the prompt, one per line.
- Token / cost / context-fill summary posts after every `Stop`.

### One-time setup

1. **Install `zellij` ≥ 0.44** (older versions have a teardown-race panic that takes down the whole session):
   ```bash
   nix-env -iA nixpkgs.zellij   # nix
   brew install zellij           # macOS
   cargo install zellij          # build from source
   ```
   Verify: `zellij --version`

2. **Pick a session name** (optional). The bridge defaults to `meow`; override by exporting `BRIDGE_ZELLIJ_SESSION=<name>` before starting the daemon. To attach and watch tabs:
   ```bash
   zellij attach meow
   ```

3. **State directories** are auto-created under `~/.local/state/claude-discord-bridge/` (task-settings, attachments, the SQLite db). No manual setup needed.

4. **Optional: get `@`-mentioned when claude is stuck**. Export `BRIDGE_NOTIFY_USER_ID=<your-discord-user-id>` so AskUserQuestion / ExitPlanMode / free-text-stall prompts prefix with a mention.

### Headless rendering — the phantom client

A zellij session with **no terminal client attached** doesn't render its panes
and silently drops `zellij action write-chars`: the spawned TUI (Claude Code
v2.1.x) blocks waiting on terminal queries (DA1/DA2/cursor-position) that nobody
answers, so it never paints and never receives the keystrokes the bridge relays.
The session reports a bogus screen size and relayed prompts vanish into the void.
Symptom: `/start` binds (the `SessionStart` hook fires) but the first prompt
produces no output, no tool call, no transcript — until a human runs
`zellij attach` once, at which point everything that was queued suddenly drives.

This makes a pure remote/headless deployment (drive entirely from Discord, no
local terminal) impossible without a manual attach. **The daemon fixes this
automatically**: `serve` spawns and supervises a *phantom client*
(`python -m bridge.phantom`) that allocates a PTY at a real size
(`PHANTOM_COLUMNS`×`PHANTOM_LINES`, default 200×60), claims it as a controlling
tty, and `zellij attach`es headlessly — draining all output to `/dev/null`. That
gives the session a real, sized client so panes render and keystrokes land, with
no human in the loop. It restarts if it exits and is torn down on shutdown.

Opt out with `BRIDGE_PHANTOM=0` if you already keep a real terminal attached to
the session (a second client is then redundant). The client can also be run
standalone for debugging: `python -m bridge.phantom`.

### Configuration env vars

| Variable | Default | Purpose |
|---|---|---|
| `BRIDGE_URL` | `http://127.0.0.1:8787` | Where hooks POST events. Override only if you run the daemon on a non-default port. |
| `BRIDGE_ZELLIJ_SESSION` | `meow` | zellij session name the bridge spawns task tabs into. |
| `BRIDGE_PHANTOM` | `1` | Auto-spawn the headless phantom zellij client (see below). Set `0` to disable, e.g. when you attach a real terminal yourself. |
| `PHANTOM_COLUMNS` | `200` | Phantom client PTY width. |
| `PHANTOM_LINES` | `60` | Phantom client PTY height. |
| `BRIDGE_NOTIFY_USER_ID` | _(unset)_ | Discord user id to `@`-mention on TUI-blocking prompts. |
| `BRIDGE_ATTACHMENT_TTL_SECS` | `604800` | TTL for attachment cleanup (default 7 days). |
| `BRIDGE_CONTEXT_LIMIT` | _(model default)_ | Override the per-model context window for `/stats` math (e.g. `1000000` for `[1m]`). |
| `BRIDGE_PROJECT_ROOTS` | _(unset)_ | Colon-separated parent paths whose immediate subfolders are spawnable from `/spawn`'s autocomplete picker. Example: `/Users/me/code/Work:/Users/me/code/Personal`. |
| `BRIDGE_SPAWN_BIND_TIMEOUT_SECS` | `60` | How long to wait for claude's `SessionStart` hook before giving up and relaying the user's message anyway. Cold-start with multiple MCP servers + plugin sync regularly takes 15–40s; bump higher on slow hardware. |
| `WISPR_FLOW_API_TOKEN` | _(unset)_ | If set, voice memos use Wispr Flow's API; otherwise local `whisper`. |
| `BRIDGE_WHISPER_BIN` | `whisper` | Override the local-whisper binary path. |
| `BRIDGE_WHISPER_MODEL` | `base` | Whisper model size. |

### Verify setup

```bash
uv run claude-discord-bridge doctor
```

Runs ten checks: secrets file present + 0600, daemon health, settings.json hooks (Stop/Notification), `/ask-discord` skill symlink, `zellij` installed, bridge session reachable, task-settings dir writable, all hook scripts present, `claude` on PATH.

## Architecture

Single-process Python daemon. `aiohttp.web.AppRunner` and `discord.py` share one asyncio event loop. Per-session thread mapping lives in SQLite (WAL). Reply routing uses a per-thread `asyncio.Lock` (FIFO) plus a sliding 3-second coalescing window so multi-message replies fold into one response.

| File | Role |
|---|---|
| `src/bridge/server.py` | aiohttp app, endpoints `/v1/notify`, `/v1/ask`, `/v1/health`, `/v1/hook/event`, `/v1/hook/pretooluse` |
| `src/bridge/bot.py` | discord.py wrapper — chunked send, retries on 5xx, `on_message` dispatch, embed edits |
| `src/bridge/threads.py` | session_id → thread_id with create-on-miss + recreate-on-404 |
| `src/bridge/listener.py` | Pending-ask state, sliding coalescing window, future lifecycle |
| `src/bridge/state.py` | aiosqlite — `sessions`, `tasks`, `approval_log` tables |
| `src/bridge/secrets.py` | 0600 JSON loader/writer |
| `src/bridge/cli.py` | click CLI: `init`, `serve`, `doctor` |
| `src/bridge/commands.py` | discord.py slash-command tree (`/start`, `/list`, `/stop`, `/kill`, `/restart`, `/skill`, `/rename`, `/stats`, `/tasks`) |
| `src/bridge/tasks.py` | `TaskRegistry`: Discord-driven task lifecycle, hook-event dispatch, transcript streaming, subagent block management, task-list mirror |
| `src/bridge/zellij.py` | Async wrapper around the `zellij` CLI (≥ 0.44 recommended) |
| `src/bridge/phantom.py` | Headless phantom client — attaches a sized PTY to the session so panes render without a human terminal (`serve` auto-spawns it; `BRIDGE_PHANTOM=0` to disable) |
| `src/bridge/tool_summary.py` | One-liner formatter + fenced diff/code/checklist blocks per tool name |
| `src/bridge/transcript.py` | Bounded utf-8 JSONL reader for claude transcripts |
| `src/bridge/usage.py` | Token/cost/context-fill computation for `/stats` and Stop footer |
| `src/bridge/voice.py` | Audio transcription (Wispr Flow API or local `whisper` CLI) |
| `src/bridge/skills.py` | Enumerate user-level + enabled-plugin skills for `/skill` autocomplete |
| `src/bridge/approvals.py` | `ApprovalRouter` — PreToolUse and TUI-prompt round-trips via reactions/text |
| `hooks/notify-stop.py` | Standalone-mode Stop hook (long-turn ping) |
| `hooks/notify-notification.py` | Standalone-mode Notification hook (permission/idle ping) |
| `hooks/event.py` | Discord-driven mode multi-event dispatcher (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, `SubagentStop`, `Notification`, `SessionEnd`, `PreCompact`) |
| `hooks/pretooluse-approve.py` | Discord-driven mode PreToolUse approval wrapper (fail-closed, used selectively for `AskUserQuestion` / `ExitPlanMode`) |
| `skills/SKILL.md` | `/ask-discord` skill instructions for Claude (symlinked into `~/.claude/skills/ask-discord/`) |

See `CLAUDE.md` for the full set of gotchas and invariants — start there before adding features.

## Development

```bash
uv run pytest -q --ignore=tests/test_zellij.py    # ~400 tests
uv run pytest -q tests/test_<module>.py
```

Tests use a `FakeBot` and in-memory SQLite, so the suite never hits real Discord. `tests/test_zellij.py` is excluded by default because the (older) tests in it can crash a live zellij session; run it deliberately in isolation if you need to.

## License

TBD.

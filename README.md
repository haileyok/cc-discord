# claude-discord-bridge

Drive [Polytoken](https://github.com/) agent sessions from Discord. `/start` spawns a per-session daemon, its activity mirrors into a Discord thread, and your replies in the thread become prompts — no terminal attach required.

## What it does

Runs as a small Python daemon (`aiohttp` + `discord.py`) on `127.0.0.1:8787`. Each Discord task is one **Polytoken daemon process** (`polytoken new --no-attach`) bound to a thread:

- Slash commands manage the lifecycle: `/start`, `/spawn`, `/list`, `/stop`, `/kill`, `/skill`, `/effort`, `/rename`, `/stats`, `/tasks`, `/pin`, `/unpin`.
- The bridge follows each daemon's `/events` SSE stream and mirrors assistant text, tool use (with fenced diffs for edits/writes), and subagent activity (one live-updated embed per agent) into the task's thread.
- Replies in the thread relay into the session via `POST /prompt`. Attachments are saved and referenced as `@/absolute/path` so the agent resolves them; voice memos are auto-transcribed (Wispr Flow API or local `whisper`).
- The agent's structured questions (`ask_user_question`, clarification/confirmation interrogatives) post to the thread; your next reply answers them.
- The agent can attach files back by emitting `[[attach: /absolute/path]]` markers in its replies.

Permissions run in **bypass mode** — there's no Discord approval round-trip; the daemon is trusted.

## Prereqs

- Python 3.12 managed by [uv](https://github.com/astral-sh/uv) (pinned via `.python-version`).
- A Discord application with a bot, message-content intent enabled, invited to a guild you control, with permission to view + send messages + create public threads in one channel.
- The `polytoken` binary on `PATH`.

## Setup

### 1. Discord bot

1. https://discord.com/developers/applications → **New Application** → **Bot** tab → **Reset Token**, copy it.
2. **Privileged Gateway Intents** → enable **Message Content Intent**. Save.
3. **OAuth2 → URL Generator** → scopes: `bot` → permissions: `View Channels`, `Send Messages`, `Create Public Threads`, `Send Messages in Threads`, `Read Message History` (+ `Manage Channels` if you'll use `/pin`) → open the URL → invite the bot.
4. In Discord: User Settings → Advanced → **Developer Mode** → right-click the target channel → **Copy Channel ID**.

### 2. Bridge daemon

```bash
git clone https://github.com/haileyok/cc-discord.git claude-discord-bridge
cd claude-discord-bridge
uv sync
uv run claude-discord-bridge init
```

`init` prompts for the bot token and channel ID, writes `~/.config/claude-discord-bridge/secrets.json` at mode `0600`, validates the token by connecting to Discord, and posts a confirmation message.

### 3. Start the daemon

**Foreground (simplest):**
```bash
uv run claude-discord-bridge serve
```
Wait for `Bot ready as <name>, watching #<channel>`. Use `tmux` or `nohup` to outlive the shell.

**systemd user unit (survives reboots):**
```bash
uv tool install .
bash scripts/install-systemd-user.sh
systemctl --user daemon-reload
systemctl --user enable --now claude-discord-bridge
```
If `systemctl --user` errors with `Operation not permitted`, run `sudo loginctl enable-linger $USER` first.

### 4. Verify

```bash
uv run claude-discord-bridge doctor
```
Checks: secrets file present + 0600, daemon health, the `polytoken` binary present + a headless spawn smoke, and the attachments dir writable. `[fail]` lines tell you what to fix; `[warn]` lines are non-blocking.

## Slash commands

| Command | What it does |
|---|---|
| `/start cwd:<path> [prompt:<text>]` | Spawn a new daemon session in `cwd`, open a fresh thread, optionally send the first prompt. |
| `/spawn project:<picker> [prompt:<text>]` | Same as `/start` but `project` is an autocompleted picker over immediate subfolders of `BRIDGE_PROJECT_ROOTS`. |
| `/list` | List active tasks with status, cwd leaf, age, and thread link. |
| `/stop [thread:<#thread>]` | Cancel any in-flight turn and terminate the daemon. |
| `/kill [thread:<#thread>]` | Immediately terminate the daemon. |
| `/restart [thread:<#thread>]` | Not supported with the daemon backend (headless resume isn't available); use `/kill` + `/start`. |
| `/skill <name> [args:<text>]` | Invoke a skill via an `@<name>` reference prompt. Autocomplete shows the session's available skills. |
| `/effort <level>` | Change the session's reasoning effort (re-selects the active model with the new effort). |
| `/model name:<model> [effort:<level>]` | Switch the active model (autocomplete from `polytoken models`); a bare switch resets effort to the model default, or set it inline. |
| `/facet <name>` | Switch the active facet (free-text; the daemon validates and rejects unknowns). |
| `/rename [name:<text>]` | Rename the thread; omit `name` to use the daemon's auto-generated session title. |
| `/stats [thread:<#thread>]` | Model, reasoning effort, facet, and context-window usage from the daemon's `/state`. |
| `/tasks [thread:<#thread>]` | Show the session's todo list. |
| `/pin [name:<text>] [project:<picker>]` | Create a Discord channel bound to a cwd; messages in it auto-spawn a session. Requires `Manage Channels`. |
| `/unpin` | Remove the pin from the current channel. |

Commands without an explicit `thread:` operate on the task whose thread you're invoking from.

## What gets mirrored

- Assistant text and `thinking` blocks, streamed per content block.
- Tool use as coalesced one-liner summaries; edits/writes get a fenced diff block; `TodoWrite` gets a checklist.
- Subagent activity rolls up into one live-edited embed per agent (yellow while running → green when finished), routed by the event stream's `subagent_handle`.
- Structured questions / clarification / confirmation interrogatives post to the thread; your next reply answers them.
- Voice memos transcribed (Wispr Flow API if `WISPR_FLOW_API_TOKEN` is set, else local `whisper`) and inlined as `[voice memo] <text>`.
- Discord attachments saved under `~/.local/state/claude-discord-bridge/attachments/<task_id>/` and referenced in the prompt as `@<absolute-path>`.

## Configuration env vars

| Variable | Default | Purpose |
|---|---|---|
| `BRIDGE_URL` | `http://127.0.0.1:8787` | Bridge health URL (used by `doctor`). |
| `POLYTOKEN_BIN` | `polytoken` | Override the `polytoken` binary used by `doctor`'s checks. |
| `BRIDGE_NOTIFY_USER_ID` | _(unset)_ | Discord user id to `@`-mention on questions / attention pings. |
| `BRIDGE_ATTACHMENT_TTL_SECS` | `604800` | TTL for attachment cleanup (default 7 days). |
| `BRIDGE_CONTEXT_LIMIT` | _(daemon value)_ | Override the context-window limit shown in `/stats`. |
| `BRIDGE_PROJECT_ROOTS` | _(unset)_ | Colon-separated parent paths whose immediate subfolders are spawnable from `/spawn`. |
| `WISPR_FLOW_API_TOKEN` | _(unset)_ | If set, voice memos use Wispr Flow's API; otherwise local `whisper`. |
| `BRIDGE_WHISPER_BIN` | `whisper` | Override the local-whisper binary. |
| `BRIDGE_WHISPER_MODEL` | `base` | Whisper model size. |

## Architecture

Single-process Python daemon. `aiohttp.web.AppRunner` and `discord.py` share one asyncio event loop. Task bookkeeping lives in SQLite (WAL). Each task runs one long-lived `/events` consumer that translates daemon events into Discord render actions.

| File | Role |
|---|---|
| `src/bridge/server.py` | aiohttp app (`GET /v1/health`) + the Discord message dispatcher |
| `src/bridge/bot.py` | discord.py wrapper — chunked send, retries on 5xx, `on_message` dispatch, embed edits |
| `src/bridge/polytoken_client.py` | async HTTP client for one daemon session (`/prompt`, `/events` SSE, `/state`, lifecycle) |
| `src/bridge/daemon_supervisor.py` | spawn / list / terminate Polytoken daemons via the `polytoken` CLI |
| `src/bridge/events.py` | pure `DaemonEvent` → Discord render-action translator |
| `src/bridge/tasks.py` | `TaskRegistry`: task lifecycle, per-task event consumer, prompt routing, subagent embeds |
| `src/bridge/commands.py` | discord.py slash-command tree |
| `src/bridge/state.py` | aiosqlite — `sessions`, `tasks`, `pins` tables |
| `src/bridge/tool_summary.py` | one-liner formatter + fenced diff/code/checklist blocks per tool |
| `src/bridge/usage.py` | renders `/state.context_usage` for `/stats` |
| `src/bridge/voice.py` | audio transcription (Wispr Flow API or local `whisper`) |
| `src/bridge/skills.py` | filesystem fallback for `/skill` autocomplete (primary source is `/state.available_skills`) |
| `src/bridge/secrets.py` | 0600 JSON loader/writer |
| `src/bridge/cli.py` | click CLI: `init`, `serve`, `doctor` |

See `CLAUDE.md` for the gotchas and invariants — start there before adding features.

## Development

```bash
uv run pytest -q
uv run pytest -q tests/test_<module>.py
```

Tests use `FakeBot` / `FakeSupervisor` / `FakePolytokenClient` and in-memory SQLite, so the suite never hits real Discord or a real daemon.

## License

TBD.

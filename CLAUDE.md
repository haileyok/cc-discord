# cc-bridge

Localhost HTTP bridge between Claude Code sessions and Discord or Mattermost. Single-process Python daemon — `aiohttp` server and a `ChatPlatform` backend (Discord or Mattermost) share one asyncio event loop.

Freshness: 2026-05-11

## Repo location and tooling

This repo lives at `/home/discord/cc-discord` (or locally at the user's clone). **Outside** the `/home/discord/discord` monorepo if running on the coder workstation. `clyde`, `clint`, the monorepo's pre-commit hooks, and Buildkite CI do not apply here. Don't import from or symlink into the monorepo.

Python is pinned to 3.12 via `uv` (`.python-version`). The system `python3` is 3.10 — always invoke through uv:

- Tests: `uv run pytest` (not `pytest`)
- Run daemon in foreground: `scripts/run-foreground.sh` (uses `uv run`)

## Gotchas

### Discord-specific

- **`MESSAGE_CONTENT` privileged intent.** `src/bridge/backends/discord/bot.py` sets `intents.message_content = True` because reply routing reads message text. The bot user in the Discord Developer Portal must have this intent enabled, or `on_message` payloads arrive empty. The `init` wizard prints a reminder; agents adding new gateway features should not forget it.

### Mattermost-specific

- **Emoji name ↔ Unicode mapping.** Mattermost uses emoji names (e.g., `+1`) while the bridge internally uses Unicode strings (e.g., `👍`). The mapping is inline in `backends/mattermost/bot.py` (`_emoji_to_mattermost` / `_mattermost_to_emoji`); keep it in sync if adding new reaction types.
- **Markdown formatting differences.** Mattermost's markdown is slightly different from Discord. The bridge uses `backends/mattermost/formatting.py` to convert Claude's Markdown. Some features like fancy embeds don't map 1:1.
- **Token-based auth, not intents.** Mattermost uses a Personal Access Token (PAT) in the `Authorization: Bearer <token>` header, not OAuth intents. PATs can be scoped; the minimum scope for the bridge is `post:channels`.
- **`AllowedUntrustedInternalConnections` required for localhost webhooks.** Mattermost blocks outgoing HTTP requests to localhost and private IPs by default. If the bridge runs on the same host, the Mattermost admin must add `127.0.0.1 localhost` to `ServiceSettings.AllowedUntrustedInternalConnections` in `config.json` (or via System Console > Environment > Developer). Without this, all slash commands fail with "command with a trigger of '/X' failed".
- **Slash commands via `/v1/slash/{command}` routes.** `cc-bridge register-slash-commands` creates `/start`, `/stop`, `/kill`, `/list`, `/restart`, `/skill`, `/retitle`, `/stats`, `/tasks` in Mattermost pointing at the bridge's HTTP server. Each POSTs to `/v1/slash/<name>`. The verification token is stored in `secrets.json` as `slash_command_token`. Mattermost slash commands don't provide thread context (`root_id`), only `channel_id` — thread-scoped commands require an explicit `task_id` argument. The route is only registered when `platform == "mattermost"`. Text commands (`!start`, `!stop`, etc.) still work in parallel.

### Platform-agnostic

- **`BRIDGE_CLAUDE_COMMAND` controls the launcher binary.** Default: `"claude"`. Set to a shlex-compatible command string to use a wrapper like `claude-mode`. Include the `--` separator when using claude-mode so bridge-appended flags (`--settings`, `--resume`, `--dangerously-skip-permissions`) pass through correctly. Example: `BRIDGE_CLAUDE_COMMAND="claude-mode extend --"`. The `generate_thread_name()` one-shot (`claude -p`) is unaffected — it always uses bare `claude`.
- **Hooks must always exit 0.** `hooks/notify-stop.py` and `hooks/notify-notification.py` wrap `main()` in `try/except: pass; finally: sys.exit(0)` on purpose — a Claude Code Stop/Notification hook that fails non-zero degrades the user's session. Preserve that contract when editing.
- **FIFO ordering for `/v1/ask` is enforced by `AskLockMap`, not `Listener`.** `Listener.register()` raises `RuntimeError` if a thread already has a pending ask — this is an invariant guard, not the queueing mechanism. The per-thread `asyncio.Lock` in `server.AskLockMap` must be acquired *before* posting the question and *released* after `unregister`. Any new `/v1/ask`-style endpoint must follow the same lock-then-register pattern.
- **Single event loop, shared by aiohttp + discord.py.** Long blocking work (sync DB calls, `time.sleep`, `requests`) inside any handler starves both the HTTP server and the Discord gateway. Use the async equivalents (`aiosqlite`, `asyncio.sleep`, `aiohttp` client).
- **`SKILL.md` in `skills/` is symlinked into `~/.claude/skills/ask-discord/SKILL.md`.** Edit the file in this repo; the live skill follows. Don't duplicate.
- **`cli doctor` checks settings.json hook paths against `bridge.__file__`.** If you `uv tool install .` the bridge into `~/.local/bin`, `bridge.__file__` resolves into the uv tool venv, not this repo. The doctor's hook-path check expects `<repo>/hooks/notify-*.py` paths in `~/.claude/settings.json` to match wherever the package is currently importing from. Run `doctor` from the same install you registered hooks against.
- **Task-scoped settings via `--settings` flag, not env var.** Discord-driven sessions (`/start` slash command) generate a per-task settings file at `~/.local/state/cc-bridge/task-settings/<task_id>.json` and pass it via `claude --settings <path>`. Hooks **accumulate** (merge), not override — the user's existing `~/.claude/settings.json` hooks (e.g. `notify-stop.py`, `notify-notification.py`) still fire alongside the task-scoped hooks. The bridge's `event.py` hook is idempotent, so duplicate fires from both sources are harmless.
- **PreToolUse is registered ONLY for `AskUserQuestion` and `ExitPlanMode`.** `_on_pre_tool_use` dispatches `_handle_ask_user_question` / `_handle_exit_plan_mode` directly from the hook body's structured `tool_input` — that's the *only* moment we have access to the question text and option list (Claude doesn't flush the tool_use to the JSONL until the user answers, and the Notification body just says "Claude Code needs your attention" with no question content). The hook returns no `permissionDecision` so the tool still proceeds to TUI block; the user's Discord reaction reply gets typed into the pane via the existing TUI handler flow. All *other* PreToolUse traffic is unhooked so auto-mode's classifier still drives approvals. `_on_notification` skips dispatch when a TUI handler is already running, so PreToolUse + the trailing Notification don't double-post.
- **Bridge zellij session is shared and configurable.** Default name is `cc-bridge-worker`; override with `BRIDGE_ZELLIJ_SESSION`. Each task is one named tab (`cc-<task_id_prefix>`). Don't `zellij kill-session <name>` while tasks are running — it kills every tab at once and the bridge will mark them all `crashed` on next event.
- **Self-attach panic.** zellij ≥ 0.43 panics if the daemon calls `zellij attach --create-background <name>` for the session it's already running inside. `zellij._running_inside_target_session()` checks `ZELLIJ_SESSION_NAME` and skips the attach when colocated. New code that wants to ensure the session is alive should call `ensure_session_alive()`, not invoke attach directly.
- **zellij is client-server: env on the `zellij run` subprocess is invisible to the spawned command.** The spawned process inherits the *server's* env (set when the user originally started zellij), not the bridge daemon's env. To inject task vars, use the `env(1)` prefix in the spawn argv — `ZellijManager.spawn_task` does this for `CC_BRIDGE_TASK_ID` / `BRIDGE_URL` (backward-compat fallback to `CC_DISCORD_TASK_ID`). Do not rely on `subprocess.Popen(..., env=...)` for anything that needs to reach the spawned pane.
- **`load_from_db` defers Discord posts to `flush_startup_notices()`.** Reconcile-against-zellij happens before the HTTP server accepts requests, but the Discord bot logs in later — so `self._bot` is `None` during reconcile. Stage notices via `_pending_startup_notices`; the caller flushes them after `bot.is_ready`. Don't add `self._bot.*` calls inside the reconcile branch.
- **Assistant content streams at PostToolUse boundaries, not all at Stop.** `_stream_assistant_progress` walks the transcript at each `PostToolUse` and again at `Stop`, posting each new assistant entry's `text`/`thinking` blocks. Per-task `Task.posted_assistant_uuids` dedupes on entry uuid — cleared on `UserPromptSubmit` and `SessionStart` (new turn). The set lives in memory only; if the bridge restarts mid-turn, the new daemon may re-post entries already seen. Acceptable trade-off for the simpler design.
- **Stop hook fires before Claude flushes the final assistant entry.** `_on_stop` calls `_wait_for_transcript_stable` (waits up to `_STOP_TRANSCRIPT_RETRY_SECS`, default 10s, for the file's size to stay constant for 250ms) before the final stream pass. Tests override the retry seconds to 0.0 when verifying static-transcript branches.
- **Edit / MultiEdit / Write get a fenced diff/content block alongside the one-liner summary.** `tool_summary.diff_block` produces the block; `_post_tool_diff` sends it as a separate Discord post (the aggregator coalesces summaries; diffs are individual messages). Bodies truncate at ~1920 chars to stay under Discord's 2000-char limit.
- **Subagent activity is collated into per-agent live-edited Discord embeds.** Each subagent (Claude's `agentId`) gets one embed (title = attribution, description = last 5 actions, footer = "running|finished · N actions · Ns", color = yellow→green) edited in place. Modern CC writes subagent activity to separate `<session>/subagents/agent-*.jsonl` files; `_refresh_subagent_blocks` scans them on each PostToolUse / Stop / SubagentStop, creates a `SubagentBlock` per file, and `Bot.edit_message(embed=…)` updates the running embed. Edits are throttled to ~1.5s per block to stay under Discord's per-channel edit rate limit. PostToolUse events classified as sidechain (via `_is_sidechain_tool`, which also checks subagent files) are suppressed from the main aggregator. Blocks are cleared on UserPromptSubmit / SessionStart.
- **TodoWrite renders as a checklist alongside the one-liner summary.** `tool_summary.diff_block("TodoWrite", input)` formats the `todos` list into ✅/▶️/⬜ marks with content text; `_post_tool_diff` emits it as a separate Discord message after the aggregated summary line. Subagent TodoWrite calls only surface inside the subagent block as `• 📋 TodoWrite: N/M done` (no full checklist) to keep blocks compact.
- **Discord attachments are saved under `~/.local/state/cc-bridge/attachments/<task_id>/` and their absolute paths are appended to the relayed user message** — one per line, no `- ` bullet prefix (a leading dash is fatal to `zellij action write-chars`, see the zellij architecture entry). Claude reads them with the `Read` tool (which handles images, PDFs, JSON, plain text). Filenames are sanitized to basename and prefixed with `msg_id` to avoid collisions. No size cap beyond Discord's own — large files just stream through `Attachment.read()`.
- **Audio attachments are split off and transcribed before reaching the agent.** `voice.transcribe()` auto-selects a backend: if `WISPR_FLOW_API_TOKEN` is set it uses the Wispr Flow REST API (16kHz PCM WAV, base64-encoded, `POST https://api.wisprflow.ai/transcribe`); otherwise it shells out to a local CLI (default `whisper` from `pip install -U openai-whisper`, override binary via `BRIDGE_WHISPER_BIN`, model via `BRIDGE_WHISPER_MODEL`, default `base`). Successful transcriptions become `[voice memo] <text>` blocks in the relayed prompt — they do NOT appear in the attached-files list. On failure (no backend, ffmpeg missing, CLI missing, HTTP error, timeout) we fall back to `[voice memo received — transcription unavailable; raw file: <path>]` so the user knows it didn't transcribe but the file is on disk.
- **Agent → Discord file attachments use the `[[attach: <path>]]` marker.** When a streamed assistant `text` block contains `[[attach: /absolute/path]]`, `_parse_attach_markers` strips the marker, resolves the path (must be absolute and exist), and `Bot.post_with_attachments` uploads up to 10 files per Discord message alongside the cleaned text. Useful for screenshots / generated images / log dumps the agent wants to surface visually. Convention is *not* auto-injected into the agent — instruct claude per-conversation, or add the convention to the project's CLAUDE.md.
- **Attachment cleanup is paired with task-settings cleanup via `_cleanup_task_artifacts(task_id)`.** Every lifecycle terminal (stop, kill, crash, archive) calls it so the two on-disk artifacts can't drift apart. In addition, `sweep_old_attachments()` runs at daemon startup and hourly, deleting any file older than `BRIDGE_ATTACHMENT_TTL_SECS` (default 7 days) and removing the now-empty per-task dir.
- **`/rename` auto-generates names by shelling out to `claude -p`.** `TaskRegistry.generate_thread_name` reads the first user prompt + first assistant response from the transcript, builds a short kebab-case naming prompt, and runs `claude -p <prompt>` in a subprocess (30s timeout). Uses whatever auth/model the user's `claude` CLI is configured with — no separate Anthropic API key needed. Empty stdout / non-zero exit → returns None and the slash command surfaces a "pass a name explicitly" error.
- **`/restart` uses `--settings` + `--resume`.** The `/restart <task-id>` command spawns a new pane with both `claude --settings <path>` (to wire the task hooks) and `claude --resume <session_id>` (to pick up from the prior session). Don't manually delete `~/.claude/projects/...` for a session the bridge is using.

## Deployment paths

The systemd unit at `packaging/cc-bridge.service` hardcodes `%h/.local/bin/cc-bridge` — it assumes `uv tool install .`, not `uv run`. The two install paths are not interchangeable.

`systemctl --user` is **not** available on the coder workstation by default ("Operation not permitted"). The verified-working path is `scripts/run-foreground.sh` under tmux/nohup. To use real systemd, the user must first run `loginctl enable-linger $USER`.

### Docker (qcluster-1 / headless)

The Docker container runs the bridge as PID 1 *outside* zellij. Zellij is started in the background via `attach --create-background`. This means **`action write-chars` silently fails** — it returns 0 but doesn't deliver keystrokes because no terminal client is attached to the session. The recommended deployment is bare-metal with a persistent zellij client (e.g. `tmux new -d 'zellij attach cc-bridge-worker'`). Claude is always spawned interactively; prompts from `!start` are delivered via `write_initial_prompt` after SessionStart fires. Both initial and follow-up messages require an attached zellij client.

`MattermostMessageAdapter` (in `backends/mattermost/bot.py`) wraps Mattermost post dicts into `MessageLike`-compatible objects so the platform-agnostic dispatcher and task router can use attribute access. If you add new fields the dispatcher reads from messages, update the adapter too.

Production integration testing against the live Mattermost instance is documented in `plans/mattermost-production-testing.md` — covers token creation, API calls, log interpretation, zellij diagnostics, and hotpatching.

## Architecture quick reference

### Platform abstraction

- `src/bridge/platform.py` — `ChatPlatform` protocol defines the interface both Discord and Mattermost backends must implement. Methods: `connect()`, `disconnect()`, `post_message()`, `edit_message()`, `delete_message()`, `add_reaction()`, etc. Structured to allow swapping backends at runtime via `BRIDGE_PLATFORM` env var. All IDs (`thread_id`, `message_id`) are `str` at the protocol boundary — Discord's backend converts to/from `int` internally.

### Server and state

- `src/bridge/server.py` — aiohttp app, endpoints `/v1/notify`, `/v1/ask`, `/v1/health`, `/v1/hook/event`, `/v1/hook/pretooluse`. `AskLockMap` lives here. Platform-agnostic.
- `src/bridge/state.py` — aiosqlite, WAL mode. Tables: `sessions`, `tasks`, `approval_log`. Platform-agnostic.
- `src/bridge/threads.py` — `ThreadRegistry` owns session_id→thread_id (Discord) or post_id (Mattermost) mapping with 404 recovery. Single global lock is intentional (per-session contention is rare).
- `src/bridge/listener.py` — sliding-window coalescing for `/v1/ask` replies. `GRACE_SECS = 3.0` default; tests override. Platform-agnostic.
- `src/bridge/secrets.py` — 0600 JSON at `~/.config/cc-bridge/secrets.json`. Holds both Discord token and Mattermost PAT depending on platform.

### Task and command handling

- `src/bridge/tasks.py` — `TaskRegistry` for chat-driven sessions. Owns task lifecycle, hook-event dispatch, typing/tool-summary/transcript relay, startup reconciliation against zellij. Platform-agnostic.
- `src/bridge/command_handlers.py` — Shared command logic (start, stop, rename, restart, stats, etc.) used by both backend command dispatchers.
- `src/bridge/backends/discord/commands.py` — Discord slash-command tree (discord.py `app_commands` dispatcher). Delegates to `command_handlers`.
- `src/bridge/backends/mattermost/commands.py` — Mattermost command handler (text command parser). Delegates to `command_handlers`.

### Backend implementations

- `src/bridge/backends/discord/bot.py` — `discord.py` wrapper. `DiscordBot` implements `ChatPlatform`. `_chunk()` and `_extract_images()` are lifted verbatim from `/home/discord/victrola/src/discord_bot/bot.py` — keep them in sync if upstream changes. `_with_retry` wraps every `fetch_channel` / `target.send` call with bounded backoff (4 attempts) on `DiscordServerError` / `ClientConnectionError` so transient Discord 5xx waves don't drop user-facing posts (incl. AskUserQuestion notifications).
- `src/bridge/backends/mattermost/bot.py` — `MattermostBot` implements `ChatPlatform`. WebSocket client + REST API wrapper. Handles incoming events (messages, reactions) and outgoing posts/edits.
- `src/bridge/backends/mattermost/formatting.py` — Markdown formatting converter (Claude Markdown → Mattermost Markdown).
- `src/bridge/backends/mattermost/rich_formatter.py` — `MattermostRichFormatter`: renders rich content (task lists, subagent blocks, todos) as Mattermost markdown.
- `src/bridge/backends/mattermost/ws.py` — WebSocket client for Mattermost real-time events.
- `src/bridge/backends/mattermost/api.py` — REST API wrapper for Mattermost HTTP endpoints.

### Common utilities

- `src/bridge/zellij.py` — async wrapper around the `zellij` CLI (≥ 0.44 recommended; 0.43 still works in degraded mode). Each task is a named tab in the configured session, opened via `new-tab --layout <kdl>` so the tab spawns with claude as its only pane (no default shell). `tasks._write_task_layout` generates the per-task KDL file at `~/.local/state/cc-bridge/task-settings/<task_id>.kdl` with `env K=V ... claude <args>` baked into the layout (env vars can't be passed via `zellij run` because zellij is client-server — the spawned process inherits the *server's* env); `_kdl_quote` strips control bytes from interpolated values so a malformed env/argv field can't smuggle a literal newline / NUL / ESC through the parser. `write_to_pane` types content via `action write-chars` per segment; multi-line content is wrapped in bracketed paste (`ESC[200~ … ESC[201~`) so embedded newlines don't submit, LF (`10`) separates segments inside the paste, and trailing CR (`13`) submits the prompt. Single-line bodies that start with `-` are auto-routed through paste-mode by `write_to_pane`, because zellij's argparse otherwise eats `-`-leading args as flags — but **anyone calling `action write-chars` directly still needs to avoid leading `-`** (e.g. raw paste segments inside `_action_write_bytes`). The body is also stripped of C0/C1 control bytes (except LF/TAB) before sending so a hostile Discord message can't terminate paste mode early or smuggle CSI/OSC sequences. `list_panes` uses zellij 0.44's `action list-panes --json --state --tab` to report real `exited` status per tab; on older zellij it falls back to `query-tab-names` and `exited` is hardcoded False.
- `src/bridge/approvals.py` — `ApprovalRouter` for PreToolUse round-trips (reactions/text → hook decision, platform-agnostic) and `Notification` TUI prompts (AskUserQuestion / ExitPlanMode / free-text).
- `src/bridge/usage.py` — token-usage / cost stats from transcript. Hardcoded `MODEL_PRICES` (Anthropic list rates) and `MODEL_CONTEXT` maps; refresh when prices or models change. `[1m]` alias in `~/.claude/settings.json` auto-detected at module import as the default 1M context limit (overrides per-model defaults). `BRIDGE_CONTEXT_LIMIT` env var always wins. Footer posts after Stop; `/stats` slash command shows on demand.
- `src/bridge/skills.py` — enumerates available skills (user-level under `~/.claude/skills/` plus enabled-plugin skills resolved from `~/.claude/plugins/installed_plugins.json`) for autocomplete. Plugin skills are exposed as `<plugin>:<skill>` to match Claude Code's own naming.
- `src/bridge/tool_summary.py` — One-liner formatter + fenced diff/code/checklist blocks. Platform-agnostic.
- `src/bridge/transcript.py` — Bounded utf-8 JSONL reader for claude transcripts. Platform-agnostic.
- `src/bridge/voice.py` — Audio transcription (Wispr Flow API or local `whisper` CLI). Platform-agnostic.

### Hooks and skills

- `hooks/` — Claude Code hooks. `notify-stop.py` / `notify-notification.py` are the standalone-bridge hooks. `event.py` (multi-event dispatcher) and `pretooluse-approve.py` (fail-closed approval wrapper) are the chat-driven-session hooks injected via `--settings`. All post to `BRIDGE_URL` (default `http://127.0.0.1:8787`); the notify hooks fall back to a Discord webhook URL at `~/.claude/discord-notify-webhook` if the bridge is down and Discord is the platform.
- `skills/` — `/ask-discord` skill. `SKILL.md` is symlinked into `~/.claude/skills/ask-discord/` and `~/.claude/skills/ask-bridge/` (for backward compatibility).

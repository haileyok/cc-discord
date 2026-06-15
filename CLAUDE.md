# claude-discord-bridge

Localhost bridge between **Polytoken daemon sessions** and Discord. Single-process Python daemon — `aiohttp` server and `discord.py` client share one asyncio event loop. Each Discord task is one Polytoken daemon process; the bridge drives it over HTTP (`POST /prompt`) and follows its `GET /events` SSE stream.

Freshness: 2026-06-15

## Repo location and tooling

This repo lives at `/home/discord/claude-discord-bridge`, **outside** the `/home/discord/discord` monorepo. `clyde`, `clint`, the monorepo's pre-commit hooks, and Buildkite CI do not apply here. Don't import from or symlink into the monorepo.

Python is pinned to 3.12 via `uv` (`.python-version`). The system `python3` is 3.10 — always invoke through uv:

- Tests: `uv run pytest` (not `pytest`)
- Run daemon in foreground: `scripts/run-foreground.sh` (uses `uv run`)

## Architecture

```
Discord ──(discord.py)──> bot.py / commands.py            [Discord surface]
                              │
                       TaskRegistry (tasks.py)
              ┌───────────────┴────────────────┐
   inbound: POST /prompt            outbound: GET /events (SSE, seq-tracked)
              │                                │
        polytoken_client.py  ◄──────►  events.py translator ──► tool_summary / embeds
              │
       daemon_supervisor.py  (polytoken new --no-attach; terminate; reconcile via `polytoken sessions`)
              ▼
   one polytoken daemon per task  (127.0.0.1:PORT, own agent loop, bypass perms)
```

## Gotchas

- **`MESSAGE_CONTENT` privileged intent.** `bot.py` sets `intents.message_content = True` because reply routing reads message text. The bot user in the Discord Developer Portal must have this intent enabled, or `on_message` payloads arrive empty.
- **Single event loop, shared by aiohttp + discord.py.** Long blocking work (sync DB calls, `time.sleep`, `requests`) inside any handler starves both the HTTP server and the Discord gateway. Use the async equivalents (`aiosqlite`, `asyncio.sleep`, the `aiohttp`-based `PolytokenClient`).
- **One daemon per task; the port is runtime-only.** A task row stores `polytoken_session_id` and `port`. The port is captured at spawn (`polytoken new --no-attach` prints `session_id=<id> port=<port>`) and re-discovered on bridge restart via `polytoken sessions` (a fixed-width table; the supervisor splits the last column on maxsplit=4 so project paths with spaces survive). Per-task daemons are separate processes that **survive a bridge restart** — `load_from_db(reconcile_with_daemons=True)` re-attaches the event consumer to live sessions and marks missing ones `crashed`.
- **`subagent_handle` is the routing key.** Almost every `DaemonEvent` carries an optional `subagent_handle`. Set ⇒ the activity belongs to a spawned subagent and routes to that subagent's live embed; unset ⇒ main session, routes to the thread aggregator. This replaces the old JSONL sidechain heuristic. Do not infer subagent membership any other way.
- **Bypass permission mode — no approval UX.** The daemon is trusted; permission interrogatives never need a user answer (`events.py` suppresses `interrogative_type == "permission"`). There is no Discord reaction/approval round-trip. Only `ask_user_question`, `clarification`, and `confirmation` interrogatives surface to the user.
- **A pending interrogative consumes the next plain-text reply.** When the daemon asks a question, `TaskRegistry` stashes a `PendingInterrogative` on the task; the user's next text-only message in the thread is interpreted as the answer (`POST /interrogative/{id}/respond`) instead of a new prompt. Numeric replies select an option; otherwise free text is sent. A reply carrying attachments/voice is treated as a normal prompt and the pending question is re-stashed.
- **Attachments become `@`-references, not inlined paths.** `maybe_route_message` saves Discord attachments under `~/.local/state/claude-discord-bridge/attachments/<task_id>/` and appends `@<absolute-path>` tokens to the prompt content; the daemon resolves them (emitting `image_reference_resolved`). The reference syntax is `@<path>` (confirmed via the daemon's `Message` schema). Audio attachments are split off and transcribed by `voice.transcribe()` into `[voice memo] <text>` segments before reaching the daemon.
- **Agent → Discord file attachments use the `[[attach: <path>]]` marker.** When a streamed assistant text block contains `[[attach: /absolute/path]]`, `_parse_attach_markers` strips it, resolves the path (must be absolute and exist), and `Bot.post_with_attachments` uploads up to 10 files alongside the cleaned text.
- **Tool-name adapter.** Polytoken tool names are snake_case (`file_read`, `shell_exec`, `file_edit_search_replace`, …); `tool_summary` is keyed on Claude Code names (`Read`, `Bash`, `Edit`). `events.py::_adapt_tool` maps name + input-field differences so the existing summarizer renders sensible one-liners; unknown tools fall through to the generic formatter.
- **SSE resume is gap-free by design.** `PolytokenClient.stream_events(last_seq=...)` sends `Last-Event-ID`, so on reconnect the daemon replays after the last seen `seq`. The translator emits a `Reconcile` action on a seq gap or `stream_discontinuity`; today that's a logged backstop because `Last-Event-ID` resume already covers the gap. The consumer reconnects with exponential backoff (capped 10s) when the stream drops.
- **`/stop` and `/kill` both terminate the daemon; `/restart` is unsupported.** Headless resume isn't available in polytoken 0.1.20 (`polytoken continue` requires a TTY). `/stop` cancels any in-flight turn then terminates; `/kill` terminates immediately; `/restart` returns a clear "not supported" message. There is no "stop but keep the daemon idle" state.
- **`/effort` re-selects the active model.** Polytoken effort is a reasoning variant on the model, so `set_effort` reads `/state.active_model` and re-POSTs `/model` with the new `reasoning_effort`. The daemon validates the level against the model's capabilities and falls back gracefully.
- **Dollar cost is not derivable.** `/state.context_usage` is `{used_tokens, limit_tokens}` (context-window occupancy only); there are no per-turn token-usage events. `/stats` shows model + effort + context window; `usage.MODEL_PRICES` is kept dormant for if/when per-turn token counts become available.
- **`load_from_db` defers Discord posts to `flush_startup_notices()`.** Reconcile-against-`polytoken sessions` happens before the bot logs in, so staged notices are flushed after `bot.is_ready`. Don't add `self._bot.*` calls inside the reconcile branch.
- **Attachment cleanup + sweep.** Terminal lifecycle paths call `_cleanup_task_attachments(task_id)`; `sweep_old_attachments()` runs at startup and hourly, deleting files older than `BRIDGE_ATTACHMENT_TTL_SECS` (default 7 days).

## Schema

`state.py` (aiosqlite, WAL). Tables: `sessions`, `tasks`, `pins`.

`tasks` columns: `task_id, thread_id, cwd, status, polytoken_session_id, port, created_at, last_activity`. `init_schema` runs `_migrate_legacy_tasks`: if a pre-daemon `tasks` table is detected (any of `zellij_pane_id` / `current_claude_session_id` / `current_transcript_path`), it is **dropped and recreated** (and `approval_log` is dropped). In-flight zellij tasks don't migrate to daemons, and the DB is disposable bookkeeping, so a clean recreate is intentional.

## Deployment paths

The systemd unit at `packaging/claude-discord-bridge.service` hardcodes `%h/.local/bin/claude-discord-bridge` — it assumes `uv tool install .`, not `uv run`. The two install paths are not interchangeable.

`systemctl --user` is **not** available on the coder workstation by default ("Operation not permitted"). The verified-working path is `scripts/run-foreground.sh` under tmux/nohup. To use real systemd, run `loginctl enable-linger $USER` first.

The `polytoken` binary must be on `PATH` for the daemon process (the supervisor shells out to it). launchd/systemd units don't inherit your shell `PATH` — set it explicitly in the unit.

## Architecture quick reference

- `src/bridge/server.py` — aiohttp app, single endpoint `GET /v1/health`, plus `make_message_dispatcher` (routes Discord messages to `TaskRegistry.maybe_route_message`).
- `src/bridge/bot.py` — discord.py wrapper. `_with_retry` wraps every `fetch_channel` / `send` with bounded backoff on transient 5xx. `post`, `post_with_attachments`, `post_embed`, `edit_message`, `rename_thread`, `create_thread`, `create_channel`.
- `src/bridge/polytoken_client.py` — async (aiohttp) client for one daemon session: `prompt`, `stream_events` (SSE async-gen yielding `SseEnvelope`, seq + `Last-Event-ID`), `state`, `history`, `respond_interrogative`, `set_title`/`set_model`/`set_facet`, `cancel_turn`, `compact`, `terminate`, `health`.
- `src/bridge/daemon_supervisor.py` — `spawn(cwd)` (parses `session_id=…port=…`), `list_sessions()`, `find_session()`, `terminate(session_id)`. Injectable subprocess runner + client factory for tests.
- `src/bridge/events.py` — pure translator: stateful `Translator.handle(envelope) -> list[Action]`. Buffers text/thinking content blocks, pairs `tool_call`/`tool_result` by `call_id`, routes by `subagent_handle`, maps interrogatives, emits `Reconcile` on gaps. `Action` is a small typed set `TaskRegistry` pattern-matches on.
- `src/bridge/tasks.py` — `TaskRegistry`: spawn-via-supervisor, one `_consume_events` task per active task driving `_render(action)`, prompt routing, subagent embeds, interrogative replies, stop/kill via terminate, reconcile via `polytoken sessions`.
- `src/bridge/commands.py` — discord.py slash-command tree (`/start`, `/spawn`, `/list`, `/stop`, `/kill`, `/restart`, `/skill`, `/effort`, `/rename`, `/stats`, `/tasks`, `/pin`, `/unpin`).
- `src/bridge/state.py` — aiosqlite. Tables `sessions`, `tasks`, `pins`.
- `src/bridge/tool_summary.py` — pure one-liner formatter + fenced diff/code/checklist blocks, keyed on Claude tool names (fed via `events.py`'s adapter).
- `src/bridge/usage.py` — `format_state_summary(state)` for `/stats` from `/state.context_usage`. `MODEL_PRICES` dormant.
- `src/bridge/voice.py` — audio transcription (Wispr Flow API or local `whisper` CLI).
- `src/bridge/skills.py` — filesystem fallback for `/skill` autocomplete; the primary source is the session's `/state.available_skills`.
- `src/bridge/secrets.py` — 0600 JSON at `~/.config/claude-discord-bridge/secrets.json`.
- `src/bridge/cli.py` — click CLI: `init`, `serve`, `doctor` (checks secrets, daemon health, the `polytoken` binary + a headless spawn smoke, attachments dir).
- `src/bridge/threads.py`, `src/bridge/listener.py`, `src/bridge/transcript.py` — retained but no longer on the hot path (threads/listener were the old hook-notify/ask machinery; `MessageLike` still lives in `listener.py`).

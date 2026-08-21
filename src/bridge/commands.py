"""Slack commands, shortcuts, modals, and task-root controls.

The command layer deliberately does not depend on a Slack web framework.  The
Socket Mode adapter hands this module either a raw Socket Mode envelope or a
normalized payload; :class:`CommandDispatcher` acknowledges the envelope first
and then runs the potentially slow registry operation.
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from bridge import skills, usage
from bridge.domain import ConversationKey
from bridge.redaction import redact, safe_error
from bridge.tasks import (
    Task,
    TaskNotFound,
    TaskPrivilegeError,
    TaskRegistry,
    TaskRestartError,
    TaskRoutingError,
    TaskSpawnError,
    normalize_message,
)

log = logging.getLogger(__name__)

_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max", "none")
_ACKED = "_bridge_acknowledged"


@dataclass(frozen=True)
class SlackResponse:
    """A transport-neutral response returned by :meth:`dispatch`.

    ``text`` and ``blocks`` are useful to tests and adapters which do not expose
    a response URL.  ``ephemeral`` expresses the intended visibility; Slack's
    response URL or Web API adapter is responsible for enforcing it.
    """

    text: str = ""
    blocks: list[dict[str, Any]] | None = None
    ephemeral: bool = True
    modal: dict[str, Any] | None = None


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _actor_id(payload: Mapping[str, Any]) -> str:
    user = payload.get("user")
    if isinstance(user, Mapping):
        return str(user.get("id") or user.get("user_id") or "")
    return str(payload.get("user_id") or payload.get("actor_id") or user or "")


def _channel_id(payload: Mapping[str, Any]) -> str:
    channel = payload.get("channel")
    if isinstance(channel, Mapping):
        return str(channel.get("id") or "")
    return str(payload.get("channel_id") or channel or "")


def _team_id(payload: Mapping[str, Any], bot: Any = None) -> str:
    team = payload.get("team")
    if isinstance(team, Mapping):
        team = team.get("id")
    return str(payload.get("team_id") or team or getattr(bot, "team_id", "") or "")


def _root_ts(payload: Mapping[str, Any]) -> str:
    message = payload.get("message")
    if isinstance(message, Mapping):
        return str(
            payload.get("root_ts")
            or payload.get("thread_ts")
            or message.get("thread_ts")
            or message.get("ts")
            or payload.get("message_ts")
            or ""
        )
    return str(
        payload.get("root_ts")
        or payload.get("thread_ts")
        or payload.get("message_ts")
        or payload.get("ts")
        or ""
    )


def _json_or_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return {}


def normalize_socket_payload(envelope: Any) -> dict[str, Any]:
    """Normalize Slack Socket Mode and direct callback fixtures.

    Slack sends interactive payloads as JSON strings in ``payload`` while slash
    commands and events are often mappings.  This function accepts both forms
    and preserves ``envelope_id``, an optional ack callback, and response
    callbacks for the dispatcher.
    """
    if not isinstance(envelope, Mapping):
        raise ValueError("malformed Slack envelope: expected object")
    raw = dict(envelope)
    payload_value = raw.get("payload", raw)
    payload = _json_or_mapping(payload_value)
    if not payload:
        raise ValueError("malformed Slack envelope payload")
    # Socket Mode wraps event callbacks in payload.event; retain the event type
    # while leaving event fields at the top level for a simple registry contract.
    event = payload.get("event")
    if isinstance(event, Mapping):
        normalized = dict(event)
        normalized.update({k: v for k, v in payload.items() if k != "event"})
        normalized["type"] = "events" if normalized.get("type") == "event_callback" else normalized.get("type", "events")
    else:
        normalized = dict(payload)
    envelope_type = str(raw.get("type") or payload.get("type") or "")
    if envelope_type in {"slash_commands", "slash_command", "command"} or "command" in payload:
        normalized.setdefault("type", "slash_commands")
    elif envelope_type in {"interactive", "block_actions", "view_submission", "shortcut", "shortcuts"}:
        normalized.setdefault("type", str(payload.get("type") or envelope_type or "interactive"))
    elif envelope_type in {"events_api", "event_callback", "events"} or isinstance(event, Mapping):
        normalized.setdefault("type", "events")
    normalized.setdefault("envelope_id", raw.get("envelope_id"))
    if str(normalized.get("type") or "") == "view_submission":
        view = _mapping(normalized.get("view"))
        metadata = _json_or_mapping(view.get("private_metadata"))
        if metadata.get("channel_id") and not normalized.get("channel_id"):
            normalized["channel_id"] = str(metadata["channel_id"])
        user = normalized.get("user")
        if isinstance(user, Mapping) and user.get("id"):
            normalized.setdefault("actor_id", str(user["id"]))
    for key in ("ack", "respond", "response_url", "trigger_id"):
        if key in raw and key not in normalized:
            normalized[key] = raw[key]
    normalized["_raw_envelope"] = raw
    return normalized


def _text_field(values: Mapping[str, Any], *names: str) -> str:
    """Read a Slack modal state value by action_id."""
    for block in values.values():
        if not isinstance(block, Mapping):
            continue
        for action_key, action in block.items():
            if not isinstance(action, Mapping):
                continue
            # Real view_submission payloads key actions by action_id and do
            # not necessarily repeat action_id inside the action object.
            action_id = str(action.get("action_id") or action_key or "")
            if action_id in names or any(name in action_id for name in names):
                value = action.get("value")
                if value is None:
                    selected = action.get("selected_option")
                    value = selected.get("value") if isinstance(selected, Mapping) else ""
                return str(value or "").strip()
    return ""


def _resolve_model_name(requested: str, available: list[str]) -> tuple[str | None, list[str]]:
    """Resolve a free-text `/agent model <name>` argument against the daemon's
    real model registry (e.g. ``router/gpt-5.6-sol:api``).

    Users naturally type short names like ``sol`` rather than the full
    router id. Passing that straight to the daemon fails outright (the
    daemon rejects unknown model ids), so without resolution the switch
    silently does nothing useful. Returns ``(resolved_id, candidates)``:
    on success ``candidates`` is empty; on failure ``resolved_id`` is
    ``None`` and ``candidates`` lists near matches for the error hint.
    """
    query = str(requested or "").strip()
    if not query:
        return None, []
    if not available:
        # The model registry could not be fetched; trust the caller's input
        # rather than blocking every model switch on a transient listing
        # failure.
        return query, []
    for candidate in available:
        if candidate == query:
            return candidate, []
    lowered = query.lower()
    for candidate in available:
        if candidate.lower() == lowered:
            return candidate, []
    substring_matches = [c for c in available if lowered in c.lower()]
    if len(substring_matches) == 1:
        return substring_matches[0], []
    if substring_matches:
        return None, sorted(substring_matches)
    return None, []


def _resolve_working_directory(value: str, projects: list[Any]) -> tuple[str | None, str]:
    """Resolve user-entered paths and configured project aliases tolerantly."""
    requested = str(value or "").strip().strip("`\"'").strip()
    if not requested:
        return None, ""
    key = requested.casefold()
    for project in projects:
        path = Path(str(getattr(project, "path", ""))).expanduser()
        aliases = {
            str(getattr(project, "name", "")),
            path.name,
            str(path),
            f"{getattr(project, 'root_label', '')}/{getattr(project, 'name', '')}",
        }
        if key in {alias.strip().casefold() for alias in aliases if alias.strip()} and path.is_dir():
            return str(path.resolve()), requested
    expanded = Path(os.path.expandvars(requested)).expanduser()
    if expanded.is_dir():
        return str(expanded.resolve()), requested
    return None, requested


def _plain_text(value: Any, limit: int = 75) -> str:
    """Return text safe for Slack's option/button text limits."""
    text = " ".join(str(value or "").split())
    return text[:limit]


def _static_options(values: Any, current: str = "") -> list[dict[str, Any]]:
    """Build at most 100 unique Slack select options, retaining the initial value."""
    raw_values: list[Any] = []
    if isinstance(values, (list, tuple)):
        raw_values.extend(values)
    if current and current not in raw_values:
        raw_values.insert(0, current)
    options: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_values:
        if isinstance(raw, Mapping):
            value = str(raw.get("value") or raw.get("name") or raw.get("id") or "").strip()
            label = raw.get("text") or raw.get("label") or value
            if isinstance(label, Mapping):
                label = label.get("text") or label.get("value") or value
        else:
            value = str(raw or "").strip()
            label = value
        if not value or value in seen:
            continue
        seen.add(value)
        options.append({"text": {"type": "plain_text", "text": _plain_text(label)}, "value": value[:2000]})
        if len(options) >= 100:
            break
    return options


def _select_element(action_id: str, options: list[dict[str, Any]], current: str, *, placeholder: str) -> dict[str, Any]:
    element: dict[str, Any] = {
        "type": "static_select",
        "action_id": action_id,
        "options": options,
        "placeholder": {"type": "plain_text", "text": _plain_text(placeholder)},
    }
    initial = next((option for option in options if option.get("value") == current), None)
    if initial is not None:
        # Slack requires initial_option to be one of options, not merely equal
        # by value.  Reuse the exact object to keep that invariant obvious.
        element["initial_option"] = initial
    return element


def build_task_root_blocks(task_id: str, *, mode: str = "personal") -> list[dict[str, Any]]:
    """Return stable Block Kit controls for a task's root message."""
    value = str(task_id)
    first = [
        ("task.compact", "Compact", "primary"),
        ("task.stop", "Stop", "danger"),
        ("task.kill", "Kill", "danger"),
    ]
    second = [
        ("task.participants", "Participants", None),
        ("task.configure", "Configure", None),
    ]

    def button(action_id: str, text: str, style: str | None) -> dict[str, Any]:
        item: dict[str, Any] = {
            "type": "button",
            "action_id": action_id,
            "text": {"type": "plain_text", "text": text},
            "value": value,
        }
        if style:
            item["style"] = style
        if action_id == "task.clear":
            item["confirm"] = {
                "title": {"type": "plain_text", "text": "Clear context?"},
                "text": {"type": "mrkdwn", "text": "This removes the current model context while retaining durable session history. This cannot be undone from Slack."},
                "confirm": {"type": "plain_text", "text": "Clear context"},
                "deny": {"type": "plain_text", "text": "Cancel"},
                "style": "danger",
            }
        return item

    return [
        {"type": "actions", "block_id": f"task-controls-{value}", "elements": [button(*item) for item in first]},
        {"type": "actions", "block_id": f"task-controls-more-{value}", "elements": [button(*item) for item in second]},
        {"type": "actions", "block_id": f"task-controls-context-{value}", "elements": [button("task.clear", "Clear context", "danger")]},
    ]


# Public spelling used by a few adapters.
task_root_blocks = build_task_root_blocks


class CommandDispatcher:
    """Route Slack slash commands and interactivity to a TaskRegistry."""

    def __init__(self, bot: Any, registry: TaskRegistry, projects: list[Any] | None = None,
                 *, ack: Callable[[str], Awaitable[Any]] | None = None) -> None:
        self.bot = bot
        self.registry = registry
        self.projects = list(projects or [])
        self._ack_callback = ack
        self._models_cache: list[str] = []

    async def _ack(self, envelope: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
        # Bot versions which own Socket Mode acknowledgement mark the callback
        # payload.  Never acknowledge an already-acked envelope a second time.
        if payload.get(_ACKED) or envelope.get(_ACKED) or payload.get("acknowledged") or envelope.get("acknowledged"):
            return
        callback = payload.get("ack") or envelope.get("ack") or self._ack_callback
        envelope_id = str(envelope.get("envelope_id") or payload.get("envelope_id") or "")
        if callable(callback):
            result = callback(envelope_id) if envelope_id else callback()
            if inspect.isawaitable(result):
                await result
        elif envelope_id:
            bot_ack = getattr(self.bot, "ack", None)
            if callable(bot_ack):
                result = bot_ack(envelope_id)
                if inspect.isawaitable(result):
                    await result

    async def handle_socket_envelope(self, envelope: Any) -> SlackResponse:
        raw = envelope if isinstance(envelope, Mapping) else {}
        payload = normalize_socket_payload(envelope)
        await self._ack(raw, payload)
        return await self._dispatch(payload)

    async def dispatch(self, payload: Any) -> SlackResponse:
        """Dispatch an already normalized payload (or normalize a raw one)."""
        raw = payload if isinstance(payload, Mapping) else {}
        normalized = normalize_socket_payload(payload)
        await self._ack(raw, normalized)
        return await self._dispatch(normalized)

    def _require_ingress(self, payload: Mapping[str, Any]) -> None:
        expected = str(getattr(self.bot, "team_id", "") or "")
        supplied = _team_id(payload, self.bot) if payload.get("team_id") or payload.get("team") else ""
        if not expected or not supplied or supplied != expected:
            raise TaskRoutingError("unauthenticated or mismatched Slack team")

    async def _dispatch(self, payload: Mapping[str, Any]) -> SlackResponse:
        kind = str(payload.get("type") or "").lower()
        self._require_ingress(payload)
        try:
            if kind in {"slash_commands", "slash_command", "command"}:
                return await self._slash(payload)
            if kind in {"interactive", "block_actions", "view_submission", "shortcut", "shortcuts", "message_action"}:
                return await self._interactive(payload)
            if kind in {"events", "event_callback", "message"} or isinstance(payload.get("event"), Mapping):
                return await self._event(payload)
            # A direct payload containing a command is a convenient test and
            # webhook contract even if type was omitted.
            if payload.get("command"):
                return await self._slash(payload)
            return await self._error(payload, "Unsupported Slack interaction. Use `/agent help`.")
        except (TaskPrivilegeError, PermissionError) as exc:
            return await self._error(payload, "Not allowed. Only the task owner may use this control.")
        except (TaskNotFound, TaskSpawnError, TaskRestartError, TaskRoutingError, ValueError) as exc:
            # These exception types are raised exclusively by the bridge's own
            # code with already-deliberate, human-readable reasons (e.g.
            # "daemon rejected model change", "Unknown model `sol`..."). Using
            # safe_error() here discarded that specific reason every time and
            # showed the same unhelpful generic text regardless of what
            # actually happened. redact() keeps the message but still strips
            # any accidental token/URL/path (e.g. a raw cwd in a spawn error).
            message = redact(str(exc)).strip() or "Could not complete that request"
            return await self._error(payload, message)
        except Exception as exc:  # interaction handlers must not crash Socket Mode
            log.error("Slack interaction failed: %s", safe_error(exc, "interaction failed"))
            return await self._error(payload, "Unexpected bridge error. Try again or check the task status.")

    async def handle_interaction(self, payload: Any) -> SlackResponse:
        """Authenticated public entrypoint for interactive payloads."""
        return await self.dispatch(payload)

    async def handle_command(self, payload: Any) -> SlackResponse:
        """Authenticated public entrypoint for slash commands."""
        return await self.dispatch(payload)

    async def handle_event(self, payload: Any) -> SlackResponse:
        """Authenticated public entrypoint for Events API payloads."""
        return await self.dispatch(payload)

    async def _slash(self, payload: Mapping[str, Any]) -> SlackResponse:
        command = str(payload.get("command") or "")
        text = str(payload.get("text") or "").strip()
        if command and command.rstrip("/").split("/")[-1] not in {"agent", "start", "spawn", "list", "stop", "kill", "restart", "reload", "skill", "model", "facet", "effort", "title", "rename", "stats", "todos", "tasks", "pin", "unpin"}:
            return await self._error(payload, "Unknown command. Try `/agent help`.")
        if command.rstrip("/").split("/")[-1] != "agent" and command:
            text = " ".join([command.lstrip("/"), text]).strip()
        try:
            tokens = shlex.split(text)
        except ValueError as exc:
            return await self._error(payload, "Invalid command quoting. Check quoting and try again.")
        if not tokens:
            return await self._reply(payload, self._help_text())
        name = tokens.pop(0).lower()
        args = self._parse_args(tokens)
        actor = _actor_id(payload)
        if name in {"help", "?"}:
            return await self._reply(payload, self._help_text())
        if name in {"start", "spawn"}:
            self._configured_owner(actor)
            return await self._start(payload, name, args, tokens, actor)
        if name == "list":
            self._configured_owner(actor)
            tasks = await self.registry.list_tasks(actor)
            if not tasks:
                return await self._reply(payload, "No active tasks.")
            lines = ["*Active tasks:*"]
            for task in tasks:
                lines.append(f"• `{task.task_id[:8]}` · `{Path(task.cwd).name or '/'}` · {task.status} · <#{task.channel_id}>")
            return await self._reply(payload, "\n".join(lines))
        if name == "restart":
            task = await self._task_from_payload(payload, actor, require_owner=True)
            return await self._reply(payload, f"❌ Restart is unsupported for `{task.task_id[:8]}`; use `/agent kill` then `/agent start` (headless resume is unavailable).")
        if name in {"stop", "kill", "compact", "reload", "skill", "model", "facet", "effort", "title", "rename", "stats", "todos", "tasks", "pin", "unpin"}:
            return await self._session_command(payload, name, args, tokens, actor)
        return await self._error(payload, "Unknown `/agent` command. Try `/agent help`.")

    @staticmethod
    def _parse_args(tokens: list[str]) -> dict[str, str]:
        args: dict[str, str] = {}
        positional: list[str] = []
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token.startswith("--"):
                token = token[2:]
            if "=" in token:
                key, value = token.split("=", 1)
                args[key.strip().lower()] = value
            elif token.startswith("-") and len(token) > 1:
                args[token.lstrip("-").lower()] = tokens[i + 1] if i + 1 < len(tokens) else ""
                i += 1
            else:
                positional.append(token)
            i += 1
        args["_positional"] = " ".join(positional)
        return args

    def _configured_owner(self, actor: str) -> None:
        owner = str(getattr(self.bot, "owner_user_id", "") or "")
        if owner and actor != owner:
            raise TaskPrivilegeError(f"actor {actor or '<unknown>'} is not the configured owner")

    async def _start(self, payload: Mapping[str, Any], name: str, args: dict[str, str], tokens: list[str], actor: str) -> SlackResponse:
        if name == "spawn":
            project = args.get("project") or args.get("name") or args.get("_positional", "")
            selected = next((p for p in self.projects if f"{getattr(p, 'root_label', '')}/{getattr(p, 'name', '')}" == project or getattr(p, "name", "") == project), None)
            if selected is None:
                return await self._error(payload, "`spawn` needs a configured project name. Use `/agent spawn project=<name>`.")
            cwd = str(selected.path)
        else:
            cwd = args.get("cwd") or args.get("_positional", "")
        if not cwd:
            return await self._error(payload, "This command needs a configured working directory.")
        prompt = args.get("prompt")
        team = _team_id(payload, self.bot)
        channel = _channel_id(payload) or str(getattr(self.bot, "home_channel_id", ""))
        task = await self.registry.spawn_task(cwd, team_id=team, channel_id=channel, owner_user_id=actor, prompt=prompt)
        await self._decorate_root(task)
        return await self._reply(
            payload,
            f"✅ Started task `{task.task_id[:8]}` in `{Path(task.cwd).name}`. "
            "Open the task message in the channel and choose *Reply in thread* to work with the agent.",
        )

    async def _session_command(self, payload: Mapping[str, Any], name: str, args: dict[str, str], tokens: list[str], actor: str) -> SlackResponse:
        task = await self._task_from_payload(payload, actor, require_owner=True)
        # Slash-command payloads carry no message/thread context, so the
        # Socket Mode adapter cannot populate root_ts for them. Now that a
        # task is resolved, thread every reply below into its root message
        # so confirmations/errors are visible in the agent-view Messages tab
        # (which silently drops ephemeral replies) instead of vanishing.
        payload = {
            **dict(payload),
            "root_ts": task.control_message_ts or task.root_ts,
            "channel_id": task.channel_id,
        }
        if name == "stop":
            ok = await self.registry.stop_task(task.task_id, actor)
            return await self._reply(payload, f"✅ Stopped `{task.task_id[:8]}`" if ok else f"⚠️ Could not terminate `{task.task_id[:8]}`; it remains active.")
        if name == "kill":
            ok = await self.registry.kill_task(task.task_id, actor)
            return await self._reply(payload, f"💥 Killed `{task.task_id[:8]}`" if ok else f"⚠️ Could not terminate `{task.task_id[:8]}`; it remains active.")
        if name == "compact":
            outcome = await self.registry.request_compaction(task.task_id, owner_user_id=actor)
            if outcome == "queued":
                return await self._reply(payload, f"🕒 Compaction queued for `{task.task_id[:8]}`; it will run when the active turn finishes.")
            return await self._reply(payload, f"🧹 Compaction accepted for `{task.task_id[:8]}`; completion will appear in the activity stream.")
        if name == "reload":
            result = await self.registry.reload_daemon(task.task_id, owner_user_id=actor)
            failed = result.get("failed") if isinstance(result, Mapping) else None
            if failed:
                names = ", ".join(str(item) for item in failed)
                return await self._reply(payload, f"⚠️ Reloaded `{task.task_id[:8]}` with failures: {names}.")
            return await self._reply(payload, f"♻️ Reloaded daemon configuration for `{task.task_id[:8]}`.")
        if name == "skill":
            skill_name = args.get("name") or (args.get("_positional", "").split(maxsplit=1) or [""])[0]
            skill_args = args.get("args")
            if not skill_name:
                return await self._error(payload, "`skill` needs a skill name.")
            await self.registry.invoke_skill(task.task_id, skill_name, skill_args, owner_user_id=actor)
            return await self._reply(payload, f"✅ Sent `@{skill_name}` to `{task.task_id[:8]}`.")
        if name == "model":
            requested = args.get("name") or args.get("_positional", "")
            if not requested:
                return await self._error(payload, "`model` needs a model name.")
            available = await self.registry.list_models(actor)
            model, candidates = _resolve_model_name(requested, available)
            if model is None:
                if candidates:
                    hint = f" Did you mean: {', '.join(f'`{c}`' for c in candidates[:5])}?"
                else:
                    hint = ""
                return await self._error(payload, f"Unknown model `{requested}`.{hint}")
            await self.registry.set_model(task.task_id, model, owner_user_id=actor, reasoning_effort=args.get("effort"))
            return await self._reply(payload, f"🔧 Switched `{task.task_id[:8]}` to `{model}`.")
        if name == "facet":
            facet = args.get("name") or args.get("_positional", "")
            if not facet:
                return await self._error(payload, "`facet` needs a facet name.")
            await self.registry.set_facet(task.task_id, facet, owner_user_id=actor)
            return await self._reply(payload, f"🎭 Switched `{task.task_id[:8]}` to facet `{facet}`.")
        if name == "effort":
            level = args.get("level") or args.get("_positional", "")
            if level not in _EFFORT_LEVELS:
                return await self._error(payload, f"Effort must be one of: {', '.join(_EFFORT_LEVELS)}.")
            await self.registry.set_effort(task.task_id, level, owner_user_id=actor)
            return await self._reply(payload, f"⚙️ Set effort to `{level}`.")
        if name in {"title", "rename"}:
            title = args.get("name") or args.get("title") or args.get("_positional", "")
            if args.get("_positional") and (args.get("name") or args.get("title")):
                title = f"{title} {args['_positional']}"
            if not title:
                title = await self.registry.generate_root_title(task.task_id, owner_user_id=actor)
            title = " ".join((title or "").split())[:100]
            if not title:
                return await self._error(payload, "No title is available; pass `name=<title>`." )
            await self.registry.set_title(task.task_id, title, owner_user_id=actor)
            # Preserve the existing visible root title where the root is
            # bot-authored; the registry synchronizes Polytoken + Agent session.
            if not bool(getattr(task, "mention_required", False)):
                await self.bot.edit_message(task.channel_id, task.root_ts, text=title)
            return await self._reply(payload, f"✏️ Renamed task to `{title}`.")
        if name in {"stats", "todos", "tasks"}:
            state = await self.registry.get_state(task.task_id, actor)
            if not state:
                return await self._error(payload, "Could not reach the task daemon; try again shortly.")
            if name == "stats":
                return await self._reply(payload, usage.format_state_summary(state))
            return await self._reply(payload, _format_todos(state.get("todos") or []))
        if name == "pin":
            text = args.get("text") or args.get("_positional", "")
            if not text:
                return await self._error(payload, "`pin` needs text to pin.")
            key = ConversationKey(task.team_id, task.channel_id, task.root_ts)
            pin = await self.registry.pin_channel(key, text, actor)
            return await self._reply(payload, f"📌 Pinned `{pin.pin_id}`: {text}")
        if name == "unpin":
            pin_id = args.get("id") or args.get("pin") or args.get("_positional", "")
            if not pin_id:
                return await self._error(payload, "`unpin` needs a pin id.")
            key = ConversationKey(task.team_id, task.channel_id, task.root_ts)
            removed = await self.registry.unpin_channel(key, actor, pin_id)
            return await self._reply(payload, "📍 Unpinned." if removed else "That pin was not found.")
        raise ValueError("unknown command")

    async def _interactive(self, payload: Mapping[str, Any]) -> SlackResponse:
        kind = str(payload.get("type") or "interactive").lower()
        if kind == "view_submission" or payload.get("view"):
            return await self._view_submission(payload)
        if kind in {"shortcut", "shortcuts"} or payload.get("callback_id") and not payload.get("actions"):
            return await self._shortcut(payload)
        actions = payload.get("actions") or []
        if isinstance(actions, Mapping):
            actions = [actions]
        if not actions:
            return await self._error(payload, "This interaction did not include an action. Refresh and try again.")
        action = actions[0] if isinstance(actions[0], Mapping) else {}
        return await self._block_action(payload, action)

    async def _block_action(self, payload: Mapping[str, Any], action: Mapping[str, Any]) -> SlackResponse:
        action_id = str(action.get("action_id") or action.get("name") or "")
        value = _json_or_mapping(action.get("value"))
        task_id = str(value.get("task_id") or payload.get("task_id") or (action.get("value") if isinstance(action.get("value"), str) and not value else ""))
        actor = _actor_id(payload)
        if action_id.startswith("interrogative") or payload.get("interrogative_id"):
            return await self._answer_from_interaction(payload, action, actor)
        task = await self._task_from_payload(payload, actor, task_id=task_id or None, require_owner=True)
        operation = action_id.rsplit(".", 1)[-1].rsplit(":", 1)[-1].lower()
        if operation == "feedback":
            rating = str(value.get("rating") or "").lower()
            if rating not in {"positive", "negative"}:
                return await self._error(payload, "That feedback action is invalid.")
            log.info("Slack agent response feedback task=%s rating=%s", task.task_id[:8], rating)
            return await self._reply(payload, "Thanks for the feedback.")
        if operation in {"stop", "kill"}:
            ok = await (self.registry.stop_task(task.task_id, actor) if operation == "stop" else self.registry.kill_task(task.task_id, actor))
            return await self._reply(payload, f"{'✅ Stopped' if operation == 'stop' and ok else '💥 Killed' if operation == 'kill' and ok else '⚠️ Termination rejected'} `{task.task_id[:8]}`.")
        if operation == "compact":
            outcome = await self.registry.request_compaction(task.task_id, owner_user_id=actor)
            if outcome == "queued":
                return await self._reply(payload, "🕒 Compaction queued; it will run when the active turn finishes.", ephemeral=False)
            return await self._reply(payload, "🧹 Compaction accepted; Slack will post when it completes.", ephemeral=False)
        if operation == "clear":
            await self.registry.clear_context(task.task_id, owner_user_id=actor)
            return await self._reply(payload, "🗑️ Context cleared. Durable session history remains on disk.")
        if operation == "stats":
            state = await self.registry.get_state(task.task_id, actor)
            return await self._reply(payload, usage.format_state_summary(state or {}))
        if operation in {"todos", "tasks"}:
            state = await self.registry.get_state(task.task_id, actor)
            return await self._reply(payload, _format_todos((state or {}).get("todos") or []))
        if operation == "promote":
            promoted = await self.registry.promote_task(task.task_id, actor)
            await self._decorate_root(promoted)
            return await self._reply(payload, f"🤝 Promoted `{promoted.task_id[:8]}` to collaborative mode in <#{promoted.channel_id}>.")
        if operation == "participants":
            return await self._open_participants_modal(payload, task)
        if operation == "configure":
            state = await self.registry.get_state(task.task_id, actor)
            if not state:
                return await self._error(payload, "Could not reach the task daemon; try again shortly.")
            models = await self.registry.list_models(actor)
            return await self._open_modal(payload, _configure_modal(task, state, models))
        if operation in {"activity", "subagents"}:
            return await self._reply(payload, _format_subagent_activity(task))
        if operation == "title":
            return await self._reply(payload, "Use `/agent title name=<new title>` to rename this task.")
        return await self._error(payload, "Unknown task control. Refresh the task root.")

    async def _shortcut(self, payload: Mapping[str, Any]) -> SlackResponse:
        callback = str(payload.get("callback_id") or payload.get("callback") or "agent")
        actor = _actor_id(payload)
        if callback == "start_agent_here":
            self._configured_owner(actor)
            team = _team_id(payload, self.bot)
            channel = _channel_id(payload)
            root = _root_ts(payload)
            if not team or not channel or not root:
                return await self._error(payload, "Start agent here needs the selected message's exact thread.")
            membership = getattr(self.bot, "is_channel_member", None)
            if callable(membership):
                is_member = membership(channel)
                if inspect.isawaitable(is_member):
                    is_member = await is_member
                if not is_member:
                    return await self._open_modal(payload, _invite_bot_modal())
            return await self._open_modal(payload, _start_here_modal(team=team, channel=channel, root=root))
        if callback not in {"agent", "bridge.agent", "agent_shortcut", "task_agent", "agent_global"}:
            return await self._error(payload, "Unknown shortcut.")
        task_id = ""
        message = payload.get("message")
        if isinstance(message, Mapping):
            task_id = str(message.get("task_id") or "")
        view = _agent_modal(task_id=task_id, channel_id=_channel_id(payload), root_ts=_root_ts(payload))
        return await self._open_modal(payload, view)

    async def _view_submission(self, payload: Mapping[str, Any]) -> SlackResponse:
        view = _mapping(payload.get("view")) or payload
        callback = str(view.get("callback_id") or payload.get("callback_id") or "")
        values = _mapping(_mapping(view.get("state")).get("values"))
        metadata = _json_or_mapping(view.get("private_metadata"))
        actor = _actor_id(payload) or str(_mapping(payload.get("user")).get("id") or _mapping(view.get("user")).get("id") or "")
        # View submissions omit top-level conversation context. Restore the
        # channel captured when the modal was opened so confirmations/errors
        # can be delivered ephemerally without recursive response failures.
        if metadata.get("channel_id") or metadata.get("root_ts"):
            payload = {
                **dict(payload),
                "channel_id": str(metadata.get("channel_id") or payload.get("channel_id") or ""),
                "root_ts": str(metadata.get("root_ts") or payload.get("root_ts") or ""),
                "actor_id": actor,
            }
        if callback in {"bridge.start_agent_here", "start_agent_here_modal"}:
            self._configured_owner(actor)
            team = str(metadata.get("team_id") or "")
            channel = str(metadata.get("channel_id") or "")
            root = str(metadata.get("root_ts") or "")
            if not team or not channel or not root or _team_id(payload, self.bot) != team:
                raise TaskRoutingError("selected Slack thread binding is invalid")
            existing = self.registry.get_by_conversation(team, channel, root)
            if existing is None:
                restore = getattr(self.registry, "restore_by_conversation", None)
                if callable(restore):
                    existing = await restore(team, channel, root)
            if existing is not None and existing.status in {"spawning", "running", "paused", "rebinding", "promoting"}:
                return await self._reply(
                    payload,
                    f"✅ This thread already has active task `{existing.task_id[:8]}`.",
                )
            requested = _text_field(values, "cwd", "project")
            prompt = _text_field(values, "initial_prompt", "prompt")
            cwd, normalized = _resolve_working_directory(requested, self.projects)
            if cwd is None:
                choices = sorted({str(getattr(project, "name", "")) for project in self.projects if getattr(project, "name", "")})
                choice_hint = f" Available projects: {', '.join(choices[:8])}." if choices else ""
                shown = _plain_text(normalized or "(empty)", 160)
                return await self._error(
                    payload,
                    f"Working directory or project `{shown}` was not found.{choice_hint}",
                )
            task = await self.registry.spawn_task(
                cwd, team_id=team, channel_id=channel, owner_user_id=actor,
                root_ts=root, prompt=prompt or None, bind_existing_root=True,
            )
            return await self._reply(payload, f"✅ Started `{task.task_id[:8]}` in this thread.")
        if callback in {"bridge.agent", "agent_modal", "bridge.command"}:
            command = _text_field(values, "command", "agent_command")
            text = _text_field(values, "text", "agent_text")
            command = command or "agent"
            if text:
                command = f"{command} {text}"
            return await self._slash({**dict(payload), "type": "slash_commands", "command": "/agent", "text": command, "user_id": actor, **metadata})
        task_id = str(metadata.get("task_id") or _text_field(values, "task_id"))
        if callback in {"bridge.configure", "configure_modal"}:
            task = await self._task_from_payload(payload, actor, task_id=task_id, require_owner=True)
            model = _text_field(values, "configure_model", "model")
            effort = _text_field(values, "configure_effort", "effort")
            facet = _text_field(values, "configure_facet", "facet")
            skill = _text_field(values, "configure_skill", "skill")
            current_model = str(metadata.get("current_model") or "")
            current_effort = str(metadata.get("current_effort") or "")
            current_facet = str(metadata.get("current_facet") or "")
            current_skill = str(metadata.get("current_skill") or "")
            changed: list[str] = []
            model_changed = bool(model and model != current_model)
            effort_changed = bool(effort and effort != current_effort)
            if model_changed:
                await self.registry.set_model(
                    task.task_id, model, owner_user_id=actor,
                    reasoning_effort=effort or None,
                )
                changed.append("model")
            elif effort_changed:
                await self.registry.set_effort(task.task_id, effort, owner_user_id=actor)
                changed.append("effort")
            if facet and facet != current_facet:
                await self.registry.set_facet(task.task_id, facet, owner_user_id=actor)
                changed.append("facet")
            if skill and skill != current_skill:
                await self.registry.invoke_skill(task.task_id, skill, owner_user_id=actor)
                changed.append("skill")
            if not changed:
                return await self._reply(payload, f"No configuration changes for `{task.task_id[:8]}`.")
            return await self._reply(payload, f"✅ Updated `{task.task_id[:8]}`: {', '.join(changed)}.")
        if callback in {"bridge.participants", "participants_modal"}:
            task = await self._task_from_payload(payload, actor, task_id=task_id, require_owner=True)
            add_ids = _text_field(values, "participants", "participant_ids", "add_participants")
            entries = [item.strip() for item in add_ids.replace(",", " ").split() if item.strip()]
            for entry in entries:
                parts = entry.split(":", 1)
                participant = parts[0].strip()
                kind = parts[1].strip().lower() if len(parts) == 2 else "human"
                if not participant or kind not in {"human", "app"}:
                    raise ValueError("participants must use actor_id[:human|app]")
                adder = self.registry.add_participant
                result = adder(task.task_id, actor, participant, display_name=None, kind=kind)
                if inspect.isawaitable(result):
                    await result
            return await self._reply(payload, f"✅ Updated participants for `{task.task_id[:8]}`.")
        return await self._error(payload, "Unknown modal submission; close it and try again.")

    async def _answer_from_interaction(self, payload: Mapping[str, Any], action: Mapping[str, Any], actor: str) -> SlackResponse:
        task = await self._task_from_payload(payload, actor, task_id=str(payload.get("task_id") or "") or None, require_owner=False)
        answer = str(action.get("value") or payload.get("answer") or payload.get("text") or "").strip()
        await self._answer(task, actor, answer, str(payload.get("interrogative_id") or ""))
        return await self._reply(payload, "✅ Answer sent.")

    async def _answer(self, task: Task, actor: str, text: str, interrogative_id: str = "") -> None:
        pending_for = getattr(self.registry, "_pending_for", None)
        answer = getattr(self.registry, "_answer_interrogative", None)
        if not callable(pending_for) or not callable(answer):
            raise TaskRoutingError("interrogative answers are unavailable")
        pending = await pending_for(task, actor)
        if pending is None or (interrogative_id and str(pending.interrogative_id) != interrogative_id):
            raise TaskRoutingError("no pending question is addressed to you")
        await answer(task, pending, text)

    async def _task_from_payload(self, payload: Mapping[str, Any], actor: str, *, task_id: str | None = None, require_owner: bool) -> Task:
        team = _team_id(payload, self.bot)
        channel = _channel_id(payload)
        root = _root_ts(payload)
        # A task id is an explicit capability, but its binding must still match
        # any supplied conversation context. Otherwise require the complete,
        # exact (team, channel, root) binding. Never select an arbitrary task by
        # channel alone.
        if task_id:
            task = self.registry.get_by_task_id(task_id)
            if task is None:
                raise TaskNotFound("unknown task id")
            if (channel or root) and (team, channel, root) != (task.team_id, task.channel_id, task.root_ts):
                raise TaskRoutingError("task id does not match the supplied Slack conversation")
        else:
            if not team or not channel or not root:
                raise TaskRoutingError("task controls require task_id or exact team/channel/root context")
            task = self.registry.get_by_conversation(team, channel, root)
        if task is None:
            raise TaskNotFound("no task is bound to this Slack root message")
        if str(task.team_id) != team:
            raise TaskRoutingError("task belongs to another Slack team")
        if require_owner:
            checker = getattr(self.registry, "_require_owner", None)
            if callable(checker):
                checker(task, actor)
            elif actor != task.owner_user_id:
                raise TaskPrivilegeError("actor is not task owner")
        return task

    async def _event(self, payload: Mapping[str, Any]) -> SlackResponse:
        event = payload.get("event")
        if isinstance(event, Mapping):
            event = dict(event)
            event.setdefault("team", _team_id(payload, self.bot))
        else:
            event = dict(payload)
        if str(event.get("type") or "") == "message" and not event.get("root_ts"):
            # TaskRegistry.normalize_message accepts raw Slack event fields.
            event["team"] = event.get("team") or _team_id(payload, self.bot)
        routed = await self.registry.maybe_route_message(normalize_message(event))
        return SlackResponse("" if routed else "Message ignored.", ephemeral=True)

    async def _reply(self, payload: Mapping[str, Any], text: str, *, blocks: list[dict[str, Any]] | None = None, ephemeral: bool | None = None) -> SlackResponse:
        """Reply to a Slack interaction.

        ``ephemeral=None`` (the default) auto-selects visibility: Slack's
        agent-view Messages tab (DMs) does not render ``chat.postEphemeral``
        messages at all — they are silently swallowed, which previously made
        command confirmations and errors (model/facet switches, Configure
        submissions, etc.) look like nothing happened. When a task/thread is
        known (``payload["root_ts"]`` is set), post visibly into that thread
        instead. Only fall back to a true ephemeral message when no thread is
        known to post into (e.g. a generic "unknown command" reply).
        """
        if ephemeral is None:
            ephemeral = not bool(payload.get("root_ts"))
        response = SlackResponse(text=text, blocks=blocks, ephemeral=ephemeral)
        responder = payload.get("respond") or payload.get("response")
        if callable(responder):
            result = responder({"text": text, "blocks": blocks, "response_type": "ephemeral" if ephemeral else "in_channel"})
            if inspect.isawaitable(result):
                await result
        elif callable(getattr(self.bot, "respond", None)):
            result = self.bot.respond(payload, response)
            if inspect.isawaitable(result):
                await result
        return response

    async def _error(self, payload: Mapping[str, Any], text: str) -> SlackResponse:
        return await self._reply(payload, f"❌ {text}")

    async def _open_modal(self, payload: Mapping[str, Any], view: dict[str, Any]) -> SlackResponse:
        opener = getattr(self.bot, "open_modal", None) or getattr(self.bot, "views_open", None)
        trigger = payload.get("trigger_id")
        if callable(opener) and trigger:
            result = opener(trigger, view) if getattr(opener, "__name__", "") == "open_modal" else opener(trigger_id=trigger, view=view)
            if inspect.isawaitable(result):
                await result
        responder = payload.get("respond")
        if callable(responder):
            result = responder({"response_action": "push", "view": view})
            if inspect.isawaitable(result):
                await result
        return SlackResponse("", modal=view, ephemeral=True)

    async def _open_participants_modal(self, payload: Mapping[str, Any], task: Task) -> SlackResponse:
        return await self._open_modal(payload, _participants_modal(task))

    async def _decorate_root(self, task: Task) -> None:
        refresher = getattr(self.registry, "refresh_task_header", None)
        if callable(refresher):
            result = refresher(task)
            if inspect.isawaitable(result):
                await result
            return
        editor = getattr(self.bot, "edit_message", None)
        if not callable(editor):
            return
        blocks = build_task_root_blocks(task.task_id, mode=task.mode)
        result = editor(
            task.channel_id,
            task.root_ts,
            text=(f"🤖 *Agent task* `{task.task_id[:8]}` · `{Path(task.cwd).name}`\n"
                  "Reply in this message's thread to work with the agent. Agent output will stay in the thread."),
            blocks=blocks,
        )
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _help_text() -> str:
        return ("*Polytoken commands:* `/agent start cwd=<path> [prompt=<text>]`, `/agent spawn project=<name>`, "
                "`list`, `stop`, `kill`, `restart`, `reload`, `compact`, `skill`, `model`, `facet`, `effort`, `title`, "
                "`stats`, `todos`, `pin`, `unpin`. Task controls are also available on each root message.")


def _invite_bot_modal() -> dict[str, Any]:
    return {
        "type": "modal", "callback_id": "bridge.invite_required",
        "title": {"type": "plain_text", "text": "Add agent to channel"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [{
            "type": "section",
            "text": {"type": "mrkdwn", "text": (
                "*Hailey's Robot is not a member of this channel.*\n"
                "Invite the app to the channel, then run *Start agent here* again. "
                "The bot must be a member to read thread history, download files, and reply."
            )},
        }],
    }


def _start_here_modal(*, team: str, channel: str, root: str) -> dict[str, Any]:
    return {
        "type": "modal", "callback_id": "bridge.start_agent_here",
        "title": {"type": "plain_text", "text": "Start agent here"},
        "submit": {"type": "plain_text", "text": "Start"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": json.dumps({"team_id": team, "channel_id": channel, "root_ts": root}, separators=(",", ":")),
        "blocks": [
            {"type": "input", "block_id": "start_here_cwd",
             "label": {"type": "plain_text", "text": "Working directory or project"},
             "element": {"type": "plain_text_input", "action_id": "cwd",
                         "placeholder": {"type": "plain_text", "text": "/path/to/project or configured name"}}},
            {"type": "input", "block_id": "start_here_prompt", "optional": True,
             "label": {"type": "plain_text", "text": "Initial prompt"},
             "element": {"type": "plain_text_input", "action_id": "initial_prompt",
                         "multiline": True}},
        ],
    }


def _agent_modal(*, task_id: str = "", channel_id: str = "", root_ts: str = "") -> dict[str, Any]:
    return {
        "type": "modal", "callback_id": "bridge.agent", "title": {"type": "plain_text", "text": "Polytoken"},
        "submit": {"type": "plain_text", "text": "Run"}, "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": json.dumps({"task_id": task_id, "channel_id": channel_id, "root_ts": root_ts}),
        "blocks": [
            {"type": "input", "block_id": "command", "label": {"type": "plain_text", "text": "Command"}, "element": {"type": "plain_text_input", "action_id": "command", "initial_value": "help"}},
            {"type": "input", "block_id": "text", "optional": True, "label": {"type": "plain_text", "text": "Arguments"}, "element": {"type": "plain_text_input", "action_id": "text"}},
        ],
    }


def _configure_modal(task: Task, state: Mapping[str, Any], models: Any) -> dict[str, Any]:
    """Build a task-bound configuration view from the daemon's current state."""
    current_model = str(state.get("active_model") or "").strip()
    current_effort = str(state.get("active_reasoning_effort") or state.get("reasoning_effort") or "").strip()
    current_facet = str(state.get("active_facet") or "").strip()
    current_skill = str(state.get("active_skill") or "").strip()
    model_options = _static_options(models, current_model)
    effort_options = _static_options(_EFFORT_LEVELS, current_effort)
    skill_options = _static_options(state.get("available_skills") or [], current_skill)

    model_element: dict[str, Any]
    if model_options:
        model_element = _select_element("model", model_options, current_model, placeholder="Select a model")
    else:
        model_element = {
            "type": "plain_text_input", "action_id": "model",
            "placeholder": {"type": "plain_text", "text": "model/name"},
        }
        if current_model:
            model_element["initial_value"] = current_model[:3000]
    skill_element: dict[str, Any]
    if skill_options:
        skill_element = _select_element("skill", skill_options, current_skill, placeholder="Select a skill")
    else:
        skill_element = {
            "type": "plain_text_input", "action_id": "skill",
            "placeholder": {"type": "plain_text", "text": "skill name"},
        }
        if current_skill:
            skill_element["initial_value"] = current_skill[:3000]
    facet_element: dict[str, Any] = {
        "type": "plain_text_input", "action_id": "facet",
        "placeholder": {"type": "plain_text", "text": "Facet name"},
    }
    if current_facet:
        facet_element["initial_value"] = current_facet[:3000]

    metadata = {
        "task_id": task.task_id,
        "channel_id": task.channel_id,
        "root_ts": task.control_message_ts or task.root_ts,
        "current_model": current_model,
        "current_effort": current_effort,
        "current_facet": current_facet,
        "current_skill": current_skill,
    }
    return {
        "type": "modal", "callback_id": "bridge.configure",
        "title": {"type": "plain_text", "text": "Configure task"},
        "submit": {"type": "plain_text", "text": "Apply"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": json.dumps(metadata, separators=(",", ":")),
        "blocks": [
            {"type": "input", "block_id": "configure_model", "optional": True,
             "label": {"type": "plain_text", "text": "Model"}, "element": model_element},
            {"type": "input", "block_id": "configure_effort", "optional": True,
             "label": {"type": "plain_text", "text": "Reasoning effort"},
             "element": _select_element("effort", effort_options, current_effort, placeholder="Select effort")},
            {"type": "input", "block_id": "configure_facet", "optional": True,
             "label": {"type": "plain_text", "text": "Facet"}, "element": facet_element},
            {"type": "input", "block_id": "configure_skill", "optional": True,
             "label": {"type": "plain_text", "text": "Skill"}, "element": skill_element},
        ],
    }


def _format_subagent_activity(task: Any) -> str:
    """Render bounded, ephemeral activity retained on the live Task runtime."""
    blocks = list(getattr(task, "subagent_blocks", {}).values())
    if not blocks:
        return f"No subagent activity for `{str(task.task_id)[:8]}`."
    lines = [f"*Subagent activity for `{str(task.task_id)[:8]}`:* "]
    now = time.time()
    for block in blocks:
        finished_at = getattr(block, "finished_at", None)
        started_at = float(getattr(block, "started_at", now) or now)
        elapsed = max(0.0, (float(finished_at) if finished_at else now) - started_at)
        duration = f"{elapsed:.0f}s" if elapsed < 60 else f"{elapsed / 60:.1f}m"
        status = "completed" if finished_at else "running"
        actions = list(getattr(block, "actions", []) or [])
        attribution = str(getattr(block, "attribution", None) or getattr(block, "handle", "subagent"))
        lines.append(f"• *{_plain_text(attribution, 100)}* · {status} · {len(actions)} actions · {duration}")
        recent = actions[-2:]
        if recent:
            for action in recent:
                lines.append(f"  ↳ {_plain_text(action, 240)}")
        result = str(getattr(block, "result_summary", None) or "").strip()
        if result:
            lines.append(f"  ↳ result: {_plain_text(result, 240)}")
    return "\n".join(lines)[:3900]


def _participants_modal(task: Task) -> dict[str, Any]:
    return {
        "type": "modal", "callback_id": "bridge.participants",
        "title": {"type": "plain_text", "text": "Participants"},
        "submit": {"type": "plain_text", "text": "Save"}, "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": json.dumps({
            "task_id": task.task_id,
            "channel_id": task.channel_id,
            "root_ts": task.control_message_ts or task.root_ts,
        }),
        "blocks": [{"type": "input", "block_id": "participants", "optional": True,
                     "label": {"type": "plain_text", "text": "Slack user IDs"},
                     "element": {"type": "plain_text_input", "action_id": "participant_ids", "placeholder": {"type": "plain_text", "text": "U0123, U0456"}}}],
    }


def _format_todos(todos: list[Any]) -> str:
    lines = ["*Session todos:*"]
    for item in todos:
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("status") or "")
        text = str(item.get("content") or item.get("activeForm") or "").strip()
        if not text:
            continue
        mark = {"completed": "✅", "in_progress": "▶️"}.get(status, "⬜")
        lines.append(f"{mark} {text}")
    return "\n".join(lines)[:3900]


def build_dispatcher(bot: Any, registry: TaskRegistry, projects: list[Any] | None = None, **kwargs: Any) -> CommandDispatcher:
    return CommandDispatcher(bot, registry, projects, **kwargs)


__all__ = [
    "CommandDispatcher", "SlackResponse", "build_dispatcher", "build_task_root_blocks",
    "normalize_socket_payload", "task_root_blocks",
]

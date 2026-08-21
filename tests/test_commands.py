"""Slack command and interactivity contract tests (AC.3)."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bridge.commands import (
    CommandDispatcher,
    SlackResponse,
    build_task_root_blocks,
    normalize_socket_payload,
    _resolve_model_name,
    _resolve_working_directory,
)
from bridge.domain import ConversationKey
from bridge.tasks import Task, TaskPrivilegeError


@dataclass
class LocalBot:
    team_id: str = "T1"
    owner_user_id: str = "UOWNER"
    home_channel_id: str = "GHOME"
    bot_user_id: str = "UBOT"
    is_ready: bool = True
    edits: list[dict[str, Any]] = field(default_factory=list)
    roots: list[dict[str, Any]] = field(default_factory=list)
    interactions: list[Any] = field(default_factory=list)
    modal_calls: list[dict[str, Any]] = field(default_factory=list)

    async def edit_message(self, channel_id: str, message_ts: str, **kwargs: Any) -> None:
        self.edits.append({"channel_id": channel_id, "message_ts": message_ts, **kwargs})

    async def views_open(self, **kwargs: Any) -> None:
        self.modal_calls.append(dict(kwargs))


@dataclass
class LocalTask:
    task_id: str = "task-12345678"
    team_id: str = "T1"
    channel_id: str = "GHOME"
    root_ts: str = "100.1"
    owner_user_id: str = "UOWNER"
    mode: str = "personal"
    cwd: str = "/tmp/project"
    status: str = "running"
    last_activity: int = 1
    subagent_blocks: dict[str, Any] = field(default_factory=dict)
    control_message_ts: str | None = None

    @property
    def key(self) -> ConversationKey:
        return ConversationKey(self.team_id, self.channel_id, self.root_ts)


class LocalRegistry:
    def __init__(self, task: LocalTask | None = None) -> None:
        self.task = task or LocalTask()
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.participants: list[str] = []
        self.state = {
            "active_model": "model-a", "active_reasoning_effort": "medium",
            "active_facet": "execute", "available_skills": ["brainstorming", "code-review"],
            "todos": [],
        }
        self.models = ["model-a", "model-b"]
        self.pending = None

    async def list_models(self, owner_user_id: str | None = None) -> list[str]:
        self.calls.append(("list_models", (owner_user_id,), {}))
        return list(self.models)

    def get_by_task_id(self, task_id: str) -> LocalTask | None:
        return self.task if task_id == self.task.task_id else None

    def get_by_conversation(self, team: str, channel: str, root: str) -> LocalTask | None:
        return self.task if (team, channel, root) == (self.task.team_id, self.task.channel_id, self.task.root_ts) else None

    async def list_tasks(self, owner_user_id: str | None = None) -> list[LocalTask]:
        self.calls.append(("list_tasks", (owner_user_id,), {}))
        return [self.task] if owner_user_id in (None, self.task.owner_user_id) else []

    def _require_owner(self, task: LocalTask, actor: str) -> None:
        if actor != task.owner_user_id:
            raise TaskPrivilegeError("actor is not task owner")

    async def spawn_task(self, cwd: str, **kwargs: Any) -> LocalTask:
        self.calls.append(("spawn_task", (cwd,), kwargs))
        if not Path(cwd).is_dir():
            raise ValueError("cwd does not exist")
        return self.task

    async def stop_task(self, task_id: str, actor: str) -> bool:
        self.calls.append(("stop_task", (task_id, actor), {}))
        return True

    async def kill_task(self, task_id: str, actor: str) -> bool:
        self.calls.append(("kill_task", (task_id, actor), {}))
        return True

    async def get_state(self, task_id: str, actor: str) -> dict[str, Any]:
        self.calls.append(("get_state", (task_id, actor), {}))
        return self.state

    async def set_effort(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("set_effort", args, kwargs))

    async def clear_context(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("clear_context", args, kwargs))

    async def request_compaction(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append(("request_compaction", args, kwargs))
        return "queued"

    async def reload_daemon(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("reload_daemon", args, kwargs))
        return {"reloaded": ["models"], "failed": []}

    async def set_title(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("set_title", args, kwargs))

    async def set_model(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("set_model", args, kwargs))

    async def set_facet(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("set_facet", args, kwargs))

    async def invoke_skill(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("invoke_skill", args, kwargs))

    async def generate_root_title(self, *args: Any, **kwargs: Any) -> str:
        return "generated title"

    async def pin_channel(self, key: ConversationKey, text: str, actor: str) -> Any:
        self.calls.append(("pin_channel", (key, text, actor), {}))
        return type("Pin", (), {"pin_id": "P1"})()

    async def unpin_channel(self, *args: Any) -> bool:
        self.calls.append(("unpin_channel", args, {}))
        return True

    async def add_participant(self, *args: Any, **kwargs: Any) -> None:
        self.participants.append(str(args[-1]))
        self.calls.append(("add_participant", args, kwargs))

    async def promote_task(self, task_id: str, actor: str) -> LocalTask:
        self.calls.append(("promote_task", (task_id, actor), {}))
        self.task.mode = "collaborative"
        return self.task

    async def _pending_for(self, task: LocalTask, actor: str) -> Any:
        return self.pending

    async def _answer_interrogative(self, task: LocalTask, pending: Any, text: str) -> None:
        self.calls.append(("answer", (task.task_id, pending.interrogative_id, text), {}))


@pytest.mark.asyncio
async def test_socket_normalization_and_ack_before_slow_work(tmp_path: Path) -> None:
    registry = LocalRegistry()
    bot = LocalBot()
    order: list[str] = []

    async def ack(envelope_id: str) -> None:
        order.append(f"ack:{envelope_id}")

    original = registry.list_tasks
    async def list_tasks(owner_user_id: str | None = None) -> list[LocalTask]:
        order.append("work")
        return await original(owner_user_id)
    registry.list_tasks = list_tasks  # type: ignore[method-assign]

    dispatcher = CommandDispatcher(bot, registry, ack=ack)
    response = await dispatcher.handle_socket_envelope({
        "envelope_id": "E1", "payload": {"type": "slash_commands", "team_id": "T1", "command": "/agent", "text": "list", "user_id": "UOWNER"}
    })
    assert response.text.startswith("*Active tasks:*")
    assert order == ["ack:E1", "work"]
    assert normalize_socket_payload({"payload": '{"type":"block_actions","actions":[]}'} )["type"] == "block_actions"


@pytest.mark.asyncio
async def test_start_is_thread_first_and_does_not_duplicate_control_blocks(tmp_path: Path) -> None:
    registry = LocalRegistry()
    bot = LocalBot()
    dispatcher = CommandDispatcher(bot, registry)
    response = await dispatcher.dispatch({
        "type": "slash_commands", "command": "/agent",
        "team_id": "T1", "channel_id": "GHOME", "user_id": "UOWNER",
        "text": f"start cwd={tmp_path}",
    })
    assert response.blocks is None
    assert "Reply in thread" in response.text
    assert len(bot.edits) == 1
    assert "Reply in this message's thread" in bot.edits[0]["text"]
    assert bot.edits[0]["blocks"]


@pytest.mark.asyncio
async def test_owner_only_privileged_control_has_no_side_effect(tmp_path: Path) -> None:
    registry = LocalRegistry()
    dispatcher = CommandDispatcher(LocalBot(), registry)
    response = await dispatcher.dispatch({"type": "slash_commands", "team_id": "T1", "command": "/agent", "text": "stop", "channel_id": "GHOME", "thread_ts": "100.1", "user_id": "UOTHER"})
    assert response.ephemeral
    assert "not task owner" in response.text.lower() or "not allowed" in response.text.lower()
    assert not any(name == "stop_task" for name, _, _ in registry.calls)


@pytest.mark.asyncio
async def test_agent_commands_cover_model_facet_effort_title_stats_todos_and_pins() -> None:
    registry = LocalRegistry()
    bot = LocalBot()
    dispatcher = CommandDispatcher(bot, registry)
    common = {"channel_id": "GHOME", "thread_ts": "100.1", "user_id": "UOWNER", "team_id": "T1"}
    for text in ("reload", "model name=model-b", "facet name=plan", "effort level=high", "title name=New title", "stats", "todos", "pin text=remember", "unpin id=P1"):
        response = await dispatcher.dispatch({"type": "slash_commands", "command": "/agent", "text": text, **common})
        assert not response.text.startswith("❌"), (text, response.text)
    names = [name for name, _, _ in registry.calls]
    assert "reload_daemon" in names
    assert "set_model" in names and "set_facet" in names and "set_effort" in names and "set_title" in names
    assert any(edit["channel_id"] == "GHOME" and edit["message_ts"] == "100.1" and edit["text"] == "New title" for edit in bot.edits)


def test_resolve_model_name_matches_shorthand_and_flags_ambiguity() -> None:
    available = ["router/gpt-5.6-sol:api", "router/gpt-5.6-luna:api", "router-anthropic/claude-sonnet-5:api"]
    assert _resolve_model_name("sol", available) == ("router/gpt-5.6-sol:api", [])
    assert _resolve_model_name("router/gpt-5.6-sol:api", available) == ("router/gpt-5.6-sol:api", [])
    assert _resolve_model_name("ROUTER/GPT-5.6-SOL:API", available) == ("router/gpt-5.6-sol:api", [])
    resolved, candidates = _resolve_model_name("gpt-5.6", available)
    assert resolved is None and set(candidates) == {"router/gpt-5.6-sol:api", "router/gpt-5.6-luna:api"}
    resolved, candidates = _resolve_model_name("does-not-exist", available)
    assert resolved is None and candidates == []
    # When the model registry could not be fetched, trust the caller rather
    # than blocking every model switch on a transient listing failure.
    assert _resolve_model_name("sol", []) == ("sol", [])


@pytest.mark.asyncio
async def test_agent_model_command_resolves_shorthand_name() -> None:
    registry = LocalRegistry()
    registry.models = ["router/gpt-5.6-sol:api", "router/gpt-5.6-luna:api"]
    dispatcher = CommandDispatcher(LocalBot(), registry)
    response = await dispatcher.dispatch({
        "type": "slash_commands", "command": "/agent", "text": "model name=sol",
        "channel_id": "GHOME", "thread_ts": "100.1", "user_id": "UOWNER", "team_id": "T1",
    })
    assert not response.text.startswith("❌"), response.text
    assert ("set_model", ("task-12345678", "router/gpt-5.6-sol:api"), {"owner_user_id": "UOWNER", "reasoning_effort": None}) in registry.calls
    # A resolved slash-command reply is known to belong to the task's thread,
    # so it must be visible in Slack's agent-view Messages tab rather than an
    # ephemeral message the surface silently drops.
    assert response.ephemeral is False


@pytest.mark.asyncio
async def test_agent_model_command_reports_unknown_name_without_swallowing_it() -> None:
    registry = LocalRegistry()
    registry.models = ["router/gpt-5.6-sol:api", "router/gpt-5.6-luna:api"]
    dispatcher = CommandDispatcher(LocalBot(), registry)
    response = await dispatcher.dispatch({
        "type": "slash_commands", "command": "/agent", "text": "model name=nonexistent",
        "channel_id": "GHOME", "thread_ts": "100.1", "user_id": "UOWNER", "team_id": "T1",
    })
    assert "Unknown model" in response.text and "nonexistent" in response.text
    assert not any(name == "set_model" for name, _, _ in registry.calls)


@pytest.mark.asyncio
async def test_session_command_error_preserves_specific_reason_instead_of_generic_text() -> None:
    """Regression: _dispatch's outer catch used to discard the specific,
    already-safe reason TaskSpawnError carried and always show the same
    generic "Could not complete that request" text, making every failure
    (wrong model name, daemon rejection, etc.) look identical and
    undiagnosable from Slack."""
    registry = LocalRegistry()

    async def _raise_set_model(*args: Any, **kwargs: Any) -> None:
        from bridge.tasks import TaskSpawnError
        raise TaskSpawnError("daemon rejected model change (HTTP 400)")

    registry.set_model = _raise_set_model  # type: ignore[method-assign]
    dispatcher = CommandDispatcher(LocalBot(), registry)
    response = await dispatcher.dispatch({
        "type": "slash_commands", "command": "/agent", "text": "model name=model-b",
        "channel_id": "GHOME", "thread_ts": "100.1", "user_id": "UOWNER", "team_id": "T1",
    })
    assert "daemon rejected model change" in response.text
    assert "Could not complete that request" not in response.text


def test_view_submission_normalization_restores_private_channel_context() -> None:
    normalized = normalize_socket_payload({
        "type": "view_submission", "user": {"id": "UOWNER"},
        "view": {"private_metadata": json.dumps({"channel_id": "COTHER"})},
    })
    assert normalized["channel_id"] == "COTHER"
    assert normalized["actor_id"] == "UOWNER"


def test_working_directory_resolution_accepts_quotes_case_and_aliases(tmp_path: Path) -> None:
    project = SimpleNamespace(name="attie", root_label="bluesky", path=tmp_path)
    assert _resolve_working_directory("Attie", [project])[0] == str(tmp_path.resolve())
    assert _resolve_working_directory("BLUESKY/ATTIE", [project])[0] == str(tmp_path.resolve())
    assert _resolve_working_directory(f"`{tmp_path}`", [])[0] == str(tmp_path.resolve())
    assert _resolve_working_directory("does-not-exist", [project])[0] is None


def test_task_root_blocks_have_controls_and_task_value() -> None:
    blocks = build_task_root_blocks("T123", mode="personal")
    actions = [element for block in blocks for element in block["elements"]]
    action_ids = {item["action_id"] for item in actions}
    assert action_ids == {"task.compact", "task.clear", "task.stop", "task.kill", "task.participants", "task.configure"}
    assert action_ids.isdisjoint({"task.todos", "task.stats", "task.activity", "task.title", "task.promote"})
    assert all(item["value"] == "T123" for item in actions)
    clear = next(item for item in actions if item["action_id"] == "task.clear")
    assert clear["style"] == "danger" and clear["confirm"]["style"] == "danger"


@pytest.mark.asyncio
async def test_shortcut_modal_submission_and_block_action_routing() -> None:
    registry = LocalRegistry()
    bot = LocalBot()
    dispatcher = CommandDispatcher(bot, registry)
    shortcut = await dispatcher.dispatch({"type": "shortcut", "callback_id": "agent", "trigger_id": "TR", "team_id": "T1", "user_id": "UOWNER"})
    assert shortcut.modal and shortcut.modal["callback_id"] == "bridge.agent"
    submitted = await dispatcher.dispatch({
        "type": "view_submission", "team_id": "T1", "user_id": "UOWNER", "view": {
            "callback_id": "bridge.agent", "private_metadata": '{"channel_id":"GHOME","root_ts":"100.1"}',
            "state": {"values": {"command": {"command": {"action_id": "command", "value": "stats"}}}},
        },
    })
    assert "model-a" in submitted.text
    control = await dispatcher.dispatch({"type": "block_actions", "team_id": "T1", "user_id": "UOWNER", "channel_id": "GHOME", "thread_ts": "100.1", "actions": [{"action_id": "task.stop", "value": "task-12345678"}]})
    assert "Stopped" in control.text
    compact = await dispatcher.dispatch({"type": "block_actions", "team_id": "T1", "user_id": "UOWNER", "channel_id": "GHOME", "thread_ts": "100.1", "actions": [{"action_id": "task.compact", "value": "task-12345678"}]})
    assert "queued" in compact.text.lower()
    assert any(name == "request_compaction" for name, _, _ in registry.calls)
    feedback = await dispatcher.dispatch({"type": "block_actions", "team_id": "T1", "user_id": "UOWNER", "channel_id": "GHOME", "thread_ts": "100.1", "actions": [{"action_id": "task.feedback", "value": '{"task_id":"task-12345678","rating":"positive"}'}]})
    assert "thanks for the feedback" in feedback.text.lower()
    cleared = await dispatcher.dispatch({"type": "block_actions", "team_id": "T1", "user_id": "UOWNER", "channel_id": "GHOME", "thread_ts": "100.1", "actions": [{"action_id": "task.clear", "value": "task-12345678"}]})
    assert "Context cleared" in cleared.text
    assert any(name == "clear_context" for name, _, _ in registry.calls)


@pytest.mark.asyncio
async def test_interrogative_block_action_answers_targeted_question() -> None:
    registry = LocalRegistry()
    registry.pending = type("Pending", (), {"interrogative_id": "I1"})()
    dispatcher = CommandDispatcher(LocalBot(), registry)
    response = await dispatcher.dispatch({"type": "block_actions", "team_id": "T1", "user_id": "UOWNER", "channel_id": "GHOME", "thread_ts": "100.1", "interrogative_id": "I1", "actions": [{"action_id": "interrogative.answer", "value": "yes"}]})
    assert "Answer sent" in response.text
    assert any(name == "answer" for name, _, _ in registry.calls)


@pytest.mark.asyncio
async def test_configure_button_resolves_exact_task_and_opens_views_modal() -> None:
    registry = LocalRegistry()
    bot = LocalBot()
    dispatcher = CommandDispatcher(bot, registry)
    response = await dispatcher.dispatch({
        "type": "block_actions", "team_id": "T1", "user_id": "UOWNER",
        "channel_id": "GHOME", "message": {"ts": "100.1"}, "trigger_id": "TR-CONFIG",
        "actions": [{"action_id": "task.configure", "value": "task-12345678"}],
    })
    assert response.modal and response.modal["callback_id"] == "bridge.configure"
    assert bot.modal_calls == [{"trigger_id": "TR-CONFIG", "view": response.modal}]
    assert ("get_state", ("task-12345678", "UOWNER"), {}) in registry.calls
    assert ("list_models", ("UOWNER",), {}) in registry.calls
    metadata = json.loads(response.modal["private_metadata"])
    assert metadata["task_id"] == "task-12345678"
    model = next(block["element"] for block in response.modal["blocks"] if block["block_id"] == "configure_model")
    assert model["type"] == "static_select"
    assert model["initial_option"] in model["options"]
    assert len(model["options"]) <= 100


@pytest.mark.asyncio
async def test_configure_submission_applies_only_changed_settings_and_auth() -> None:
    registry = LocalRegistry()
    dispatcher = CommandDispatcher(LocalBot(), registry)
    metadata = json.dumps({
        "task_id": "task-12345678", "current_model": "model-a", "current_effort": "medium",
        "current_facet": "execute", "current_skill": "",
    })
    values = {
        "configure_model": {"model": {"action_id": "model", "selected_option": {"value": "model-b"}}},
        "configure_effort": {"effort": {"action_id": "effort", "selected_option": {"value": "high"}}},
        "configure_facet": {"facet": {"action_id": "facet", "value": "plan"}},
        "configure_skill": {"skill": {"action_id": "skill", "selected_option": {"value": "brainstorming"}}},
    }
    response = await dispatcher.dispatch({
        "type": "view_submission", "team_id": "T1", "user_id": "UOWNER",
        "view": {"callback_id": "bridge.configure", "private_metadata": metadata, "state": {"values": values}},
    })
    assert "model, facet, skill" in response.text
    assert ("set_model", ("task-12345678", "model-b"), {"owner_user_id": "UOWNER", "reasoning_effort": "high"}) in registry.calls
    assert not any(name == "set_effort" for name, _, _ in registry.calls)
    assert ("set_facet", ("task-12345678", "plan"), {"owner_user_id": "UOWNER"}) in registry.calls
    assert ("invoke_skill", ("task-12345678", "brainstorming"), {"owner_user_id": "UOWNER"}) in registry.calls

    denied = await dispatcher.dispatch({
        "type": "view_submission", "team_id": "T1", "user_id": "UOTHER",
        "view": {"callback_id": "bridge.configure", "private_metadata": metadata, "state": {"values": values}},
    })
    assert denied.ephemeral and "not allowed" in denied.text.lower()
    assert len([name for name, _, _ in registry.calls if name in {"set_model", "set_facet", "invoke_skill"}]) == 3


@pytest.mark.asyncio
async def test_configure_submission_with_root_ts_metadata_posts_visibly_into_thread() -> None:
    """Regression: the Configure modal's metadata never carried root_ts, so
    even a successful model/facet switch confirmed via an ephemeral message
    that Slack's agent-view Messages tab silently drops -- looking exactly
    like nothing happened. The modal now round-trips root_ts through
    private_metadata and the confirmation must be posted into the thread."""
    registry = LocalRegistry()
    dispatcher = CommandDispatcher(LocalBot(), registry)
    opened = await dispatcher.dispatch({
        "type": "block_actions", "team_id": "T1", "user_id": "UOWNER",
        "channel_id": "GHOME", "message": {"ts": "100.1"}, "trigger_id": "TR-CONFIG",
        "actions": [{"action_id": "task.configure", "value": "task-12345678"}],
    })
    metadata = json.loads(opened.modal["private_metadata"])
    assert metadata["root_ts"] == "100.1"
    submitted = await dispatcher.dispatch({
        "type": "view_submission", "team_id": "T1", "user_id": "UOWNER",
        "view": {
            "callback_id": "bridge.configure",
            "private_metadata": opened.modal["private_metadata"],
            "state": {"values": {
                "configure_facet": {"facet": {"action_id": "facet", "value": "plan"}},
            }},
        },
    })
    assert "facet" in submitted.text
    assert submitted.ephemeral is False


@pytest.mark.asyncio
async def test_configure_submission_succeeds_when_control_message_differs_from_root() -> None:
    """Regression: an existing-thread-bound task posts its control panel as a
    separate reply, so control_message_ts != root_ts. The Configure modal's
    metadata must carry root_ts (what _task_from_payload actually validates
    against), not control_message_ts -- using the latter made every such
    Configure submission fail with "task id does not match the supplied
    Slack conversation" even though the task_id was correct."""
    task = LocalTask(root_ts="100.1", control_message_ts="200.2")
    registry = LocalRegistry(task)
    dispatcher = CommandDispatcher(LocalBot(), registry)
    opened = await dispatcher.dispatch({
        "type": "block_actions", "team_id": "T1", "user_id": "UOWNER",
        "channel_id": "GHOME", "message": {"ts": "200.2"}, "trigger_id": "TR-CONFIG",
        "actions": [{"action_id": "task.configure", "value": task.task_id}],
    })
    metadata = json.loads(opened.modal["private_metadata"])
    assert metadata["root_ts"] == "100.1"
    submitted = await dispatcher.dispatch({
        "type": "view_submission", "team_id": "T1", "user_id": "UOWNER",
        "view": {
            "callback_id": "bridge.configure",
            "private_metadata": opened.modal["private_metadata"],
            "state": {"values": {
                "configure_facet": {"facet": {"action_id": "facet", "value": "plan"}},
            }},
        },
    })
    assert not submitted.text.startswith("❌"), submitted.text
    assert ("set_facet", (task.task_id, "plan"), {"owner_user_id": "UOWNER"}) in registry.calls


@pytest.mark.asyncio
async def test_activity_button_is_owner_only_and_retains_completion_summary() -> None:
    registry = LocalRegistry()
    registry.task.subagent_blocks["h1"] = type("Block", (), {
        "handle": "h1", "attribution": "researcher", "started_at": 1.0,
        "finished_at": 4.0, "actions": ["read file", "ran tests"], "result_summary": "done",
    })()
    dispatcher = CommandDispatcher(LocalBot(), registry)
    response = await dispatcher.dispatch({
        "type": "block_actions", "team_id": "T1", "user_id": "UOWNER",
        "channel_id": "GHOME", "message": {"ts": "100.1"},
        "actions": [{"action_id": "task.activity", "value": "task-12345678"}],
    })
    assert "completed" in response.text and "2 actions" in response.text
    assert "ran tests" in response.text and "result: done" in response.text
    denied = await dispatcher.dispatch({
        "type": "block_actions", "team_id": "T1", "user_id": "UOTHER",
        "channel_id": "GHOME", "message": {"ts": "100.1"},
        "actions": [{"action_id": "task.activity", "value": "task-12345678"}],
    })
    assert denied.ephemeral and "not allowed" in denied.text.lower()


@pytest.mark.asyncio
async def test_start_agent_here_requires_bot_channel_membership() -> None:
    class NonMemberBot(LocalBot):
        async def is_channel_member(self, channel_id: str) -> bool:
            return False

    bot = NonMemberBot()
    dispatcher = CommandDispatcher(bot, LocalRegistry())
    response = await dispatcher.dispatch({
        "type": "message_action", "callback_id": "start_agent_here", "trigger_id": "TR-NO-MEMBER",
        "team": {"id": "T1"}, "user": {"id": "UOWNER"}, "channel": {"id": "COTHER"},
        "message": {"ts": "200.2", "text": "human root"},
    })
    assert response.modal and response.modal["callback_id"] == "bridge.invite_required"
    assert "not a member" in str(response.modal["blocks"])
    assert bot.modal_calls == [{"trigger_id": "TR-NO-MEMBER", "view": response.modal}]


@pytest.mark.asyncio
async def test_start_agent_here_submission_returns_existing_active_task() -> None:
    registry = LocalRegistry()
    dispatcher = CommandDispatcher(LocalBot(), registry)
    response = await dispatcher.dispatch({
        "type": "view_submission", "team_id": "T1", "user": {"id": "UOWNER"},
        "view": {
            "callback_id": "bridge.start_agent_here",
            "private_metadata": json.dumps({"team_id": "T1", "channel_id": "GHOME", "root_ts": "100.1"}),
            "state": {"values": {}},
        },
    })
    assert "already has active task" in response.text
    assert not any(call[0] == "spawn_task" for call in registry.calls)


@pytest.mark.asyncio
async def test_start_agent_here_shortcut_binds_selected_thread_without_editing_root(tmp_path: Path) -> None:
    registry = LocalRegistry(LocalTask(root_ts="different-root"))
    bot = LocalBot()
    dispatcher = CommandDispatcher(bot, registry)
    shortcut = await dispatcher.dispatch({
        "type": "message_action", "callback_id": "start_agent_here", "trigger_id": "TR-HERE",
        "team": {"id": "T1"}, "user": {"id": "UOWNER"}, "channel": {"id": "GHOME"},
        "message": {"ts": "200.2", "thread_ts": "100.1", "text": "human root"},
    })
    assert shortcut.modal and shortcut.modal["callback_id"] == "bridge.start_agent_here"
    assert json.loads(shortcut.modal["private_metadata"]) == {"team_id": "T1", "channel_id": "GHOME", "root_ts": "100.1"}
    submitted = await dispatcher.dispatch({
        "type": "view_submission", "team_id": "T1", "user_id": "UOWNER",
        "view": {"callback_id": "bridge.start_agent_here", "private_metadata": shortcut.modal["private_metadata"],
                 "state": {"values": {"start_here_cwd": {"cwd": {"type": "plain_text_input", "value": str(tmp_path)}}}}},
    })
    assert "Started" in submitted.text
    call = next(call for call in registry.calls if call[0] == "spawn_task")
    assert call[1] == (str(tmp_path),)
    assert call[2]["root_ts"] == "100.1" and call[2]["bind_existing_root"] is True
    assert not bot.edits

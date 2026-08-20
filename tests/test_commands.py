"""Slack command and interactivity contract tests (AC.3)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from bridge.commands import (
    CommandDispatcher,
    SlackResponse,
    build_task_root_blocks,
    normalize_socket_payload,
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

    async def edit_message(self, channel_id: str, message_ts: str, **kwargs: Any) -> None:
        self.edits.append({"channel_id": channel_id, "message_ts": message_ts, **kwargs})


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

    @property
    def key(self) -> ConversationKey:
        return ConversationKey(self.team_id, self.channel_id, self.root_ts)


class LocalRegistry:
    def __init__(self, task: LocalTask | None = None) -> None:
        self.task = task or LocalTask()
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.participants: list[str] = []
        self.state = {"active_model": "model-a", "active_facet": "execute", "todos": []}
        self.pending = None

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
    for text in ("model name=model-b", "facet name=plan", "effort level=high", "title name=New title", "stats", "todos", "pin text=remember", "unpin id=P1"):
        response = await dispatcher.dispatch({"type": "slash_commands", "command": "/agent", "text": text, **common})
        assert not response.text.startswith("❌"), (text, response.text)
    names = [name for name, _, _ in registry.calls]
    assert "set_model" in names and "set_facet" in names and "set_effort" in names
    assert any(edit["channel_id"] == "GHOME" and edit["message_ts"] == "100.1" and edit["text"] == "New title" for edit in bot.edits)


def test_task_root_blocks_have_controls_and_task_value() -> None:
    blocks = build_task_root_blocks("T123", mode="personal")
    actions = [element for block in blocks for element in block["elements"]]
    assert {item["action_id"] for item in actions} >= {"task.compact", "task.todos", "task.stats", "task.stop", "task.kill", "task.participants", "task.promote"}
    assert all(item["value"] == "T123" for item in actions)


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


@pytest.mark.asyncio
async def test_interrogative_block_action_answers_targeted_question() -> None:
    registry = LocalRegistry()
    registry.pending = type("Pending", (), {"interrogative_id": "I1"})()
    dispatcher = CommandDispatcher(LocalBot(), registry)
    response = await dispatcher.dispatch({"type": "block_actions", "team_id": "T1", "user_id": "UOWNER", "channel_id": "GHOME", "thread_ts": "100.1", "interrogative_id": "I1", "actions": [{"action_id": "interrogative.answer", "value": "yes"}]})
    assert "Answer sent" in response.text
    assert any(name == "answer" for name, _, _ in registry.calls)

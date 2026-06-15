"""Tests for the daemon-model slash command tree."""

from dataclasses import dataclass, field

import pytest
from discord import app_commands

from bridge.commands import build_tree
from bridge.tasks import TaskRegistry
from tests.fakes import FakeBot, FakePolytokenClient, FakeSupervisor


@dataclass
class FakeResponse:
    _deferred: bool = False

    async def defer(self, *, ephemeral: bool = False) -> None:
        self._deferred = True


@dataclass
class FakeFollowup:
    _sends: list = field(default_factory=list)

    async def send(self, content=None, *, embed=None, ephemeral=False) -> None:
        self._sends.append({"content": content, "embed": embed})


@dataclass
class FakeInteraction:
    channel_id: int | None = None
    guild_id: int | None = 1
    response: FakeResponse = field(default_factory=FakeResponse)
    followup: FakeFollowup = field(default_factory=FakeFollowup)


@pytest.fixture(autouse=True)
def _no_consumer(monkeypatch):
    monkeypatch.setattr(TaskRegistry, "_start_consumer", lambda self, task: None)


def _make(db):
    bot = FakeBot()
    reg = TaskRegistry(db, bot, FakeSupervisor())
    tree = build_tree(bot, reg, projects=None)
    return bot, reg, tree


async def _spawn_inject(reg, tmp_path):
    task = await reg.spawn_task(cwd=str(tmp_path))
    fake = FakePolytokenClient(port=task.port)
    reg._clients[task.task_id] = fake
    return task, fake


class TestStartList:
    async def test_start_spawns(self, in_memory_db, tmp_path) -> None:
        bot, reg, tree = _make(in_memory_db)
        inter = FakeInteraction(channel_id=1, guild_id=1)
        await tree.get_command("start").callback(inter, cwd=str(tmp_path))
        assert inter.response._deferred
        assert "Started task" in inter.followup._sends[0]["content"]
        assert len(await reg.list_tasks()) == 1

    async def test_start_bad_cwd(self, in_memory_db) -> None:
        bot, reg, tree = _make(in_memory_db)
        inter = FakeInteraction(channel_id=1)
        await tree.get_command("start").callback(inter, cwd="/no/such/dir")
        assert "❌" in inter.followup._sends[0]["content"]

    async def test_list(self, in_memory_db, tmp_path) -> None:
        bot, reg, tree = _make(in_memory_db)
        task, _ = await _spawn_inject(reg, tmp_path)
        sent = {}

        class R:
            async def send_message(self, content, *, ephemeral=False):
                sent["content"] = content

        inter = FakeInteraction(channel_id=1)
        inter.response = R()
        await tree.get_command("list").callback(inter)
        assert task.task_id[:8] in sent["content"]

    async def test_list_empty(self, in_memory_db) -> None:
        bot, reg, tree = _make(in_memory_db)

        sent = {}

        class R:
            async def send_message(self, content, *, ephemeral=False):
                sent["content"] = content

        inter = FakeInteraction(channel_id=1)
        inter.response = R()
        await tree.get_command("list").callback(inter)
        assert "No active tasks" in sent["content"]


class TestLifecycleCommands:
    async def test_stop(self, in_memory_db, tmp_path) -> None:
        bot, reg, tree = _make(in_memory_db)
        task, fake = await _spawn_inject(reg, tmp_path)
        inter = FakeInteraction(channel_id=task.thread_id)
        await tree.get_command("stop").callback(inter, thread=None)
        assert fake.terminated == 1
        assert "Stopped" in inter.followup._sends[0]["content"]

    async def test_kill(self, in_memory_db, tmp_path) -> None:
        bot, reg, tree = _make(in_memory_db)
        task, fake = await _spawn_inject(reg, tmp_path)
        inter = FakeInteraction(channel_id=task.thread_id)
        await tree.get_command("kill").callback(inter, thread=None)
        assert fake.terminated == 1
        assert "Killed" in inter.followup._sends[0]["content"]

    async def test_kill_reports_failure_when_terminate_rejected(self, in_memory_db, tmp_path) -> None:
        bot, reg, tree = _make(in_memory_db)
        task, fake = await _spawn_inject(reg, tmp_path)
        fake.terminate_error_status = 500
        inter = FakeInteraction(channel_id=task.thread_id)
        await tree.get_command("kill").callback(inter, thread=None)
        assert "Couldn't terminate" in inter.followup._sends[0]["content"]

    async def test_restart_unsupported(self, in_memory_db, tmp_path) -> None:
        bot, reg, tree = _make(in_memory_db)
        task, _ = await _spawn_inject(reg, tmp_path)
        inter = FakeInteraction(channel_id=task.thread_id)
        await tree.get_command("restart").callback(inter, thread=None)
        assert "❌" in inter.followup._sends[0]["content"]
        assert "supported" in inter.followup._sends[0]["content"].lower()

    async def test_outside_thread_errors(self, in_memory_db) -> None:
        bot, reg, tree = _make(in_memory_db)
        inter = FakeInteraction(channel_id=9999)
        await tree.get_command("stop").callback(inter, thread=None)
        assert "task thread" in inter.followup._sends[0]["content"]


class TestSessionCommands:
    async def test_effort(self, in_memory_db, tmp_path) -> None:
        bot, reg, tree = _make(in_memory_db)
        task, fake = await _spawn_inject(reg, tmp_path)
        inter = FakeInteraction(channel_id=task.thread_id)
        choice = app_commands.Choice(name="high", value="high")
        await tree.get_command("effort").callback(inter, level=choice)
        assert fake.model_calls[0]["reasoning_effort"] == "high"
        assert "high" in inter.followup._sends[0]["content"]

    async def test_skill(self, in_memory_db, tmp_path) -> None:
        bot, reg, tree = _make(in_memory_db)
        task, fake = await _spawn_inject(reg, tmp_path)
        inter = FakeInteraction(channel_id=task.thread_id)
        await tree.get_command("skill").callback(inter, name="brainstorming", args="go")
        assert fake.prompts == ["@brainstorming go"]

    async def test_rename_explicit(self, in_memory_db, tmp_path) -> None:
        bot, reg, tree = _make(in_memory_db)
        task, _ = await _spawn_inject(reg, tmp_path)
        inter = FakeInteraction(channel_id=task.thread_id)
        await tree.get_command("rename").callback(inter, name="my feature")
        assert bot._rename_calls[0]["name"] == "my feature"

    async def test_stats_shows_model_and_facet(self, in_memory_db, tmp_path) -> None:
        bot, reg, tree = _make(in_memory_db)
        task, _ = await _spawn_inject(reg, tmp_path)
        inter = FakeInteraction(channel_id=task.thread_id)
        await tree.get_command("stats").callback(inter, thread=None)
        content = inter.followup._sends[0]["content"]
        assert "claude-opus-4-8" in content
        assert "execute" in content  # active_facet now shown

    async def test_model_switch(self, in_memory_db, tmp_path) -> None:
        bot, reg, tree = _make(in_memory_db)
        task, fake = await _spawn_inject(reg, tmp_path)
        inter = FakeInteraction(channel_id=task.thread_id)
        await tree.get_command("model").callback(inter, name="openai/gpt-5.5", effort=None)
        assert fake.model_calls[-1] == {"model": "openai/gpt-5.5", "reasoning_effort": None}
        assert "gpt-5.5" in inter.followup._sends[0]["content"]

    async def test_model_switch_with_effort(self, in_memory_db, tmp_path) -> None:
        bot, reg, tree = _make(in_memory_db)
        task, fake = await _spawn_inject(reg, tmp_path)
        inter = FakeInteraction(channel_id=task.thread_id)
        choice = app_commands.Choice(name="high", value="high")
        await tree.get_command("model").callback(inter, name="openai/gpt-5.5", effort=choice)
        assert fake.model_calls[-1] == {"model": "openai/gpt-5.5", "reasoning_effort": "high"}

    async def test_facet_switch(self, in_memory_db, tmp_path) -> None:
        bot, reg, tree = _make(in_memory_db)
        task, fake = await _spawn_inject(reg, tmp_path)
        inter = FakeInteraction(channel_id=task.thread_id)
        await tree.get_command("facet").callback(inter, facet="plan")
        assert fake.facet_calls == ["plan"]
        assert "plan" in inter.followup._sends[0]["content"]

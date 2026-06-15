"""Tests for the daemon-backed TaskRegistry."""

from dataclasses import dataclass, field

import pytest

from bridge import events, state
from bridge.tasks import Task, TaskNotFound, TaskRegistry, TaskRestartError, TaskSpawnError
from tests.fakes import FakeBot, FakePolytokenClient, FakeSupervisor


@dataclass
class FakeAttachment:
    filename: str
    _data: bytes = b"hello"

    async def read(self) -> bytes:
        return self._data


@dataclass
class FakeChannel:
    id: int


@dataclass
class FakeMsg:
    channel: FakeChannel
    content: str = ""
    attachments: list = field(default_factory=list)
    id: int = 777


@pytest.fixture(autouse=True)
def _no_consumer(monkeypatch):
    # Unit tests don't run the real SSE consumer.
    monkeypatch.setattr(TaskRegistry, "_start_consumer", lambda self, task: None)


def _make_registry(db) -> tuple[TaskRegistry, FakeBot, FakeSupervisor]:
    bot = FakeBot()
    sup = FakeSupervisor()
    reg = TaskRegistry(db, bot, sup)
    return reg, bot, sup


async def _bind_running_task(reg: TaskRegistry, *, thread_id=2000, port=40001) -> tuple[Task, FakePolytokenClient]:
    task = Task(
        task_id="t1", thread_id=thread_id, cwd="/w", status="running",
        polytoken_session_id="sess-1", port=port, created_at=0, last_activity=0,
    )
    await reg._index(task)
    await reg._persist(task)
    fake = FakePolytokenClient(port=port)
    reg._clients[task.task_id] = fake
    return task, fake


class TestSpawn:
    async def test_spawn_creates_thread_and_persists(self, in_memory_db, tmp_path) -> None:
        reg, bot, sup = _make_registry(in_memory_db)
        task = await reg.spawn_task(cwd=str(tmp_path))
        assert task.status == "running"
        assert task.polytoken_session_id == "sess-1"
        assert task.port is not None
        assert len(bot.get_thread_calls()) == 1
        row = await state.get_task(in_memory_db, task.task_id)
        assert row.status == "running" and row.port == task.port

    async def test_spawn_bad_cwd_raises(self, in_memory_db) -> None:
        reg, _, _ = _make_registry(in_memory_db)
        with pytest.raises(TaskSpawnError):
            await reg.spawn_task(cwd="/no/such/dir")

    async def test_spawn_failure_marks_crashed(self, in_memory_db, tmp_path) -> None:
        reg, bot, sup = _make_registry(in_memory_db)
        sup.fail_spawn = True
        with pytest.raises(TaskSpawnError):
            await reg.spawn_task(cwd=str(tmp_path))
        # Thread was created, then a failure notice posted and archived.
        assert any("failed to spawn" in c["content"] for c in bot.get_post_calls())
        assert bot.get_archive_calls()


class TestRouting:
    async def test_route_prompts_daemon(self, in_memory_db) -> None:
        reg, _, _ = _make_registry(in_memory_db)
        task, fake = await _bind_running_task(reg)
        handled = await reg.maybe_route_message(FakeMsg(FakeChannel(task.thread_id), content="hello world"))
        assert handled is True
        assert fake.prompts == ["hello world"]

    async def test_route_attachment_becomes_at_reference(self, in_memory_db) -> None:
        reg, _, _ = _make_registry(in_memory_db)
        task, fake = await _bind_running_task(reg)
        msg = FakeMsg(FakeChannel(task.thread_id), content="look", attachments=[FakeAttachment("notes.txt")])
        await reg.maybe_route_message(msg)
        assert len(fake.prompts) == 1
        assert "@" in fake.prompts[0] and "notes.txt" in fake.prompts[0]

    async def test_route_unbound_thread_returns_false(self, in_memory_db) -> None:
        reg, _, _ = _make_registry(in_memory_db)
        assert await reg.maybe_route_message(FakeMsg(FakeChannel(9999), content="hi")) is False

    async def test_pending_interrogative_consumes_reply(self, in_memory_db) -> None:
        reg, _, _ = _make_registry(in_memory_db)
        task, fake = await _bind_running_task(reg)
        reg._pending_interrogatives[task.task_id] = __import__(
            "bridge.tasks", fromlist=["PendingInterrogative"]
        ).PendingInterrogative(interrogative_id="i1", kind="confirmation")
        await reg.maybe_route_message(FakeMsg(FakeChannel(task.thread_id), content="yes"))
        assert fake.prompts == []  # not a normal prompt
        assert fake.interrogative_responses[0]["response"] == {"kind": "confirmation_answer", "confirmed": True}

    async def test_clarification_numeric_choice(self, in_memory_db) -> None:
        reg, _, _ = _make_registry(in_memory_db)
        task, fake = await _bind_running_task(reg)
        from bridge.tasks import PendingInterrogative

        reg._pending_interrogatives[task.task_id] = PendingInterrogative(
            interrogative_id="i2", kind="clarification",
            options=[{"key": "a", "label": "Apple"}, {"key": "b", "label": "Banana"}],
        )
        await reg.maybe_route_message(FakeMsg(FakeChannel(task.thread_id), content="2"))
        assert fake.interrogative_responses[0]["response"] == {"kind": "clarification_choice", "choice": "b"}


class TestLifecycle:
    async def test_stop_terminates_and_marks_stopped(self, in_memory_db) -> None:
        reg, bot, _ = _make_registry(in_memory_db)
        task, fake = await _bind_running_task(reg)
        await reg.stop_task(task.task_id)
        assert fake.cancelled == 1 and fake.terminated == 1
        assert task.status == "stopped"
        assert reg.get_by_thread_id(task.thread_id) is None
        assert bot.get_archive_calls()

    async def test_kill_terminates_and_marks_crashed(self, in_memory_db) -> None:
        reg, _, _ = _make_registry(in_memory_db)
        task, fake = await _bind_running_task(reg)
        await reg.kill_task(task.task_id)
        assert fake.terminated == 1
        assert task.status == "crashed"

    async def test_restart_unsupported(self, in_memory_db) -> None:
        reg, _, _ = _make_registry(in_memory_db)
        task, _ = await _bind_running_task(reg)
        with pytest.raises(TaskRestartError):
            await reg.restart_task(task.task_id)

    async def test_kill_unknown_raises(self, in_memory_db) -> None:
        reg, _, _ = _make_registry(in_memory_db)
        with pytest.raises(TaskNotFound):
            await reg.kill_task("nope")


class TestEffortAndState:
    async def test_set_effort_reselects_model(self, in_memory_db) -> None:
        reg, _, _ = _make_registry(in_memory_db)
        task, fake = await _bind_running_task(reg)
        await reg.set_effort(task.task_id, "low")
        assert fake.model_calls == [{"model": "anthropic/claude-opus-4-8", "reasoning_effort": "low"}]

    async def test_get_state(self, in_memory_db) -> None:
        reg, _, _ = _make_registry(in_memory_db)
        task, _ = await _bind_running_task(reg)
        st = await reg.get_state(task.task_id)
        assert st["active_model"] == "anthropic/claude-opus-4-8"

    async def test_invoke_skill_prompts_at_reference(self, in_memory_db) -> None:
        reg, _, _ = _make_registry(in_memory_db)
        task, fake = await _bind_running_task(reg)
        await reg.invoke_skill(task.task_id, "brainstorming", "go")
        assert fake.prompts == ["@brainstorming go"]


class TestRender:
    async def test_tool_line_appends_aggregator_then_flush_posts(self, in_memory_db) -> None:
        reg, bot, _ = _make_registry(in_memory_db)
        task, _ = await _bind_running_task(reg)
        await reg._render(task, events.ToolLine(line="✓ Bash: ls"))
        await reg._end_turn(task)  # flush_now
        assert any("Bash: ls" in c["content"] for c in bot.get_post_calls())

    async def test_assistant_text_posts(self, in_memory_db) -> None:
        reg, bot, _ = _make_registry(in_memory_db)
        task, _ = await _bind_running_task(reg)
        await reg._render(task, events.AssistantText(text="done"))
        assert any(c["content"] == "done" for c in bot.get_post_calls())

    async def test_title_change_renames(self, in_memory_db) -> None:
        reg, bot, _ = _make_registry(in_memory_db)
        task, _ = await _bind_running_task(reg)
        await reg._render(task, events.TitleChange(title="my-feature"))
        assert bot._rename_calls[0]["name"] == "my-feature"

    async def test_subagent_embed_lifecycle(self, in_memory_db) -> None:
        reg, bot, _ = _make_registry(in_memory_db)
        task, _ = await _bind_running_task(reg)
        await reg._render(task, events.SubagentStarted(handle="h1", subagent_type="researcher", model="m"))
        assert bot._embed_calls  # embed posted
        await reg._render(task, events.SubagentCompleted(handle="h1", outcome_kind="success", message=None, result_summary="ok"))
        assert bot._edit_calls  # embed edited to finished


class TestReconcile:
    async def test_reconcile_recovers_live_marks_dead_crashed(self, in_memory_db) -> None:
        reg, bot, sup = _make_registry(in_memory_db)
        await state.upsert_task(in_memory_db, "live", 100, "/w", "running", polytoken_session_id="sess-live", port=1)
        await state.upsert_task(in_memory_db, "dead", 101, "/w", "running", polytoken_session_id="sess-dead", port=2)
        from tests.fakes import _SessionInfo

        sup.sessions = [_SessionInfo("sess-live", 55555, project_path="/w")]
        await reg.load_from_db(reconcile_with_daemons=True)
        live = reg.get_by_task_id("live")
        assert live is not None and live.status == "running" and live.port == 55555
        dead_row = await state.get_task(in_memory_db, "dead")
        assert dead_row.status == "crashed"

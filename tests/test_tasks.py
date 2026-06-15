"""Tests for the daemon-backed TaskRegistry."""

import asyncio
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

    async def test_stop_warns_on_terminate_http_error(self, in_memory_db) -> None:
        # An HTTP-error terminate (daemon alive but rejecting) warns the user
        # but still tears down bridge-side state.
        reg, bot, _ = _make_registry(in_memory_db)
        task, fclient = await _bind_running_task(reg)
        fclient.terminate_error_status = 500
        await reg.stop_task(task.task_id)
        assert task.status == "stopped"
        assert any("rejected terminate" in c["content"] for c in bot.get_post_calls())

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

    async def test_set_model(self, in_memory_db) -> None:
        reg, _, _ = _make_registry(in_memory_db)
        task, fake = await _bind_running_task(reg)
        await reg.set_model(task.task_id, "openai/gpt-5.5", reasoning_effort="high")
        assert fake.model_calls[-1] == {"model": "openai/gpt-5.5", "reasoning_effort": "high"}

    async def test_set_facet(self, in_memory_db) -> None:
        reg, _, _ = _make_registry(in_memory_db)
        task, fake = await _bind_running_task(reg)
        await reg.set_facet(task.task_id, "plan")
        assert fake.facet_calls == ["plan"]

    async def test_list_models(self, in_memory_db) -> None:
        reg, _, _ = _make_registry(in_memory_db)
        models = await reg.list_models()
        assert "anthropic/claude-opus-4-8" in models


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

    async def test_reconcile_keeps_rows_when_listing_fails(self, in_memory_db) -> None:
        # A transient `polytoken sessions` failure must NOT mass-crash tasks.
        reg, bot, sup = _make_registry(in_memory_db)
        await state.upsert_task(in_memory_db, "t1", 100, "/w", "running", polytoken_session_id="sess-1", port=7)
        sup.fail_list = True
        await reg.load_from_db(reconcile_with_daemons=True)
        task = reg.get_by_task_id("t1")
        assert task is not None and task.status == "running"
        row = await state.get_task(in_memory_db, "t1")
        assert row.status == "running"  # not crashed
        assert not bot.get_archive_calls()


class TestDaemonDeath:
    async def test_daemon_is_gone_true_when_absent(self, in_memory_db) -> None:
        reg, _, sup = _make_registry(in_memory_db)
        task, _ = await _bind_running_task(reg)
        sup.sessions = []  # session not in registry
        assert await reg._daemon_is_gone(task) is True

    async def test_daemon_is_gone_false_when_present(self, in_memory_db) -> None:
        reg, _, sup = _make_registry(in_memory_db)
        task, _ = await _bind_running_task(reg)
        from tests.fakes import _SessionInfo

        sup.sessions = [_SessionInfo(task.polytoken_session_id, task.port)]
        assert await reg._daemon_is_gone(task) is False

    async def test_daemon_is_gone_false_when_listing_fails(self, in_memory_db) -> None:
        reg, _, sup = _make_registry(in_memory_db)
        task, _ = await _bind_running_task(reg)
        sup.fail_list = True  # inconclusive -> keep retrying
        assert await reg._daemon_is_gone(task) is False

    async def test_handle_daemon_death_marks_crashed(self, in_memory_db) -> None:
        reg, bot, _ = _make_registry(in_memory_db)
        task, fake = await _bind_running_task(reg)
        await reg._handle_daemon_death(task)
        assert task.status == "crashed"
        assert reg.get_by_thread_id(task.thread_id) is None
        assert bot.get_archive_calls()
        assert any("daemon" in c["content"].lower() for c in bot.get_post_calls())


class TestConsumerStartup:
    async def test_consumers_deferred_until_bot_ready(self, in_memory_db, monkeypatch) -> None:
        # load_from_db must NOT start consumers (bot isn't ready yet);
        # start_event_consumers() does, after serve binds the bot.
        started: list[str] = []
        monkeypatch.setattr(
            TaskRegistry, "_start_consumer", lambda self, task: started.append(task.task_id)
        )
        reg, bot, sup = _make_registry(in_memory_db)
        from tests.fakes import _SessionInfo

        await state.upsert_task(
            in_memory_db, "t1", 100, "/w", "running", polytoken_session_id="sess-1", port=1
        )
        sup.sessions = [_SessionInfo("sess-1", 5, project_path="/w")]
        await reg.load_from_db(reconcile_with_daemons=True)
        assert started == []  # deferred — not started during reconcile
        await reg.start_event_consumers()
        assert "t1" in started


class TestTeardownIdempotent:
    async def test_first_terminal_transition_wins(self, in_memory_db) -> None:
        reg, bot, _ = _make_registry(in_memory_db)
        task, _ = await _bind_running_task(reg)
        await reg._teardown_task(task, status="stopped", archive=True)
        archives = len(bot.get_archive_calls())
        # A racing teardown (e.g. daemon-death after /stop) is a no-op.
        await reg._teardown_task(task, status="crashed", archive=True)
        assert task.status == "stopped"
        assert len(bot.get_archive_calls()) == archives


class TestReconcileAction:
    async def test_reconcile_posts_notice_and_resyncs(self, in_memory_db) -> None:
        reg, bot, _ = _make_registry(in_memory_db)
        task, _ = await _bind_running_task(reg)
        reg._translators[task.task_id] = events.Translator()
        await reg._render(task, events.Reconcile(reason="stream_discontinuity missed=3"))
        assert any("gap" in c["content"].lower() for c in bot.get_post_calls())
        # re-synced the session title from /state
        assert bot._rename_calls and bot._rename_calls[0]["name"] == "fake-title"

    async def test_reconcile_recovers_pending_interrogative(self, in_memory_db) -> None:
        # A gap covering an interrogative must not strand the daemon: reconcile
        # re-registers it from /state.pending_interrogatives.
        reg, bot, fake = _make_registry(in_memory_db)
        task, fclient = await _bind_running_task(reg)
        reg._translators[task.task_id] = events.Translator()
        fclient.state_payload = dict(fclient.state_payload)
        fclient.state_payload["pending_interrogatives"] = [
            {"type": "interrogative", "interrogative_id": "i9",
             "question": "pick one?", "interrogative_type": "confirmation"}
        ]
        await reg._render(task, events.Reconcile(reason="gap"))
        pending = reg._pending_interrogatives.get(task.task_id)
        assert pending is not None and pending.interrogative_id == "i9"
        assert any("pick one?" in c["content"] for c in bot.get_post_calls())

    async def test_reconcile_does_not_double_post_gap_interrogative(self, in_memory_db) -> None:
        # When a gap's first event IS the interrogative, the translator returns
        # [Reconcile, Confirmation]. The reconcile re-feed + the direct render
        # must converge on a SINGLE post.
        from bridge.polytoken_client import SseEnvelope

        reg, bot, _ = _make_registry(in_memory_db)
        task, fclient = await _bind_running_task(reg)
        translator = events.Translator()
        reg._translators[task.task_id] = translator
        ev = {"type": "interrogative", "interrogative_id": "i9",
              "question": "pick one?", "interrogative_type": "confirmation"}
        fclient.state_payload = dict(fclient.state_payload)
        fclient.state_payload["pending_interrogatives"] = [ev]
        # last_seq=0, then a gapped envelope (seq=2) whose event is the interrogative.
        translator.handle(SseEnvelope(seq=0, session_id="s", emitted_at=None,
                                      event={"type": "heartbeat", "timestamp": "t"}))
        actions = translator.handle(SseEnvelope(seq=2, session_id="s", emitted_at=None, event=ev))
        for a in actions:  # render in the same order _consume_events would
            await reg._render(task, a)
        posts = [c for c in bot.get_post_calls() if "pick one?" in c["content"]]
        assert len(posts) == 1
        assert reg._pending_interrogatives[task.task_id].interrogative_id == "i9"


class TestShutdown:
    async def test_shutdown_cancels_consumers_and_closes_clients(self, in_memory_db) -> None:
        reg, _, _ = _make_registry(in_memory_db)
        task, fclient = await _bind_running_task(reg)
        consumer = asyncio.create_task(asyncio.sleep(100))
        reg._consumers[task.task_id] = consumer
        await reg.shutdown()
        assert consumer.cancelled() or consumer.done()
        assert fclient.closed is True
        assert reg._clients == {}

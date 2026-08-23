"""Slack TaskRegistry acceptance tests (AC.2/4/5/6/7/8/10)."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
import json
from pathlib import Path
from typing import Any

import pytest

from bridge import events, state
from bridge.domain import ConversationKey, Participant, ParticipantKind, PendingInterrogative
from bridge.polytoken_client import PolytokenClientError
from bridge.tasks import (
    SlackActor,
    SlackFile,
    SlackMessage,
    Task,
    TaskNotFound,
    TaskPrivilegeError,
    TaskRegistry,
    TaskRestartError,
    TaskRoutingError,
    TaskSpawnError,
)


@dataclass
class SpawnResult:
    session_id: str
    port: int
    credential_file_path: str | None = None


@dataclass
class FakeClient:
    port: int = 41000
    prompts: list[str] = field(default_factory=list)
    interrogative_responses: list[dict[str, Any]] = field(default_factory=list)
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    facet_calls: list[str] = field(default_factory=list)
    title_calls: list[str] = field(default_factory=list)
    reload_calls: int = 0
    compact_calls: int = 0
    clear_calls: int = 0
    terminated: int = 0
    cancelled: int = 0
    terminate_error_status: int | None = None
    closed: bool = False
    state_sequence: list[dict[str, Any]] = field(default_factory=list)
    state_payload: dict[str, Any] = field(default_factory=lambda: {
        "active_model": "anthropic/claude-opus-4-8",
        "session_title": "fake-title",
        "pending_interrogatives": [],
    })

    async def prompt(self, content: str, *, max_tool_turns=None):
        self.prompts.append(content)

    async def respond_interrogative(self, interrogative_id: str, response: dict[str, Any]):
        self.interrogative_responses.append({"id": interrogative_id, "response": response})

    async def state(self):
        if self.state_sequence:
            return dict(self.state_sequence.pop(0))
        return dict(self.state_payload)

    async def set_model(self, model: str, *, reasoning_effort=None):
        self.model_calls.append({"model": model, "reasoning_effort": reasoning_effort})

    async def set_facet(self, facet: str):
        self.facet_calls.append(facet)

    async def set_title(self, title: str):
        self.title_calls.append(title)
        self.state_payload["session_title"] = title

    async def reload(self):
        self.reload_calls += 1
        return {"reloaded": ["models"], "failed": []}

    async def compact(self):
        self.compact_calls += 1
        return f"compact-{self.compact_calls}"

    async def clear(self):
        self.clear_calls += 1
        return {"status": "cleared"}

    async def cancel_turn(self):
        self.cancelled += 1

    async def terminate(self):
        if self.terminate_error_status is not None:
            raise PolytokenClientError("rejected", status=self.terminate_error_status)
        self.terminated += 1

    async def aclose(self):
        self.closed = True


@dataclass
class FakeSupervisor:
    fail_spawn: bool = False
    _seq: int = 0
    next_channel: str = "GNEW"
    next_root: str = "9000.000"
    credential_file_path: str | None = None

    async def spawn(self, cwd: str, *, config_dir=None):
        if self.fail_spawn:
            from bridge.daemon_supervisor import DaemonSupervisorError
            raise DaemonSupervisorError("spawn failed")
        self._seq += 1
        return SpawnResult(f"sess-{self._seq}", 41000 + self._seq, self.credential_file_path)

    async def find_session(self, session_id: str):
        return object()

    async def list_sessions(self):
        return []

    async def list_models(self):
        return ["anthropic/claude-opus-4-8"]


@dataclass
class FakeBot:
    team_id: str = "T1"
    owner_user_id: str = "UOWNER"
    home_channel_id: str = "CHOME"
    app_actor_id: str = "AAPP"
    bot_user_id: str = "UBRIDGE"
    bot_id: str = "B-BRIDGE"
    posts: list[dict[str, Any]] = field(default_factory=list)
    edits: list[dict[str, Any]] = field(default_factory=list)
    roots: list[dict[str, Any]] = field(default_factory=list)
    private_channels: list[str] = field(default_factory=list)
    invites: list[dict[str, Any]] = field(default_factory=list)
    archives: list[str] = field(default_factory=list)
    kicks: list[dict[str, Any]] = field(default_factory=list)
    downloads: list[dict[str, Any]] = field(default_factory=list)
    reactions: list[dict[str, Any]] = field(default_factory=list)
    deletions: list[dict[str, Any]] = field(default_factory=list)
    thread_pages: dict[str, dict[str, Any]] = field(default_factory=dict)
    thread_calls: list[dict[str, Any]] = field(default_factory=list)
    suggested_prompts: list[dict[str, Any]] = field(default_factory=list)
    conversation_infos: dict[str, dict[str, Any]] = field(default_factory=dict)
    fail_invite: bool = False
    fail_root: bool = False
    _message_seq: int = 0

    async def post(self, text: str, channel_id: str, root_ts=None, blocks=None, fallback_text=None):
        self._message_seq += 1
        ts = f"m{self._message_seq}"
        self.posts.append({"text": text, "channel_id": channel_id, "root_ts": root_ts, "blocks": blocks, "fallback_text": fallback_text, "ts": ts})
        return [ts]

    async def edit_message(self, channel_id: str, message_ts: str, *, text=None, blocks=None, content=None):
        self.edits.append({"channel_id": channel_id, "message_ts": message_ts, "text": text, "blocks": blocks, "content": content})

    async def delete_message(self, channel_id: str, message_ts: str):
        self.deletions.append({"channel_id": channel_id, "message_ts": message_ts})

    async def post_with_attachments(self, paths, *, channel_id, root_ts, text):
        self.posts.append({"attachments": list(paths), "channel_id": channel_id, "root_ts": root_ts, "text": text})
        return ["attachment-message"]

    async def create_task_root(self, channel_id: str, text: str, blocks=None):
        if self.fail_root:
            raise RuntimeError("root failed")
        self.roots.append({"channel_id": channel_id, "text": text, "blocks": blocks})
        return "1000.001"

    async def create_private_channel(self, name: str):
        self.private_channels.append(name)
        return "GNEW"

    async def invite_participants(self, channel_id: str, actor_ids: list[str]):
        if self.fail_invite:
            raise RuntimeError("invite failed")
        self.invites.append({"channel_id": channel_id, "actor_ids": list(actor_ids)})

    async def archive_channel(self, channel_id: str):
        self.archives.append(channel_id)

    async def remove_participants(self, channel_id: str, user_ids: list[str]):
        self.kicks.extend({"channel_id": channel_id, "user_id": user_id} for user_id in user_ids)

    async def download_file(self, url: str, path: Path, max_bytes: int):
        self.downloads.append({"url": url, "path": path, "max_bytes": max_bytes})
        path.write_bytes(b"downloaded")

    async def fetch_thread_replies(self, channel_id: str, root_ts: str, *, cursor=None, limit=100):
        self.thread_calls.append({"channel_id": channel_id, "root_ts": root_ts, "cursor": cursor, "limit": limit})
        return self.thread_pages.get(str(cursor or ""), {"messages": [], "has_more": False})

    async def conversation_info(self, channel_id: str):
        return self.conversation_infos.get(channel_id, {"id": channel_id, "name": channel_id})

    async def set_suggested_prompts(self, channel_id: str, *, title: str, prompts):
        self.suggested_prompts.append({"channel_id": channel_id, "title": title, "prompts": prompts})

    async def add_reaction(self, channel_id: str, message_ts: str, name: str):
        self.reactions.append({"channel_id": channel_id, "message_ts": message_ts, "name": name})


class RichFakeBot(FakeBot):
    """Fake native stream surface used by deterministic progress tests."""

    def __init__(self):
        super().__init__()
        self.stream_starts: list[dict[str, Any]] = []
        self.stream_appends: list[dict[str, Any]] = []
        self.stream_stops: list[dict[str, Any]] = []
        self.statuses: list[dict[str, str]] = []
        self.renames: list[dict[str, str]] = []

    async def start_stream(self, channel_id, thread_ts, *, recipient_user_id, recipient_team_id,
                           chunks=None, task_display_mode=None, markdown_text=None):
        call = {
            "channel_id": channel_id, "thread_ts": thread_ts,
            "recipient_user_id": recipient_user_id, "recipient_team_id": recipient_team_id,
            "chunks": chunks, "task_display_mode": task_display_mode,
            "markdown_text": markdown_text,
        }
        self.stream_starts.append(call)
        return f"stream-{len(self.stream_starts)}"

    async def append_stream(self, channel_id, stream_ts, *, markdown_text=None, chunks=None):
        self.stream_appends.append({
            "channel_id": channel_id, "stream_ts": stream_ts,
            "markdown_text": markdown_text, "chunks": chunks,
        })

    async def stop_stream(self, channel_id, stream_ts, **kwargs):
        self.stream_stops.append({"channel_id": channel_id, "stream_ts": stream_ts, **kwargs})

    async def set_agent_status(self, channel_id, thread_ts, status):
        self.statuses.append({"channel_id": channel_id, "thread_ts": thread_ts, "status": status})
        return True

    async def rename_agent_session(self, channel_id, thread_ts, title):
        self.renames.append({"channel_id": channel_id, "thread_ts": thread_ts, "title": title})
        return True


@pytest.fixture(autouse=True)
def _disable_sse_consumers(monkeypatch):
    monkeypatch.setattr(TaskRegistry, "_start_consumer", lambda self, task: None)


def _key(task: Task) -> ConversationKey:
    return ConversationKey(task.team_id, task.channel_id, task.root_ts)


async def _task(reg: TaskRegistry, *, mode="personal", channel="CHOME", root="1000.000", owner="UOWNER", client=None):
    task = Task("t1", "T1", channel, root, owner, mode, "/tmp", "running", "sess-1", 41001, 1, 1, app_exchange_budget=reg.app_exchange_budget)
    await reg.attach_task(task)
    fake = client or FakeClient(port=41001)
    fake.port = task.port
    reg._clients[task.task_id] = fake
    return task, fake


def _registry(db, *, budget=20):
    bot = FakeBot()
    reg = TaskRegistry(db, bot, FakeSupervisor(), app_exchange_budget=budget)
    return reg, bot


@pytest.mark.asyncio
async def test_fresh_spawn_carries_runtime_credential_path(in_memory_db, tmp_path):
    bot = FakeBot()
    supervisor = FakeSupervisor(credential_file_path=str(tmp_path / "daemon.json"))
    registry = TaskRegistry(in_memory_db, bot, supervisor)
    task = await registry.spawn_task(
        "/tmp", team_id="T1", channel_id="CHOME", owner_user_id="UOWNER"
    )
    assert task.credential_file_path == str(tmp_path / "daemon.json")
    runtime = await state.get_runtime(in_memory_db, task.task_id)
    assert runtime is not None
    assert runtime.session_id == task.polytoken_session_id
    assert runtime.port == task.port
    assert runtime.status == "running"


@pytest.mark.asyncio
async def test_bind_bot_uses_production_bot_user_identity(in_memory_db):
    bot = FakeBot()
    registry = TaskRegistry(in_memory_db, None, FakeSupervisor(), app_actor_id="WRONG")
    registry.bind_bot(bot)
    assert registry.app_actor_id == "UBRIDGE"
    assert registry.app_actor_id != bot.app_actor_id


@pytest.mark.asyncio
async def test_pending_promotion_absent_daemon_stays_crashed_without_consumer(in_memory_db, monkeypatch):
    bot = FakeBot()
    supervisor = FakeSupervisor()
    registry = TaskRegistry(in_memory_db, None, supervisor)
    task = Task("journal-task", "T1", "COLD", "R1", "UOWNER", "personal", "/tmp", "running", "missing-session", 41001, 1, 1)
    await registry.attach_task(task)
    await state.upsert_participant(in_memory_db, task.key, Participant("U1", ParticipantKind.HUMAN, "human"))
    await state.create_promotion_journal(in_memory_db, "j1", task.task_id, task.key, "personal")
    monkeypatch.setattr(registry, "_start_consumer", lambda task: (_ for _ in ()).throw(AssertionError("consumer started")))
    await registry.load_from_db(reconcile_with_daemons=True)
    loaded = registry.get_by_task_id(task.task_id)
    assert loaded is not None and loaded.status == "crashed"
    assert (await state.get_runtime(in_memory_db, task.task_id)).status == "crashed"
    registry.bind_bot(bot)
    await registry.reconcile_promotion_journals()
    loaded = registry.get_by_task_id(task.task_id)
    assert loaded is not None and loaded.status == "crashed"
    assert (await state.get_runtime(in_memory_db, task.task_id)).status == "crashed"
    assert task.task_id not in registry._consumers


@pytest.mark.asyncio
async def test_pending_promotion_list_failure_preserves_binding_and_retries_consumer(in_memory_db, monkeypatch):
    class FailingSupervisor(FakeSupervisor):
        async def list_sessions(self):
            from bridge.daemon_supervisor import DaemonSupervisorError
            raise DaemonSupervisorError("registry listing failed")

    registry = TaskRegistry(in_memory_db, None, FailingSupervisor())
    task = Task("retry-journal-task", "T1", "COLD", "R1", "UOWNER", "personal", "/tmp", "running", "live-session", 41001, 1, 1, binding_id="old-binding")
    await registry.attach_task(task)
    await state.create_promotion_journal(in_memory_db, "retry-j1", task.task_id, task.key, "personal", old_binding_id="old-binding")
    started: list[str] = []
    monkeypatch.setattr(registry, "_start_consumer", lambda loaded: started.append(loaded.task_id))

    await registry.load_from_db(reconcile_with_daemons=True)
    registry.bind_bot(FakeBot())
    await registry.reconcile_promotion_journals()
    await registry.start_event_consumers()

    loaded = registry.get_by_task_id(task.task_id)
    runtime = await state.get_runtime(in_memory_db, task.task_id)
    assert loaded is not None
    assert (loaded.status, loaded.channel_id, loaded.root_ts, loaded.binding_id) == ("running", "COLD", "R1", "old-binding")
    assert (runtime.status, runtime.binding_id) == ("running", "old-binding")
    assert started == [task.task_id, task.task_id]


@pytest.mark.asyncio
async def test_pending_promotion_live_daemon_restores_running_consumer_and_routing(in_memory_db, monkeypatch):
    class LiveSupervisor(FakeSupervisor):
        async def list_sessions(self):
            return [SimpleNamespace(
                session_id="live-session", port=41001, project_path="/tmp",
                credential_file_path="/run/pt/live-session.json",
            )]

    bot = FakeBot()
    registry = TaskRegistry(in_memory_db, None, LiveSupervisor())
    task = Task("live-journal-task", "T1", "COLD", "R1", "UOWNER", "personal", "/tmp", "running", "live-session", 41001, 1, 1)
    await registry.attach_task(task)
    client = FakeClient(port=41001)
    client.credential_file_path = "/run/pt/live-session.json"
    registry._clients[task.task_id] = client
    await state.upsert_participant(in_memory_db, task.key, Participant("UHELP", ParticipantKind.HUMAN, "helper"))
    await state.create_promotion_journal(in_memory_db, "live-j1", task.task_id, task.key, "personal")
    started: list[str] = []
    monkeypatch.setattr(registry, "_start_consumer", lambda loaded: started.append(loaded.task_id))

    await registry.load_from_db(reconcile_with_daemons=True)
    registry.bind_bot(bot)
    await registry.reconcile_promotion_journals()

    loaded = registry.get_by_task_id(task.task_id)
    assert loaded is not None and loaded.status == "running"
    assert loaded.credential_file_path == "/run/pt/live-session.json"
    assert (await state.get_runtime(in_memory_db, task.task_id)).status == "running"
    assert started == [task.task_id]
    assert await registry.maybe_route_message(SlackMessage("T1", "COLD", "R1", SlackActor("UOWNER"), "after restart", "E-live", "M-live"))
    assert json.loads(client.prompts[0])["body"] == "after restart"


@pytest.mark.asyncio
async def test_normalized_nested_ingress_uses_scalar_stable_id_and_dedups(in_memory_db):
    reg, _ = _registry(in_memory_db)
    task, client = await _task(reg, root="1000.normalized")
    normalized = {"event": {"team": "T1", "channel": "CHOME", "ts": "1000.normalized", "user": "UOWNER", "text": "hello"}, "id": "ENVELOPE-1", "team_id": "T1", "channel_id": "CHOME", "root_ts": "1000.normalized", "actor_id": "UOWNER", "text": "hello"}
    msg = __import__("bridge.tasks", fromlist=["normalize_message"]).normalize_message(normalized)
    assert msg.event_id == "ENVELOPE-1" and not isinstance(msg.event_id, dict)
    assert (msg.team_id, msg.channel_id, msg.root_ts, msg.actor_id) == ("T1", "CHOME", "1000.normalized", "UOWNER")
    assert await reg.maybe_route_message(msg)
    assert await reg.maybe_route_message(msg)
    assert len(client.prompts) == 1


@pytest.mark.asyncio
async def test_restart_cleanup_remembers_journal_channel_before_verified_archive(in_memory_db):
    old = ConversationKey("T1", "COLD", "R-clean")
    registry = TaskRegistry(in_memory_db, None, FakeSupervisor())
    task = Task("cleanup-journal-task", "T1", "COLD", "R-clean", "UOWNER", "personal", "/tmp", "running", "sess-clean", 41001, 1, 1)
    await registry.attach_task(task)
    await state.create_promotion_journal(in_memory_db, "cleanup-j1", task.task_id, old, "personal")
    await state.update_promotion_journal(in_memory_db, "cleanup-j1", new_channel_id="GNEW", state="cleanup_pending")

    class OwnershipBot(FakeBot):
        def __init__(self):
            super().__init__()
            self.remembered: set[str] = set()
        def remember_owned_channel(self, channel_id):
            self.remembered.add(channel_id)
        async def archive_channel(self, channel_id):
            assert channel_id in self.remembered
            await super().archive_channel(channel_id)

    bot = OwnershipBot()
    fresh = TaskRegistry(in_memory_db, bot, FakeSupervisor())
    await fresh.load_from_db()
    await fresh.reconcile_promotion_journals()
    await fresh.reconcile_promotion_journals()
    assert bot.remembered == {"GNEW"}
    assert bot.archives == ["GNEW"]
    assert (await state.get_promotion_journal(in_memory_db, "cleanup-j1")).state == "failed"


class TestAC2IdentityRouting:
    async def test_public_home_observer_cannot_prompt_personal_task(self, in_memory_db):
        reg, _ = _registry(in_memory_db)
        task, client = await _task(reg)
        assert reg.get_by_key(_key(task)) is task
        assert await reg.maybe_route_message(SlackMessage("T1", "CHOME", "1000.000", SlackActor("UOWNER"), "hello", "E1", "M1"))
        assert json.loads(client.prompts[0])["body"] == "hello"
        assert not await reg.maybe_route_message(SlackMessage("T1", "CHOME", "1000.000", SlackActor("UOTHER"), "no", "E2", "M2"))
        assert len(client.prompts) == 1

    async def test_collaborative_requires_explicit_participant(self, in_memory_db):
        reg, _ = _registry(in_memory_db)
        task, client = await _task(reg, mode="collaborative", root="1000.002")
        assert not await reg.maybe_route_message(SlackMessage("T1", "CHOME", task.root_ts, SlackActor("UOTHER"), "no", "E1", "M1"))
        await state.upsert_participant(in_memory_db, task.key, Participant("UOTHER", ParticipantKind.HUMAN))
        assert await reg.maybe_route_message(SlackMessage("T1", "CHOME", task.root_ts, SlackActor("UOTHER"), "yes", "E2", "M2"))
        assert json.loads(client.prompts[0])["body"] == "yes"

    async def test_provenance_is_stable_and_dedup_covers_event_and_message(self, in_memory_db):
        reg, _ = _registry(in_memory_db)
        task, client = await _task(reg, root="1000.003")
        msg = SlackMessage("T1", "CHOME", task.root_ts, SlackActor("UOWNER"), "raw body", "E1", "M1")
        assert await reg.maybe_route_message(msg)
        assert await reg.maybe_route_message(msg)
        assert json.loads(client.prompts[0])["body"] == "raw body"
        assert task.last_envelope is not None
        assert task.last_envelope.body == "raw body"
        assert task.last_envelope.provenance.team_id == "T1"
        assert task.last_envelope.provenance.actor_id == "UOWNER"

    async def test_self_and_unknown_messages_are_ignored(self, in_memory_db):
        reg, _ = _registry(in_memory_db)
        task, client = await _task(reg, root="1000.004")
        assert not await reg.maybe_route_message(SlackMessage("T1", "CHOME", task.root_ts, SlackActor("AAPP", is_app=True), "self", "E1", "M1"))
        assert not await reg.maybe_route_message(SlackMessage("T1", "CHOME", "other-root", SlackActor("UOWNER"), "unknown", "E2", "M2"))
        assert client.prompts == []

    async def test_owner_mention_without_task_starts_ad_hoc_bluesky_session(self, in_memory_db, tmp_path):
        bot = FakeBot()
        supervisor = FakeSupervisor()
        reg = TaskRegistry(in_memory_db, bot, supervisor, ad_hoc_cwd=str(tmp_path))
        bot.thread_pages = {"": {"messages": [
            {"ts": "1.000", "user": "UOTHER", "text": "earlier thread context"},
        ], "has_more": False}}
        prompts: list[str] = []

        async def capture(_task, content, **_kwargs):
            prompts.append(content)
            return True

        reg._prompt = capture  # type: ignore[method-assign]
        event = {
            "kind": "app_mention", "team_id": "T1", "channel_id": "COTHER",
            "root_ts": "1.000", "message_ts": "2.000", "actor_id": "UOWNER",
            "id": "E-ad-hoc", "text": "<@UBRIDGE> deploy Attie with sha abc123",
        }
        assert await reg.maybe_route_message(event)
        task = reg.get_by_conversation("T1", "COTHER", "1.000")
        assert task is not None
        assert task.cwd == str(tmp_path)
        assert task.mention_required is True
        assert supervisor._seq == 1
        assert len(prompts) == 1
        assert "earlier thread context" in prompts[0]
        assert "deploy Attie with sha abc123" in prompts[0]
        assert '"actor_id":"UOWNER"' in prompts[0]
        assert await reg.maybe_route_message(event)
        assert supervisor._seq == 1
        await reg.shutdown()

    async def test_non_owner_mention_without_task_is_ignored(self, in_memory_db, tmp_path):
        bot = FakeBot()
        supervisor = FakeSupervisor()
        reg = TaskRegistry(in_memory_db, bot, supervisor, ad_hoc_cwd=str(tmp_path))
        assert not await reg.maybe_route_message({
            "kind": "app_mention", "team_id": "T1", "channel_id": "COTHER",
            "root_ts": "1.000", "message_ts": "2.000", "actor_id": "UOTHER",
            "id": "E-unauthorized", "text": "<@UBRIDGE> do something",
        })
        assert supervisor._seq == 0
        await reg.shutdown()

    async def test_owner_dm_without_task_starts_ad_hoc_session(self, in_memory_db, tmp_path):
        bot = FakeBot()
        supervisor = FakeSupervisor()
        reg = TaskRegistry(in_memory_db, bot, supervisor, ad_hoc_cwd=str(tmp_path))
        prompts: list[str] = []

        async def capture(_task, content, **_kwargs):
            prompts.append(content)
            return True

        reg._prompt = capture  # type: ignore[method-assign]
        event = {
            "kind": "message", "team_id": "T1", "channel_id": "DOWNERDM",
            "channel_type": "im", "root_ts": "1.000", "message_ts": "1.000",
            "actor_id": "UOWNER", "id": "E-dm-1",
            "text": "investigate the failing deploy",
        }
        assert await reg.maybe_route_message(event)
        task = reg.get_by_conversation("T1", "DOWNERDM", "1.000")
        assert task is not None
        assert task.cwd == str(tmp_path)
        # Agent-view DMs are already addressed to the bridge; no mention gate.
        assert task.mention_required is False
        assert supervisor._seq == 1
        assert len(prompts) == 1
        assert "investigate the failing deploy" in prompts[0]

        # A threaded follow-up in the DM session routes without any mention.
        followup = {
            "kind": "message", "team_id": "T1", "channel_id": "DOWNERDM",
            "channel_type": "im", "root_ts": "1.000", "message_ts": "2.000",
            "actor_id": "UOWNER", "id": "E-dm-2", "text": "also check the logs",
        }
        assert await reg.maybe_route_message(followup)
        assert supervisor._seq == 1  # no second daemon
        assert len(prompts) == 2
        assert "also check the logs" in prompts[1]
        await reg.shutdown()

    async def test_non_owner_dm_without_task_is_ignored(self, in_memory_db, tmp_path):
        bot = FakeBot()
        supervisor = FakeSupervisor()
        reg = TaskRegistry(in_memory_db, bot, supervisor, ad_hoc_cwd=str(tmp_path))
        assert not await reg.maybe_route_message({
            "kind": "message", "team_id": "T1", "channel_id": "DSTRANGER",
            "channel_type": "im", "root_ts": "1.000", "message_ts": "1.000",
            "actor_id": "UOTHER", "id": "E-dm-stranger", "text": "do something",
        })
        assert supervisor._seq == 0
        await reg.shutdown()

    async def test_empty_owner_dm_gets_usage_hint_without_spawning(self, in_memory_db, tmp_path):
        bot = FakeBot()
        supervisor = FakeSupervisor()
        reg = TaskRegistry(in_memory_db, bot, supervisor, ad_hoc_cwd=str(tmp_path))
        assert await reg.maybe_route_message({
            "kind": "message", "team_id": "T1", "channel_id": "DOWNERDM",
            "channel_type": "im", "root_ts": "3.000", "message_ts": "3.000",
            "actor_id": "UOWNER", "id": "E-dm-empty", "text": "   ",
        })
        assert supervisor._seq == 0
        assert "Tell me what you want" in bot.posts[-1]["text"]
        await reg.shutdown()

    async def test_agent_view_home_sets_owner_prompts_without_posting(self, in_memory_db):
        reg, bot = _registry(in_memory_db)
        assert await reg.handle_app_home_opened({
            "team_id": "T1", "actor_id": "UOWNER", "channel_id": "DOWNERDM",
            "event": {"type": "app_home_opened", "tab": "messages", "user": "UOWNER", "channel": "DOWNERDM"},
        })
        assert bot.suggested_prompts[0]["channel_id"] == "DOWNERDM"
        assert len(bot.suggested_prompts[0]["prompts"]) == 4
        assert bot.posts == []

    async def test_agent_context_is_authenticated_resolved_and_consumed_by_next_dm(self, in_memory_db):
        reg, bot = _registry(in_memory_db)
        task, client = await _task(reg, channel="DOWNERDM", root="1.000")
        bot.conversation_infos["CCONTEXT"] = {"id": "CCONTEXT", "name": "deploys"}
        bot.thread_pages[""] = {"messages": [{"team": "T1", "channel": "CCONTEXT", "ts": "9.000", "user": "UOTHER", "text": "deploy is failing"}], "has_more": False}
        assert await reg.handle_app_context_changed({
            "team_id": "T1", "event": {"type": "app_context_changed", "context": {"entities": [
                {"type": "slack#/types/message_context", "team_id": "T1", "value": {"channel_id": "CCONTEXT", "message_ts": "9.000"}},
            ]}},
        })
        assert await reg.maybe_route_message({
            "kind": "message", "team_id": "T1", "channel_id": "DOWNERDM", "channel_type": "im",
            "root_ts": task.root_ts, "message_ts": "2.000", "actor_id": "UOWNER", "id": "E-context",
            "text": "investigate this",
        })
        body = json.loads(client.prompts[0])["body"]
        assert "Slack Agent View context" in body
        assert '"channel_name": "deploys"' in body
        assert '"text": "deploy is failing"' in body
        # Context is one-shot: the next prompt does not silently reuse stale entities.
        assert await reg.maybe_route_message({
            "kind": "message", "team_id": "T1", "channel_id": "DOWNERDM", "channel_type": "im",
            "root_ts": task.root_ts, "message_ts": "3.000", "actor_id": "UOWNER", "id": "E-context-2",
            "text": "follow up",
        })
        assert "Slack Agent View context" not in json.loads(client.prompts[1])["body"]

    def test_source_blocks_deduplicate_and_label_answer_urls(self):
        blocks = TaskRegistry._source_blocks("See https://docs.slack.dev/x and https://docs.slack.dev/x. Also https://example.com/report")
        assert len(blocks) == 1 and blocks[0]["type"] == "context"
        text = blocks[0]["elements"][0]["text"]
        assert text.count("docs.slack.dev/x") == 1
        assert "[1] docs.slack.dev" in text and "[2] example.com" in text

    async def test_existing_thread_requires_exact_mention_before_auth_or_files(self, in_memory_db):
        reg, bot = _registry(in_memory_db)
        task, client = await _task(reg, root="1000.004b")
        task.mention_required = True
        attachment = SlackFile("https://files.invalid/a", "a.png", 10, "image/png")
        assert not await reg.maybe_route_message(SlackMessage("T1", "CHOME", task.root_ts, SlackActor("UOWNER"), "ordinary chatter", "E1", "M1", (attachment,)))
        assert not await reg.maybe_route_message(SlackMessage("T1", "CHOME", task.root_ts, SlackActor("UOWNER"), "@bridge hello", "E2", "M2"))
        assert not await reg.maybe_route_message(SlackMessage("T1", "CHOME", task.root_ts, SlackActor("UOWNER"), "<@UBRIDGE|bridge> spoof", "E3", "M3"))
        assert bot.downloads == []
        assert await reg.maybe_route_message(SlackMessage("T1", "CHOME", task.root_ts, SlackActor("UOWNER"), "<@UBRIDGE> verified", "E4", "M4", (attachment,)))
        body = json.loads(client.prompts[0])["body"]
        assert body.startswith("verified [Slack file ")
        assert f'"local_reference": "@{bot.downloads[0]["path"]}"' in body
        assert '"name": "a.png"' in body
        assert '"shared_by": "UOWNER"' in body

    async def test_stopped_existing_thread_mention_gets_visible_restart_notice(self, in_memory_db):
        reg, bot = _registry(in_memory_db)
        task, client = await _task(reg, root="1000.stopped")
        task.mention_required = True
        task.status = "stopped"
        await reg._persist_root(task)
        assert await reg.maybe_route_message(SlackMessage(
            "T1", "CHOME", task.root_ts, SlackActor("UOWNER"),
            "<@UBRIDGE> investigate", "E-stopped", "M-stopped",
        ))
        assert client.prompts == []
        assert "is stopped" in bot.posts[-1]["text"]
        assert "Start agent here" in bot.posts[-1]["text"]

    async def test_existing_thread_trusts_authenticated_app_mention_without_literal_token(self, in_memory_db):
        reg, _ = _registry(in_memory_db)
        task, client = await _task(reg, root="1000.004mention")
        task.mention_required = True
        event = {
            "kind": "app_mention", "team_id": "T1", "channel_id": "CHOME",
            "root_ts": task.root_ts, "actor_id": "UOWNER",
            "message_ts": "M-app-mention", "id": "E-app-mention",
            "text": "summarize this thread",
        }
        assert await reg.maybe_route_message(event)
        assert json.loads(client.prompts[0])["body"] == "summarize this thread"

    async def test_owner_mention_reload_targets_existing_thread_daemon(self, in_memory_db):
        reg, bot = _registry(in_memory_db)
        task, client = await _task(reg, root="1000.reload")
        task.mention_required = True
        assert await reg.maybe_route_message({
            "kind": "app_mention", "team_id": "T1", "channel_id": "CHOME",
            "root_ts": task.root_ts, "actor_id": "UOWNER",
            "message_ts": "M-reload", "id": "E-reload", "text": "reload",
        })
        assert client.reload_calls == 1
        assert client.prompts == []
        assert "Reloaded this session's daemon" in bot.posts[-1]["text"]

    async def test_existing_thread_routes_block_only_rich_text_mention(self, in_memory_db):
        reg, bot = _registry(in_memory_db)
        task, client = await _task(reg, root="1000.004c")
        task.mention_required = True
        event = {
            "team_id": "T1", "channel_id": "CHOME", "root_ts": task.root_ts,
            "actor_id": "UOWNER", "message_ts": "M-rich", "id": "E-rich",
            "text": "", "blocks": [{"type": "rich_text", "elements": [
                {"type": "rich_text_section", "elements": [
                    {"type": "user", "user_id": "UBRIDGE"},
                    {"type": "text", "text": " summarize this thread"},
                ]},
            ]}],
        }
        assert await reg.maybe_route_message(event)
        assert json.loads(client.prompts[0])["body"] == "summarize this thread"
        assert not bot.posts

    async def test_existing_thread_empty_block_mention_gets_visible_guidance(self, in_memory_db):
        reg, bot = _registry(in_memory_db)
        task, client = await _task(reg, root="1000.004d")
        task.mention_required = True
        event = {
            "team_id": "T1", "channel_id": "CHOME", "root_ts": task.root_ts,
            "actor_id": "UOWNER", "message_ts": "M-empty", "id": "E-empty",
            "text": "", "blocks": [{"type": "rich_text", "elements": [
                {"type": "rich_text_section", "elements": [
                    {"type": "user", "user_id": "UBRIDGE"},
                ]},
            ]}],
        }
        assert await reg.maybe_route_message(event)
        assert client.prompts == []
        assert "supplied no request text" in bot.posts[-1]["text"]


class TestAC4AttachmentsAndInterrogatives:
    async def test_authenticated_bounded_attachment_becomes_reference(self, in_memory_db):
        reg, bot = _registry(in_memory_db)
        task, client = await _task(reg, root="1000.005")
        msg = SlackMessage("T1", "CHOME", task.root_ts, SlackActor("UOWNER"), "inspect", "E1", "M1", (SlackFile("https://files.slack.com/private", "notes.txt", 12),))
        assert await reg.maybe_route_message(msg)
        assert bot.downloads and bot.downloads[0]["max_bytes"] > 0
        body = json.loads(client.prompts[0])["body"]
        assert body.startswith("inspect [Slack file ")
        assert f'"local_reference": "@{bot.downloads[0]["path"]}"' in body
        assert '"name": "notes.txt"' in body
        assert any("Added 1 Slack file" in post["text"] for post in bot.posts)

    async def test_pending_is_actor_targeted_and_attachment_does_not_consume(self, in_memory_db):
        reg, bot = _registry(in_memory_db)
        task, client = await _task(reg, root="1000.006")
        await reg._render(task, events.Confirmation("I1", None, "Continue?"))
        assert await reg.maybe_route_message(SlackMessage("T1", "CHOME", task.root_ts, SlackActor("UOTHER"), "yes", "E1", "M1")) is False
        file_msg = SlackMessage("T1", "CHOME", task.root_ts, SlackActor("UOWNER"), "file", "E2", "M2", (SlackFile("https://files.slack.com/x", "x.txt"),))
        assert await reg.maybe_route_message(file_msg)
        assert client.interrogative_responses == []
        assert len(client.prompts) == 1
        answer = SlackMessage("T1", "CHOME", task.root_ts, SlackActor("UOWNER"), "yes", "E3", "M3")
        assert await reg.maybe_route_message(answer)
        assert client.interrogative_responses[0]["response"]["confirmed"] is True
        assert any("Continue?" in post["text"] for post in bot.posts if "text" in post)


@pytest.mark.asyncio
async def test_failed_interrogative_delivery_restores_pending_for_retry(in_memory_db):
    reg, _ = _registry(in_memory_db)
    task, client = await _task(reg, root="1000.interrogative-retry")
    await reg._render(task, events.Confirmation("I-retry", None, "Continue?"))
    original = client.respond_interrogative
    calls = 0

    async def fail_once(interrogative_id, response):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PolytokenClientError("temporary")
        await original(interrogative_id, response)

    client.respond_interrogative = fail_once
    answer = SlackMessage("T1", "CHOME", task.root_ts, SlackActor("UOWNER"), "yes")
    assert await reg.maybe_route_message(answer)
    assert await state.get_pending_interrogative(in_memory_db, task.key, "UOWNER") is not None
    assert await reg.maybe_route_message(SlackMessage("T1", "CHOME", task.root_ts, SlackActor("UOWNER"), "yes"))
    assert client.interrogative_responses[-1]["id"] == "I-retry"


@pytest.mark.asyncio
async def test_failed_old_answer_does_not_overwrite_newer_interrogative(in_memory_db):
    reg, _ = _registry(in_memory_db)
    task, client = await _task(reg, root="1000.interrogative-race")
    await reg._render(task, events.Confirmation("I-old", None, "Old question?"))

    async def accepted_then_failed(interrogative_id, response):
        newer = PendingInterrogative("I-new", "UOWNER", {"kind": "confirmation", "question": "New?"}, 9999999999, 1)
        await state.put_pending_interrogative(in_memory_db, task.key, newer)
        reg._pending[(task.key, "UOWNER")] = newer
        raise PolytokenClientError("ambiguous transport failure")

    client.respond_interrogative = accepted_then_failed
    assert await reg.maybe_route_message(SlackMessage("T1", "CHOME", task.root_ts, SlackActor("UOWNER"), "yes"))
    stored = await state.get_pending_interrogative(in_memory_db, task.key, "UOWNER")
    assert stored is not None and stored.interrogative_id == "I-new"
    assert reg._pending[(task.key, "UOWNER")].interrogative_id == "I-new"


@pytest.mark.asyncio
async def test_turn_status_fallback_is_one_editable_block_and_no_legacy_working_message(in_memory_db):
    reg, bot = _registry(in_memory_db)
    task, _ = await _task(reg, root="1000.status")
    await reg._render(task, events.TurnStarted("prompt-1"))
    assert len(bot.posts) == 1
    assert bot.posts[-1]["root_ts"] == task.root_ts
    assert bot.posts[-1]["blocks"]
    assert "Agent is working" not in bot.posts[-1]["text"]
    fallback_ts = task.progress_fallback_ts
    assert fallback_ts is not None
    await reg._render(task, events.ToolLine("✓ Bash: ls"))
    assert len(bot.posts) == 1  # tool progress stays in the existing card
    await reg._render(task, events.TurnComplete("prompt-1"))
    assert task.progress_stream_ts is None
    assert task.progress_fallback_ts is None
    assert any(edit["message_ts"] == fallback_ts and edit["blocks"] for edit in bot.edits)
    assert bot.deletions == [{"channel_id": task.channel_id, "message_ts": fallback_ts}]
    assert not any("Agent complete" in str(edit.get("text")) for edit in bot.edits)
    assert task.status_message_ts is None


@pytest.mark.asyncio
async def test_long_fallback_answer_is_recovered_as_one_normal_slack_message(in_memory_db):
    reg, bot = _registry(in_memory_db)
    task, _ = await _task(reg, root="1000.long-fallback")
    answer = "x" * 7000
    await reg._render(task, events.TurnStarted("prompt-long"))
    working_ts = task.progress_fallback_ts
    await reg._render(task, events.AssistantText(answer))
    await reg._render(task, events.TurnComplete("prompt-long"))

    assert bot.deletions == [{"channel_id": task.channel_id, "message_ts": working_ts}]
    recovered_answers = [post["text"] for post in bot.posts[1:]]
    assert recovered_answers == [answer]
    assert task.progress_started is False and task.progress_fallback_ts is None


@pytest.mark.asyncio
async def test_idle_background_notification_does_not_open_progress_stream(in_memory_db):
    bot = RichFakeBot()
    reg = TaskRegistry(in_memory_db, bot, FakeSupervisor())
    task, _ = await _task(reg, root="1000.idle-notification")
    await reg._render(task, events.AttentionPing("background job completed"))
    assert task.progress_started is False
    assert task.progress_keepalive is None
    assert bot.stream_starts == []
    assert len(bot.posts) == 1
    assert "<@UOWNER>" not in bot.posts[0]["text"]


@pytest.mark.asyncio
async def test_attention_notification_strips_yaml_summary_wrapper(in_memory_db):
    bot = RichFakeBot()
    reg = TaskRegistry(in_memory_db, bot, FakeSupervisor())
    task, _ = await _task(reg, root="1000.wrapped-notification")

    await reg._render(task, events.AttentionPing(
        "summary: |-\n  Re-reviewed PR #125.\n  Validation passed."
    ))

    assert len(bot.posts) == 1
    assert bot.posts[0]["text"] == "🔔 Re-reviewed PR #125.\nValidation passed."
    assert "summary:" not in bot.posts[0]["text"]


@pytest.mark.asyncio
async def test_resumed_activity_reconstructs_one_native_progress_surface(in_memory_db):
    bot = RichFakeBot()
    reg = TaskRegistry(in_memory_db, bot, FakeSupervisor())
    task, _ = await _task(reg, root="1000.resumed")
    assert task.progress_started is False

    await reg._render(task, events.AssistantText("review is still running"))
    await reg._render(task, events.ToolLine("✓ job_block"))
    await reg._render(task, events.AttentionPing("review completed"))

    assert len(bot.stream_starts) == 1
    assert bot.posts == []
    task_chunks = [call["chunks"][0] for call in bot.stream_appends if call.get("chunks")]
    assert not any(chunk.get("type") == "markdown_text" for chunk in task_chunks)
    assert any(chunk.get("id") == "activity" for chunk in task_chunks)
    assert any(chunk.get("id") == "background-job" for chunk in task_chunks)
    await reg._render(task, events.TurnComplete("resumed"))
    assert [post["text"] for post in bot.posts] == ["review is still running"]


@pytest.mark.asyncio
async def test_live_control_header_shows_title_context_and_todos(in_memory_db):
    bot = RichFakeBot()
    reg = TaskRegistry(in_memory_db, bot, FakeSupervisor())
    task, client = await _task(reg, root="1000.header")
    task.control_message_ts = "controls-1"
    await reg._persist_root(task)
    client.state_payload.update({
        "session_title": "Investigate Attie migration",
        "active_model": "anthropic/claude-opus-4-8",
        "active_reasoning_effort": "high",
        "active_facet": "orchestrate",
        "context_usage": {"used_tokens": 42100, "limit_tokens": 200000},
        "todos": [
            {"title": "Compare error windows", "status": "in_progress"},
            {"title": "Check deploy configuration", "status": "pending"},
        ],
    })
    await reg._refresh_task_header(task)
    edit = bot.edits[-1]
    assert edit["message_ts"] == "controls-1"
    rendered = str(edit["blocks"])
    assert "Investigate Attie migration" in rendered
    assert "42.1k / 200.0k (21.1%)" in rendered
    assert "Compare error windows" in rendered
    assert "Check deploy configuration" in rendered
    assert bot.renames == [{
        "channel_id": task.channel_id,
        "thread_ts": task.root_ts,
        "title": "Investigate Attie migration",
    }]
    # Periodic header refreshes do not spam the rename endpoint.
    await reg._refresh_task_header(task)
    assert len(bot.renames) == 1
    runtime = await state.get_runtime(in_memory_db, task.task_id)
    assert runtime is not None and runtime.control_message_ts == "controls-1"


@pytest.mark.asyncio
async def test_set_model_and_set_facet_refresh_the_visible_header_immediately(in_memory_db):
    """Regression: set_model/set_facet used to rely solely on the 60s
    periodic header refresh loop, so a successful model/facet switch left
    the visible task root message showing stale info for up to a minute
    (and indefinitely if the loop wasn't running), looking like the switch
    silently failed. They must now refresh the header inline, like
    set_title already does."""
    bot = FakeBot()
    reg = TaskRegistry(in_memory_db, bot, FakeSupervisor())
    task, client = await _task(reg, root="1000.refresh")
    task.control_message_ts = "controls-refresh"
    await reg._persist_root(task)

    client.state_payload.update({"active_model": "router/gpt-5.6-sol:api", "active_facet": "execute"})
    await reg.set_model(task.task_id, "router/gpt-5.6-sol:api", owner_user_id="UOWNER")
    assert bot.edits, "set_model must refresh the visible header, not just the daemon"
    assert "router/gpt-5.6-sol:api" in str(bot.edits[-1]["blocks"])

    client.state_payload.update({"active_facet": "plan"})
    await reg.set_facet(task.task_id, "plan", owner_user_id="UOWNER")
    assert "plan" in str(bot.edits[-1]["blocks"])


@pytest.mark.asyncio
async def test_duplicate_subagent_summary_notification_is_suppressed(in_memory_db):
    bot = RichFakeBot()
    reg = TaskRegistry(in_memory_db, bot, FakeSupervisor())
    task, _ = await _task(reg, root="1000.review-summary")
    summary = "summary: |-\n  ## Re-Review\n\n  No actionable findings remain."
    await reg._render(task, events.TurnStarted("prompt-review"))
    await reg._render(task, events.SubagentStarted("reviewer-1", "general-purpose", "model"))
    await reg._render(task, events.SubagentCompleted("reviewer-1", "success", None, summary))
    calls_before = len(bot.stream_appends)
    await reg._render(task, events.AttentionPing(summary))
    assert len(bot.stream_appends) == calls_before
    assert not any("🔔" in str(post.get("text", "")) for post in bot.posts)
    await reg._render(task, events.TurnComplete("prompt-review"))


@pytest.mark.asyncio
async def test_unscoped_notification_is_suppressed_during_subagent_turn(in_memory_db):
    bot = RichFakeBot()
    reg = TaskRegistry(in_memory_db, bot, FakeSupervisor())
    task, _ = await _task(reg, root="1000.subagent-notification")
    await reg._render(task, events.TurnStarted("prompt-review"))
    await reg._render(task, events.SubagentStarted("reviewer-1", "general-purpose", "model"))
    calls_before = len(bot.stream_appends)

    await reg._render(task, events.AttentionPing(
        "## Actionable finding\n\n**medium — listBlobs exposes storage generations**"
    ))

    assert len(bot.stream_appends) == calls_before
    assert not any("🔔" in str(post.get("text", "")) for post in bot.posts)
    await reg._render(task, events.SubagentCompleted("reviewer-1", "success", None, "review complete"))
    rich_completion = bot.stream_appends[-1]["chunks"][0]
    assert rich_completion["status"] == "complete"
    await reg._render(task, events.TurnComplete("prompt-review"))

    # Polytoken may queue a differently-worded notification after TurnComplete.
    # It is still the same subagent result and must not reappear as an idle bell.
    posts_before_late_notification = len(bot.posts)
    await reg._render(task, events.AttentionPing(
        "Local-only investigation findings (no files modified):\n\n## Direct serving path"
    ))
    assert len(bot.posts) == posts_before_late_notification

    task.last_subagent_event_at -= 61
    await reg._render(task, events.AttentionPing("unrelated later background job"))
    assert len(bot.posts) == posts_before_late_notification + 1


@pytest.mark.asyncio
async def test_background_job_notification_updates_timeline_without_owner_ping(in_memory_db):
    bot = RichFakeBot()
    reg = TaskRegistry(in_memory_db, bot, FakeSupervisor())
    task, _ = await _task(reg, root="1000.notification")
    await reg._render(task, events.TurnStarted("prompt-notification"))
    await reg._render(task, events.AttentionPing("job shell_exec:validate completed (exit 1)"))

    chunks = [call["chunks"][0] for call in bot.stream_appends if call.get("chunks")]
    assert any(chunk.get("id") == "background-job" for chunk in chunks)
    assert not any(f"<@{task.owner_user_id}>" in str(post.get("text")) for post in bot.posts)
    await reg._render(task, events.TurnComplete("prompt-notification"))


@pytest.mark.asyncio
async def test_native_progress_rotation_replaces_and_deletes_old_stream(in_memory_db):
    bot = RichFakeBot()
    reg = TaskRegistry(in_memory_db, bot, FakeSupervisor())
    task, _ = await _task(reg, root="1000.rotate")
    await reg._render(task, events.TurnStarted("prompt-rotate"))
    task.progress_lines.append("✓ Bash: checking services")
    await reg._render(task, events.AssistantText("partial answer"))

    assert await reg._rotate_progress_stream(task)
    assert task.progress_stream_ts == "stream-2"
    assert len(bot.stream_starts) == 2
    replacement = bot.stream_starts[-1]["chunks"]
    assert any(chunk.get("id") == "activity" for chunk in replacement)
    assert not any(chunk.get("type") == "markdown_text" for chunk in replacement)
    assert bot.posts == []
    assert bot.stream_stops == [{"channel_id": task.channel_id, "stream_ts": "stream-1"}]
    assert bot.deletions == [{"channel_id": task.channel_id, "message_ts": "stream-1"}]
    await reg._render(task, events.TurnComplete("prompt-rotate"))
    assert [post["text"] for post in bot.posts] == ["partial answer"]
    assert task.progress_keepalive is None


@pytest.mark.asyncio
async def test_rich_progress_stream_lifecycle_and_assistant_output_once(in_memory_db):
    rich_bot = RichFakeBot()
    reg = TaskRegistry(in_memory_db, rich_bot, FakeSupervisor())
    task, _ = await _task(reg, root="1000.rich")
    await reg._render(task, events.TurnStarted("prompt-rich"))
    assert task.progress_keepalive is not None and not task.progress_keepalive.done()
    await reg._render(task, events.AssistantThinking("private reasoning fragment"))
    await reg._render(task, events.ToolLine("✓ Bash: pwd"))
    await reg._render(task, events.ToolLine("✓ Read: pyproject.toml"))
    await reg._render(task, events.AssistantText("final answer"))
    await reg._render(task, events.SubagentStarted("h1", "research", "model"))
    await reg._render(task, events.SubagentActivity("h1", "reading"))
    await reg._render(task, events.SubagentCompleted("h1", "success", None, "done"))
    await reg._render(task, events.TurnComplete("prompt-rich"))

    assert len(rich_bot.stream_starts) == 1
    start = rich_bot.stream_starts[0]
    assert start["recipient_user_id"] == task.owner_user_id
    assert start["recipient_team_id"] == task.team_id
    assert start["task_display_mode"] == "timeline"
    # Native agent-session status supplies the working state and Stop button.
    assert start["chunks"] is None
    # Prose is isolated from task_update chunks in one ordinary message.
    assert [post["text"] for post in rich_bot.posts] == ["final answer"]
    assert not any(
        chunk.get("type") == "markdown_text"
        for call in rich_bot.stream_appends for chunk in (call.get("chunks") or [])
    )
    task_chunks = [call["chunks"][0] for call in rich_bot.stream_appends if call["chunks"]]
    activity_chunks = [chunk for chunk in task_chunks if chunk["type"] == "task_update" and chunk["id"] == "activity"]
    assert [chunk["title"] for chunk in activity_chunks] == ["Recent tool calls", "Recent tool calls"]
    assert activity_chunks[-1]["details"] == "✓ Bash: pwd\n✓ Read: pyproject.toml"
    assert all(chunk["status"] == "in_progress" for chunk in activity_chunks)
    assert "private reasoning fragment" not in "\n".join(task.progress_lines)
    assert not any("private reasoning fragment" in str(call) for call in rich_bot.stream_appends)
    assert any(chunk["type"] == "task_update" and chunk["id"] == "subagent-h1" for chunk in task_chunks)
    assert len(rich_bot.stream_stops) == 1
    assert rich_bot.stream_stops[0]["channel_id"] == task.channel_id
    assert rich_bot.stream_stops[0]["stream_ts"] == "stream-1"
    assert rich_bot.stream_stops[0]["blocks"] is None
    assert {deletion["message_ts"] for deletion in rich_bot.deletions} == {"stream-1"}
    assert rich_bot.statuses == [
        {"channel_id": task.channel_id, "thread_ts": task.root_ts, "status": "processing"},
        {"channel_id": task.channel_id, "thread_ts": task.root_ts, "status": "active"},
    ]
    assert sum(post.get("text") == "final answer" for post in rich_bot.posts) == 1
    assert task.progress_stream_ts is None and task.progress_started is False
    assert task.progress_keepalive is None


@pytest.mark.asyncio
async def test_subagent_activity_burst_is_coalesced_before_native_stream_exhaustion(in_memory_db):
    """A chatty subagent must not append one retained Slack chunk per event.

    Slack counts repeated task_update IDs toward its stream payload ceiling; the
    old behavior reliably degraded near 100 updates with msg_too_long.
    """
    rich_bot = RichFakeBot()
    reg = TaskRegistry(in_memory_db, rich_bot, FakeSupervisor())
    task, _ = await _task(reg, root="1000.subagent-burst")
    await reg._render(task, events.TurnStarted("prompt-subagent-burst"))
    await reg._render(task, events.SubagentStarted("h1", "general-purpose", "model"))
    appends_after_start = len(rich_bot.stream_appends)

    for index in range(100):
        await reg._render(task, events.SubagentActivity("h1", f"activity {index}"))

    assert len(rich_bot.stream_appends) == appends_after_start
    assert len(task.subagent_blocks["h1"].actions) == 100

    await reg._render(task, events.SubagentCompleted("h1", "success", None, "done"))
    assert len(rich_bot.stream_appends) == appends_after_start + 1
    assert rich_bot.stream_appends[-1]["chunks"][0]["status"] == "complete"
    await reg._render(task, events.TurnComplete("prompt-subagent-burst"))


@pytest.mark.asyncio
async def test_fallback_progress_remains_progress_only_while_answer_is_buffered(in_memory_db):
    task = Task(
        task_id="fallback-order", team_id="T1", channel_id="C1", cwd="/tmp",
        root_ts="1000.fallback-order", owner_user_id="UOWNER", mode="thread",
    )
    task.progress_lines.extend(["general-purpose started", "activity"])
    task.progress_answer = "I’m resuming the implementation."

    _, blocks = TaskRegistry._progress_blocks(task)

    assert [block["type"] for block in blocks] == ["section", "context"]
    assert task.progress_answer not in str(blocks)
    assert "2 updates" in blocks[1]["elements"][0]["text"]


@pytest.mark.asyncio
async def test_assistant_text_gets_paragraph_break_after_prior_activity(in_memory_db):
    """Regression: subagent/tool activity followed by assistant text (or a
    second text block) must not render glued onto the preceding content, e.g.
    "70 updatesGoal persistence..." or "worktree.The worktree...".
    """
    rich_bot = RichFakeBot()
    reg = TaskRegistry(in_memory_db, rich_bot, FakeSupervisor())
    task, _ = await _task(reg, root="1000.glue")
    await reg._render(task, events.TurnStarted("prompt-glue"))
    await reg._render(task, events.SubagentStarted("h1", "general-purpose", "model"))
    await reg._render(task, events.SubagentActivity("h1", "✗ Write: space/uri.go"))
    # First assistant text block after activity-only progress: must be
    # separated from the preceding activity, not appended raw.
    await reg._render(task, events.AssistantText("Goal persistence was not enabled."))
    # A second, independent text block (e.g. after further tool calls) must
    # also be separated from the first, not glued sentence-to-sentence.
    await reg._render(task, events.AssistantText("The worktree is ready."))
    await reg._render(task, events.TurnComplete("prompt-glue"))

    assert task.progress_answer == (
        "Goal persistence was not enabled.\n\nThe worktree is ready."
    )
    assert not any(
        chunk.get("type") == "markdown_text"
        for call in rich_bot.stream_appends for chunk in (call.get("chunks") or [])
    )
    assert [post["text"] for post in rich_bot.posts] == [task.progress_answer]
    assert not any(edit.get("message_ts") == task.progress_answer_ts for edit in rich_bot.edits)


@pytest.mark.asyncio
async def test_assistant_text_first_block_has_no_leading_separator(in_memory_db):
    """No prior activity this turn: the very first text block must not carry
    a leading blank paragraph."""
    rich_bot = RichFakeBot()
    reg = TaskRegistry(in_memory_db, rich_bot, FakeSupervisor())
    task, _ = await _task(reg, root="1000.no-glue")
    await reg._render(task, events.TurnStarted("prompt-no-glue"))
    await reg._render(task, events.AssistantText("Hello there."))
    await reg._render(task, events.TurnComplete("prompt-no-glue"))

    assert task.progress_answer == "Hello there."


@pytest.mark.asyncio
async def test_completed_answer_uses_standard_markdown_block(in_memory_db):
    bot = RichFakeBot()
    reg = TaskRegistry(in_memory_db, bot, FakeSupervisor())
    task, _ = await _task(reg, root="1000.markdown")
    answer = "## Schema compatibility\n\n**Yes**, with one qualification."

    await reg._render(task, events.TurnStarted("prompt-markdown"))
    await reg._render(task, events.AssistantText(answer))
    assert bot.posts == []
    await reg._render(task, events.TurnComplete("prompt-markdown"))

    assert bot.posts[-1]["blocks"] == [{"type": "markdown", "text": answer}]
    assert bot.posts[-1]["fallback_text"] == answer


@pytest.mark.asyncio
async def test_oversized_markdown_answer_uses_single_mrkdwn_message(in_memory_db):
    bot = RichFakeBot()
    reg = TaskRegistry(in_memory_db, bot, FakeSupervisor())
    task, _ = await _task(reg, root="1000.large-markdown")
    answer = "## Heading\n\n**bold**\n\n" + ("x" * 12_050)

    await reg._render(task, events.TurnStarted("prompt-large-markdown"))
    await reg._render(task, events.AssistantText(answer))
    await reg._render(task, events.TurnComplete("prompt-large-markdown"))

    assert len(bot.posts) == 1
    assert bot.posts[0]["blocks"] is None
    assert bot.posts[0]["text"].startswith("*Heading*\n\n*bold*")


@pytest.mark.asyncio
async def test_stream_append_failure_clears_stream_ts_avoiding_redundant_stop(in_memory_db):
    """Regression: a failed append_stream (e.g. msg_too_long on a long,
    chatty turn) previously left task.progress_stream_ts pointing at the
    now-broken message. _end_turn would then attempt a second, redundant
    stop_stream against it, producing an unrelated-looking
    "message_not_in_streaming_state" warning at turn end. The stream ts must
    be cleared as soon as the append fails."""
    class AppendFailsBot(RichFakeBot):
        async def append_stream(self, channel_id, stream_ts, *, markdown_text=None, chunks=None):
            self.stream_appends.append({
                "channel_id": channel_id, "stream_ts": stream_ts,
                "markdown_text": markdown_text, "chunks": chunks,
            })
            raise RuntimeError("msg_too_long")

    rich_bot = AppendFailsBot()
    reg = TaskRegistry(in_memory_db, rich_bot, FakeSupervisor())
    task, _ = await _task(reg, root="1000.append-fail")
    await reg._render(task, events.TurnStarted("prompt-fail"))
    assert task.progress_stream_ts is not None

    await reg._render(task, events.ToolLine("✓ Bash: pwd"))
    assert task.progress_stream_disabled is True
    assert task.progress_stream_ts is None
    assert len(rich_bot.stream_stops) == 1

    await reg._render(task, events.TurnComplete("prompt-fail"))
    # No second, redundant stop_stream against the already-broken message.
    assert len(rich_bot.stream_stops) == 1


@pytest.mark.asyncio
async def test_fallback_progress_update_failure_is_logged_not_silent(in_memory_db, caplog):
    """Regression: a persistently failing fallback-card edit was previously
    swallowed by a bare contextlib.suppress with zero log output, so the
    Slack message could freeze mid-turn with no operator-visible signal at
    all. It must now log a warning and, critically, must not raise."""
    class AlwaysFailsBot(RichFakeBot):
        async def append_stream(self, channel_id, stream_ts, *, markdown_text=None, chunks=None):
            raise RuntimeError("msg_too_long")

        async def edit_message(self, channel_id, message_ts, *, text=None, blocks=None, content=None):
            raise RuntimeError("some_persistent_slack_error")

    rich_bot = AlwaysFailsBot()
    reg = TaskRegistry(in_memory_db, rich_bot, FakeSupervisor())
    task, _ = await _task(reg, root="1000.fallback-fail")
    await reg._render(task, events.TurnStarted("prompt-ff"))

    with caplog.at_level("WARNING", logger="bridge.tasks"):
        # First activity: append_stream fails -> stream_ts is cleared (see
        # the previous regression fix) -> falls back to a fresh _post (which
        # succeeds here, establishing progress_fallback_ts).
        await reg._render(task, events.ToolLine("✓ Bash: pwd"))
        # Second activity: now routes through _update_fallback_progress's
        # edit_message call, which always fails in this bot -- this must be
        # logged, not silently swallowed.
        await reg._render(task, events.ToolLine("✓ Bash: ls"))

    assert any("fallback progress card update failed" in rec.message for rec in caplog.records)
    # Must not have raised out of _render at any point.
    await reg._render(task, events.TurnComplete("prompt-ff"))


@pytest.mark.asyncio
async def test_native_agent_stop_cancels_turn_without_terminating_session(in_memory_db):
    rich_bot = RichFakeBot()
    reg = TaskRegistry(in_memory_db, rich_bot, FakeSupervisor())
    task, client = await _task(reg, root="1000.native-stop")
    client.state_payload["turn_in_flight"] = True
    await reg._render(task, events.TurnStarted("prompt-stop"))

    handled = await reg.handle_agent_session_stopped({
        "team_id": task.team_id,
        "channel_id": task.channel_id,
        "root_ts": task.root_ts,
        "actor_id": task.owner_user_id,
    })

    assert handled is True
    assert client.cancelled == 1
    assert client.terminated == 0
    assert task.status == "running"
    assert task.native_stop_pending is True
    assert rich_bot.statuses[-1]["status"] == "active"
    assert any("session remains ready" in post.get("text", "") for post in rich_bot.posts)


@pytest.mark.asyncio
async def test_interrogative_suspends_agent_session_and_answer_resumes_processing(in_memory_db):
    rich_bot = RichFakeBot()
    reg = TaskRegistry(in_memory_db, rich_bot, FakeSupervisor())
    task, _ = await _task(reg, root="1000.suspended")
    action = events.Confirmation("iq-status", "prompt-status", "Continue?")
    await reg._render(task, action)
    assert rich_bot.statuses[-1]["status"] == "suspended"
    pending = await reg._pending_for(task, task.owner_user_id)
    assert pending is not None
    await reg._answer_interrogative(task, pending, "yes")
    assert rich_bot.statuses[-1]["status"] == "processing"


@pytest.mark.asyncio
async def test_plan_handoff_renders_buttons_and_answers_map_to_daemon_decisions(in_memory_db):
    """Regression: plan_handoff interrogatives previously fell through to a
    plain-text StatusNote with no way to respond. They must now render real
    Slack buttons (using the daemon's own action_labels copy) and accept
    both button clicks and free-text thread replies, mapped to the exact
    PlanHandoffDecision shape the daemon's OpenAPI schema requires."""
    reg, bot = _registry(in_memory_db)
    task, client = await _task(reg, root="1000.plan-handoff")
    action = events.PlanHandoff(
        interrogative_id="ph-1", prompt_id="prompt-1",
        plan_text="### Goal\nDo the thing.",
        plan_path="/sessions/s/plan-001.md", display_path="/sessions/s/plan-001.md",
        target_facet="execute", title="Approve plan?",
        action_labels={
            "implement_new_context": "Implement plan with a new context",
            "implement_current_context": "Implement plan within current context",
            "cancel": "Refuse (Tab to add feedback)",
        },
    )
    await reg._render(task, action)

    post = bot.posts[-1]
    assert post["blocks"] is not None
    button_texts = [
        el["text"]["text"]
        for block in post["blocks"] if block.get("type") == "actions"
        for el in block["elements"]
    ]
    assert "Implement plan with a new context" in button_texts
    assert "Implement plan within current context" in button_texts
    assert "Reject" in button_texts
    action_ids = {el["action_id"] for block in post["blocks"] if block.get("type") == "actions" for el in block["elements"]}
    assert action_ids == {"interrogative.plan_handoff"}
    assert "Do the thing." in str(post["blocks"])

    pending = await reg._pending_for(task, task.owner_user_id)
    assert pending is not None and pending.payload["kind"] == "plan_handoff"

    # A button click's value decodes straight to the bare decision string.
    await reg._answer_interrogative(task, pending, "implement_new_context")
    assert client.interrogative_responses[-1] == {
        "id": "ph-1", "response": {"kind": "plan_handoff_answer", "decision": "implement_new_context"},
    }


@pytest.mark.asyncio
async def test_plan_handoff_free_text_reply_becomes_refuse_with_feedback(in_memory_db):
    reg, _ = _registry(in_memory_db)
    task, client = await _task(reg, root="1000.plan-handoff-refuse")
    action = events.PlanHandoff(
        interrogative_id="ph-2", prompt_id=None, plan_text="plan",
        plan_path="p", display_path="p", target_facet="execute",
        title="Approve plan?", action_labels={},
    )
    await reg._render(task, action)
    pending = await reg._pending_for(task, task.owner_user_id)
    await reg._answer_interrogative(task, pending, "please add a test phase first")
    assert client.interrogative_responses[-1]["response"] == {
        "kind": "plan_handoff_answer",
        "decision": {"refuse": {"feedback": "please add a test phase first"}},
    }


@pytest.mark.asyncio
async def test_goal_proposal_renders_accept_reject_buttons_and_answers(in_memory_db):
    """Regression: goal_proposal interrogatives (from the propose_goal tool)
    previously fell through to a plain-text StatusNote with no way to
    respond -- verified live: an "Accept this goal?" message with no
    buttons and no tracked pending interrogative at all."""
    reg, bot = _registry(in_memory_db)
    task, client = await _task(reg, root="1000.goal-proposal")
    action = events.GoalProposal(
        interrogative_id="gp-1", prompt_id="prompt-1",
        title="Accept this goal?",
        proposed_summary="Throwaway test goal for interrogative schema verification",
        proposed_file_path=None,
        action_labels={"accept": "Accept goal", "reject": "Reject goal"},
    )
    await reg._render(task, action)

    post = bot.posts[-1]
    assert post["blocks"] is not None
    buttons = [el for block in post["blocks"] if block.get("type") == "actions" for el in block["elements"]]
    assert {b["text"]["text"] for b in buttons} == {"Accept goal", "Reject goal"}
    assert {b["action_id"] for b in buttons} == {"interrogative.goal_proposal"}
    assert "Throwaway test goal" in str(post["blocks"])

    pending = await reg._pending_for(task, task.owner_user_id)
    assert pending is not None and pending.payload["kind"] == "goal_proposal"

    await reg._answer_interrogative(task, pending, "accept")
    assert client.interrogative_responses[-1] == {
        "id": "gp-1", "response": {"kind": "goal_proposal_answer", "accepted": True},
    }


@pytest.mark.asyncio
async def test_goal_proposal_blocks_post_failure_falls_back_to_plain_text(in_memory_db):
    """If Slack rejects the Block Kit post (invalid_blocks, or any other
    failure), the pending interrogative is already durably recorded -- so the
    user must not be left with an invisible question. The fallback must carry
    the real content (title + summary), not the block-only "see below" stub."""
    class FailingBlocksBot(FakeBot):
        async def post(self, text: str, channel_id: str, root_ts=None, blocks=None):
            if blocks is not None:
                raise RuntimeError("invalid_blocks")
            return await super().post(text, channel_id, root_ts=root_ts, blocks=blocks)

    bot = FailingBlocksBot()
    reg = TaskRegistry(in_memory_db, bot, FakeSupervisor())
    task, _ = await _task(reg, root="1000.goal-proposal-fallback")
    action = events.GoalProposal(
        interrogative_id="gp-2", prompt_id="prompt-2",
        title="Accept this goal?",
        proposed_summary="Trace agentgateway auth paths and add the narrowest access-control change",
        proposed_file_path=None,
        action_labels={"accept": "Accept goal", "reject": "Reject goal"},
    )
    await reg._render(task, action)

    assert not any(p.get("blocks") for p in bot.posts)
    post = bot.posts[-1]
    assert "Accept this goal?" in post["text"]
    assert "Trace agentgateway auth paths" in post["text"]
    assert "yes/accept" in post["text"]

    # The pending interrogative was still recorded, so a plain-text reply works.
    pending = await reg._pending_for(task, task.owner_user_id)
    assert pending is not None and pending.payload["kind"] == "goal_proposal"


@pytest.mark.asyncio
async def test_goal_proposal_answer_with_bot_mention_is_not_misread_as_reject(in_memory_db):
    """Regression: live report -- user replied "yes @Hailey's Robot" to an
    "Accept this goal?" prompt (mentioning the bot is how replies route in
    most threads) and the daemon recorded it as rejected. Root cause:
    maybe_route_message's raw_text (fed to _answer_interrogative) used
    msg.text unconditionally, so the literal <@BOT_ID> mention token rode
    along into _goal_proposal_response's exact-match check, which silently
    defaulted to accepted=False for anything that wasn't a bare "yes"."""
    reg, bot = _registry(in_memory_db)
    task, client = await _task(reg, root="1000.goal-mention")
    action = events.GoalProposal(
        interrogative_id="gp-mention", prompt_id="prompt-mention",
        title="Accept this goal?",
        proposed_summary="Finish implementing Atproto Spaces alpha support",
        proposed_file_path=None,
        action_labels={"accept": "Accept goal", "reject": "Reject goal"},
    )
    await reg._render(task, action)

    assert await reg.maybe_route_message(SlackMessage(
        "T1", "CHOME", task.root_ts, SlackActor("UOWNER"),
        "yes <@UBRIDGE>", "E1", "M1",
    ))

    assert client.interrogative_responses[-1] == {
        "id": "gp-mention", "response": {"kind": "goal_proposal_answer", "accepted": True},
    }


def test_goal_proposal_blocks_truncates_long_title_to_avoid_invalid_blocks() -> None:
    """Regression: live failure `interrogative post with blocks failed
    (invalid_blocks)`. `title` can fall back to the daemon's raw
    natural-language `question` (events.py truncates only to 4000 chars).
    Embedded unbounded into a single section's mrkdwn text, that can exceed
    Slack's 3000-char section text limit and get the whole message rejected."""
    task = Task("t1", "T1", "CHOME", "1000.long", "UOWNER", "personal", "/tmp", "running", "sess-1", 41001, 1, 1)
    long_title = "x" * 4000
    action = events.GoalProposal(
        interrogative_id="gp-long", prompt_id="p-1", title=long_title,
        proposed_summary="short summary", proposed_file_path=None, action_labels={},
    )
    _, blocks = TaskRegistry._goal_proposal_blocks(task, "UOWNER", "gp-long", action)
    header = blocks[0]["text"]["text"]
    assert len(header) < 3000
    assert header.count("x") <= 400


def test_plan_handoff_blocks_truncates_long_title_to_avoid_invalid_blocks() -> None:
    task = Task("t1", "T1", "CHOME", "1000.long", "UOWNER", "personal", "/tmp", "running", "sess-1", 41001, 1, 1)
    long_title = "y" * 4000
    action = events.PlanHandoff(
        interrogative_id="ph-long", prompt_id="p-1", plan_text="short plan",
        plan_path="", display_path="z" * 4000, target_facet="plan",
        title=long_title, action_labels={},
    )
    _, blocks = TaskRegistry._plan_handoff_blocks(task, "UOWNER", "ph-long", action)
    header = blocks[0]["text"]["text"]
    assert len(header) < 3000
    display_path_block = next(b for b in blocks if b.get("type") == "context")
    assert len(display_path_block["elements"][0]["text"]) < 3000


def test_goal_proposal_response_maps_free_text_to_accepted_bool() -> None:
    resp = TaskRegistry._goal_proposal_response
    assert resp("accept")["accepted"] is True
    assert resp("yes")["accepted"] is True
    assert resp("reject")["accepted"] is False
    assert resp("anything else")["accepted"] is False
    assert resp("accept") == {"kind": "goal_proposal_answer", "accepted": True}


def test_plan_handoff_response_aliases_map_to_daemon_decisions() -> None:
    resp = TaskRegistry._plan_handoff_response
    assert resp("implement_new_context") == {"kind": "plan_handoff_answer", "decision": "implement_new_context"}
    assert resp("1") == {"kind": "plan_handoff_answer", "decision": "implement_new_context"}
    assert resp("new context") == {"kind": "plan_handoff_answer", "decision": "implement_new_context"}
    assert resp("2") == {"kind": "plan_handoff_answer", "decision": "implement_current_context"}
    assert resp("continue") == {"kind": "plan_handoff_answer", "decision": "implement_current_context"}
    assert resp("3") == {"kind": "plan_handoff_answer", "decision": "cancel"}
    assert resp("no") == {"kind": "plan_handoff_answer", "decision": "cancel"}
    assert resp("reject") == {"kind": "plan_handoff_answer", "decision": "cancel"}
    assert resp("please rescope AC.2") == {
        "kind": "plan_handoff_answer", "decision": {"refuse": {"feedback": "please rescope AC.2"}},
    }


@pytest.mark.asyncio
async def test_tool_activity_keeps_only_five_most_recent_calls(in_memory_db):
    rich_bot = RichFakeBot()
    reg = TaskRegistry(in_memory_db, rich_bot, FakeSupervisor())
    task, _ = await _task(reg, root="1000.tools-five")
    await reg._render(task, events.TurnStarted("prompt-tools"))
    for index in range(7):
        await reg._render(task, events.ToolLine(f"tool call {index}"))
    assert task.recent_tool_lines == [f"tool call {index}" for index in range(2, 7)]
    chunks = [call["chunks"][0] for call in rich_bot.stream_appends if call.get("chunks")]
    activity = [chunk for chunk in chunks if chunk.get("id") == "activity"][-1]
    assert activity["details"] == "\n".join(f"tool call {index}" for index in range(2, 7))
    await reg._render(task, events.TurnComplete("prompt-tools"))


@pytest.mark.asyncio
async def test_stream_append_failure_stops_before_in_place_fallback(in_memory_db):
    class FailingAppendBot(RichFakeBot):
        async def append_stream(self, channel_id, stream_ts, *, markdown_text=None, chunks=None):
            raise RuntimeError("invalid_arguments")

    bot = FailingAppendBot()
    reg = TaskRegistry(in_memory_db, bot, FakeSupervisor())
    task, _ = await _task(reg, root="1000.degrade")
    await reg._render(task, events.TurnStarted("prompt-degrade"))
    await reg._render(task, events.ToolLine("✓ Bash: trigger degraded stream"))
    await reg._render(task, events.AssistantText("answer after degradation"))

    assert bot.stream_stops == [{"channel_id": task.channel_id, "stream_ts": "stream-1"}]
    assert task.progress_stream_ts is None
    assert task.progress_fallback_ts == "stream-1"
    assert any(edit["message_ts"] == "stream-1" and edit["blocks"] for edit in bot.edits)
    assert bot.posts == []
    assert not any("answer after degradation" in str(edit.get("text")) for edit in bot.edits)

    await reg._render(task, events.TurnComplete("prompt-degrade"))
    assert bot.deletions == [{"channel_id": task.channel_id, "message_ts": "stream-1"}]
    assert [post["text"] for post in bot.posts] == ["answer after degradation"]
    assert task.progress_stream_disabled is False
    await reg._render(task, events.TurnStarted("prompt-retry"))
    assert len(bot.stream_starts) == 2
    assert task.progress_stream_ts == "stream-2"


class TestAC5AppBudget:
    async def test_collaborative_app_exchange_budget_pauses_and_alerts_owner(self, in_memory_db):
        reg, bot = _registry(in_memory_db, budget=1)
        task, client = await _task(reg, mode="collaborative", root="1000.007")
        await state.upsert_participant(in_memory_db, task.key, Participant("AHELPER", ParticipantKind.APP))
        app = lambda text, event, message: SlackMessage("T1", "CHOME", task.root_ts, SlackActor("AHELPER", is_app=True), text, event, message)
        assert await reg.maybe_route_message(app("first", "E1", "M1"))
        assert await reg.maybe_route_message(app("second", "E2", "M2"))
        assert task.status == "paused"
        assert task.app_exchanges == 1
        assert any("budget" in str(post).lower() and "UOWNER" in str(post) for post in bot.posts)
        assert json.loads(client.prompts[0])["body"] == "first"


class TestAC6PromotionAndMembership:
    async def test_promote_is_create_invite_root_then_atomic_swap(self, in_memory_db):
        reg, bot = _registry(in_memory_db)
        task, _ = await _task(reg, root="1000.008")
        task.mention_required = True
        task.control_message_ts = "old-controls"
        await reg._persist_root(task)
        promoted = await reg.promote_task(task.task_id, "UOWNER", ["UOTHER"], name="collab")
        assert promoted.mode == "collaborative"
        assert promoted.channel_id == "GNEW"
        assert promoted.root_ts == "1000.001"
        assert reg.get_by_key(ConversationKey("T1", "CHOME", "1000.008")) is None
        assert bot.private_channels == ["collab"]
        assert bot.invites[-1] == {"channel_id": "GNEW", "actor_ids": ["UOTHER"]}
        assert (await state.get_root(in_memory_db, promoted.key)).owner.mode == "collaborative"
        assert (await state.get_active_promotion(in_memory_db, ConversationKey("T1", "CHOME", "1000.008"))) is not None
        runtime = await state.get_runtime(in_memory_db, promoted.task_id)
        assert runtime is not None
        assert runtime.control_message_ts == promoted.root_ts
        assert runtime.mention_required is False

    async def test_default_promotion_preserves_participant_objects_and_routes_app_message(self, in_memory_db):
        reg, bot = _registry(in_memory_db)
        task, client = await _task(reg, root="1000.008b")
        await state.upsert_participant(in_memory_db, task.key, Participant("BAPP", ParticipantKind.APP, "Helper App"))
        await state.upsert_participant(in_memory_db, task.key, Participant("UOTHER", ParticipantKind.HUMAN, "Other User"))

        promoted = await reg.promote_task(task.task_id, "UOWNER", name="collab")
        rows = await state.list_participants(in_memory_db, promoted.key)
        saved = {str(row.participant.actor_id): row.participant for row in rows}
        assert saved == {
            "BAPP": Participant("BAPP", ParticipantKind.APP, "Helper App"),
            "UOTHER": Participant("UOTHER", ParticipantKind.HUMAN, "Other User"),
        }
        assert bot.invites[-1]["actor_ids"] == ["BAPP", "UOTHER"]
        assert await reg.maybe_route_message(SlackMessage(
            "T1", promoted.channel_id, promoted.root_ts,
            SlackActor("BAPP", is_app=True, display_name="Helper App"),
            "post-promotion app message", "E-app", "M-app",
        ))
        assert json.loads(client.prompts[-1])["body"] == "post-promotion app message"

    async def test_explicit_b_id_promotion_participant_is_app(self, in_memory_db):
        reg, _ = _registry(in_memory_db)
        task, _ = await _task(reg, root="1000.008c")
        promoted = await reg.promote_task(task.task_id, "UOWNER", ["BAPP"], name="explicit")
        rows = await state.list_participants(in_memory_db, promoted.key)
        assert rows[0].participant == Participant("BAPP", ParticipantKind.APP)

    async def test_promotion_db_failure_restores_mention_gate_and_binding(self, in_memory_db, monkeypatch):
        reg, _ = _registry(in_memory_db)
        task, _ = await _task(reg, root="1000.rollback")
        task.mention_required = True
        task.binding_id = "old-binding"
        await reg._persist_root(task)

        async def fail_replace(*args, **kwargs):
            raise RuntimeError("injected rebind failure")

        monkeypatch.setattr("bridge.tasks.replace_runtime_binding", fail_replace)
        with pytest.raises(TaskRoutingError, match="promotion failed"):
            await reg.promote_task(task.task_id, "UOWNER")
        assert task.key == ConversationKey("T1", "CHOME", "1000.rollback")
        assert task.mention_required is True
        assert task.binding_id == "old-binding"
        runtime = await state.get_runtime(in_memory_db, task.task_id)
        assert runtime is not None and runtime.mention_required is True
        assert runtime.binding_id == "old-binding"

    async def test_promotion_refuses_to_orphan_pending_interrogative(self, in_memory_db):
        reg, bot = _registry(in_memory_db)
        task, _ = await _task(reg, root="1000.pending-promotion")
        await reg._render(task, events.Confirmation("I-promote", None, "Continue?"))
        with pytest.raises(TaskRoutingError, match="pending agent question"):
            await reg.promote_task(task.task_id, "UOWNER")
        assert task.key == ConversationKey("T1", "CHOME", "1000.pending-promotion")
        assert bot.private_channels == []

    async def test_promotion_failure_cleans_new_channel_and_keeps_old_root(self, in_memory_db):
        reg, bot = _registry(in_memory_db)
        task, _ = await _task(reg, root="1000.009")
        bot.fail_root = True
        with pytest.raises(TaskRoutingError):
            await reg.promote_task(task.task_id, "UOWNER", [])
        assert reg.get_by_key(task.key) is task
        assert bot.archives == ["GNEW"]
        assert (await state.get_root(in_memory_db, task.key)).owner.mode == "personal"

    async def test_owner_can_remove_participant_only_after_slack_kick(self, in_memory_db):
        reg, bot = _registry(in_memory_db)
        task, _ = await _task(reg, mode="collaborative", root="1000.010b")
        await state.upsert_participant(in_memory_db, task.key, Participant("UOTHER", ParticipantKind.HUMAN))
        assert await reg.remove_participant(task.task_id, "UOWNER", "UOTHER")
        assert bot.kicks == [{"channel_id": "CHOME", "user_id": "UOTHER"}]
        assert await state.list_participants(in_memory_db, task.key) == []
        with pytest.raises(TaskPrivilegeError):
            await reg.remove_participant(task.task_id, "UOWNER", "UOWNER")

    async def test_participant_persists_only_after_slack_invite_success(self, in_memory_db):
        reg, bot = _registry(in_memory_db)
        task, _ = await _task(reg, mode="collaborative", root="1000.010")
        bot.fail_invite = True
        with pytest.raises(RuntimeError):
            await reg.add_participant(task.task_id, "UOWNER", "UOTHER")
        assert await state.list_participants(in_memory_db, task.key) == []
        bot.fail_invite = False
        await reg.add_participant(task.task_id, "UOWNER", "UOTHER")
        assert [str(row.participant.actor_id) for row in await state.list_participants(in_memory_db, task.key)] == ["UOTHER"]


class TestAC7LifecycleAndConfig:
    async def test_owner_required_and_personal_close_never_archives_home(self, in_memory_db):
        reg, bot = _registry(in_memory_db)
        task, client = await _task(reg, root="1000.011")
        with pytest.raises(TaskPrivilegeError):
            await reg.stop_task(task.task_id, "UOTHER")
        assert await reg.stop_task(task.task_id, "UOWNER")
        assert task.status == "stopped" and client.terminated == 1
        assert bot.archives == []

    async def test_collaborative_close_archives_private_channel_and_config_is_owner_only(self, in_memory_db):
        reg, bot = _registry(in_memory_db)
        task, _ = await _task(reg, mode="collaborative", channel="GPRIVATE", root="1000.012")
        task.channel_owned = True
        await state.update_runtime(in_memory_db, task.task_id, channel_owned=True)
        with pytest.raises(TaskPrivilegeError):
            await reg.set_facet(task.task_id, "plan", owner_user_id="UOTHER")
        await reg.set_facet(task.task_id, "plan", owner_user_id="UOWNER")
        await reg.close_task(task.task_id, "UOWNER")
        assert bot.archives == ["GPRIVATE"]

    async def test_compaction_lifecycle_is_visible_and_labels_automatic_runs(self, in_memory_db):
        rich_bot = RichFakeBot()
        reg = TaskRegistry(in_memory_db, rich_bot, FakeSupervisor())
        task, _ = await _task(reg, root="1000.compaction-events")
        await reg._render(task, events.CompactionUpdate("started", "threshold"))
        await reg._render(task, events.CompactionUpdate("retry", "threshold", "attempt 2/3"))
        await reg._render(task, events.CompactionUpdate("complete", "threshold"))
        chunks = [call["chunks"][0] for call in rich_bot.stream_appends if call.get("chunks")]
        updates = [chunk for chunk in chunks if chunk.get("id") == "compaction"]
        assert [chunk["title"] for chunk in updates] == [
            "Automatic compaction started", "Automatic compaction retrying", "Automatic compaction completed",
        ]
        assert updates[1]["details"] == "attempt 2/3"
        assert task.progress_started is False

    async def test_clear_context_rejects_active_turn_and_clears_idle_context(self, in_memory_db):
        reg, _ = _registry(in_memory_db)
        task, client = await _task(reg, root="1000.clear")
        client.state_payload["turn_in_flight"] = True
        with pytest.raises(TaskSpawnError, match="active"):
            await reg.clear_context(task.task_id, owner_user_id="UOWNER")
        assert client.clear_calls == 0
        client.state_payload["turn_in_flight"] = False
        await reg.clear_context(task.task_id, owner_user_id="UOWNER")
        assert client.clear_calls == 1

    async def test_compaction_runs_immediately_when_idle_and_queues_during_turn(self, in_memory_db):
        reg, bot = _registry(in_memory_db)
        task, client = await _task(reg, root="1000.compact")
        client.state_payload["turn_in_flight"] = False
        assert await reg.request_compaction(task.task_id, owner_user_id="UOWNER") == "accepted"
        assert client.compact_calls == 1
        assert task.compaction_id == "compact-1"
        # An untagged/different automatic terminal must not complete the manual
        # operation. Only the matching SSE operation ID is authoritative.
        await reg._render(task, events.CompactionUpdate("complete", "threshold"))
        assert task.compaction_id == "compact-1"
        await reg._render(task, events.CompactionUpdate("complete", "manual", compaction_id="compact-1"))
        assert task.compaction_id is None

        client.state_payload["turn_in_flight"] = True
        assert await reg.request_compaction(task.task_id, owner_user_id="UOWNER") == "queued"
        assert await reg.request_compaction(task.task_id, owner_user_id="UOWNER") == "queued"
        assert task.compaction_pending is True
        assert client.compact_calls == 1
        worker = reg._compaction_workers[task.task_id]

        client.state_payload["turn_in_flight"] = False
        # message_complete may arrive before the daemon releases its turn slot.
        # The queued request must wait for authoritative idle state.
        client.state_sequence = [
            {**client.state_payload, "turn_in_flight": True},
            {**client.state_payload, "turn_in_flight": True},
            {**client.state_payload, "turn_in_flight": False},
        ]
        await reg._render(task, events.TurnComplete("prompt-compact"))
        await worker
        assert task.compaction_pending is False
        assert task.compaction_id == "compact-2"
        assert client.compact_calls == 2
        assert sum("Queued compaction accepted" in post.get("text", "") for post in bot.posts) == 1
        assert not any("Queued compaction completed" in post.get("text", "") for post in bot.posts)
        await reg._render(task, events.CompactionUpdate("complete", "manual", compaction_id="compact-2"))
        assert task.compaction_id is None

    async def test_model_effort_skill_and_restart_contract(self, in_memory_db):
        reg, _ = _registry(in_memory_db)
        task, client = await _task(reg, root="1000.013")
        result = await reg.reload_daemon(task.task_id, owner_user_id="UOWNER")
        assert result == {"reloaded": ["models"], "failed": []}
        assert client.reload_calls == 1
        with pytest.raises(TaskPrivilegeError):
            await reg.reload_daemon(task.task_id, owner_user_id="UOTHER")
        await reg.set_effort(task.task_id, "low", owner_user_id="UOWNER")
        await reg.set_model(task.task_id, "openai/gpt-5", owner_user_id="UOWNER", reasoning_effort="high")
        await reg.invoke_skill(task.task_id, "brainstorming", "go", owner_user_id="UOWNER")
        assert client.model_calls == [
            {"model": "anthropic/claude-opus-4-8", "reasoning_effort": "low"},
            {"model": "openai/gpt-5", "reasoning_effort": "high"},
        ]
        assert json.loads(client.prompts[0])["body"] == "@brainstorming go"
        with pytest.raises(TaskRestartError):
            await reg.restart_task(task.task_id, "UOWNER")


class TestAC8DaemonRendering:
    async def test_render_text_tool_summary_and_subagent_blocks(self, in_memory_db):
        reg, bot = _registry(in_memory_db)
        task, _ = await _task(reg, root="1000.014")
        await reg._render(task, events.AssistantText("done"))
        await reg._render(task, events.ToolLine("✓ Bash: ls"))
        await reg._end_turn(task)
        await reg._render(task, events.SubagentStarted("h1", "researcher", "model"))
        await reg._render(task, events.SubagentActivity("h1", "reading"))
        await reg._render(task, events.SubagentCompleted("h1", "success", None, "ok"))
        assert task.subagent_blocks["h1"].result_summary == "ok"
        assert any("done" in post.get("text", "") for post in bot.posts)
        assert any("Bash: ls" in edit.get("text", "") for edit in bot.edits)
        assert any(post.get("blocks") for post in bot.posts)
        assert bot.edits and bot.edits[-1]["blocks"]

    async def test_sse_translator_action_routes_subagent_by_handle(self, in_memory_db):
        reg, bot = _registry(in_memory_db)
        task, _ = await _task(reg, root="1000.015")
        await reg._render(task, events.SubagentStarted("h1", "research", "m"))
        await reg._render(task, events.AssistantText("subagent result", subagent_handle="h1"))
        assert task.subagent_blocks["h1"].actions == ["• 💬 subagent result"]
        # Live block updates are throttled; completion forces the edit.
        await reg._render(task, events.SubagentCompleted("h1", "success", None, "done"))
        assert bot.edits


class TestAC10DedupAndDaemonErrors:
    async def test_terminate_rejection_leaves_task_live_and_unknown_raises(self, in_memory_db):
        reg, _ = _registry(in_memory_db)
        task, client = await _task(reg, root="1000.016")
        client.terminate_error_status = 500
        assert await reg.stop_task(task.task_id, "UOWNER") is False
        assert task.status == "running"
        with pytest.raises(TaskNotFound):
            await reg.kill_task("missing", "UOWNER")

    async def test_spawn_requires_identity_and_marks_failed_root(self, in_memory_db, tmp_path):
        reg, bot = _registry(in_memory_db)
        # The Bot supplies configured identity when values are omitted; explicit
        # empty strings are intentionally normalized to those configured values.
        reg._supervisor.fail_spawn = True
        with pytest.raises(TaskSpawnError):
            await reg.spawn_task(str(tmp_path), team_id="T1", channel_id="CHOME", owner_user_id="UOWNER")
        assert any("failed to spawn" in post.get("text", "") for post in bot.posts)


@pytest.mark.asyncio
async def test_start_agent_here_reuses_terminal_binding_without_unique_conflict(in_memory_db, tmp_path):
    reg, _ = _registry(in_memory_db)
    original, _ = await _task(reg, root="1000.terminal")
    original.status = "stopped"
    await reg._persist_root(original)

    restarted = await reg.spawn_task(
        str(tmp_path), team_id="T1", channel_id="CHOME", owner_user_id="UOWNER",
        root_ts=original.root_ts, bind_existing_root=True,
    )
    assert restarted.task_id == original.task_id
    assert restarted.status == "running"
    assert restarted.polytoken_session_id == "sess-1"
    runtime = await state.get_runtime(in_memory_db, original.task_id)
    assert runtime is not None and runtime.status == "running"
    assert runtime.key == original.key
    await reg.shutdown()


@pytest.mark.asyncio
async def test_existing_root_restores_durable_binding_before_duplicate_spawn(in_memory_db, tmp_path):
    reg, _ = _registry(in_memory_db)
    original, _ = await _task(reg, root="1000.restore")
    reg._by_key.clear()
    reg._by_task_id.clear()
    reg._by_session_id.clear()

    restored = await reg.spawn_task(
        str(tmp_path), team_id="T1", channel_id="CHOME", owner_user_id="UOWNER",
        root_ts=original.root_ts, bind_existing_root=True,
    )
    assert restored.task_id == original.task_id
    assert reg._supervisor._seq == 0
    assert reg.get_by_conversation("T1", "CHOME", original.root_ts) is restored
    await reg.shutdown()


@pytest.mark.asyncio
async def test_existing_root_fetches_paginated_history_once_with_order_files_and_duplicate_guard(in_memory_db, tmp_path):
    reg, bot = _registry(in_memory_db)
    bot.thread_pages = {
        "": {"messages": [
            {"ts": "3.000", "user": "UOTHER", "text": "newest", "files": [{"name": "unsupported.bin"}]},
            {"ts": "1.000", "user": "UOWNER", "text": "old", "files": [{"url_private_download": "https://files.invalid/p", "name": "picture.png", "size": 12, "mimetype": "image/png"}]},
            {"ts": "2.000", "bot_id": "B-BRIDGE", "text": "bridge controls"},
        ], "has_more": True, "response_metadata": {"next_cursor": "page-2"}},
        "page-2": {"messages": [
            {"ts": "3.000", "user": "UOTHER", "text": "duplicate newest"},
            {"ts": "4.000", "user": "UOTHER", "text": "oversize", "files": [{"url_private_download": "https://files.invalid/huge", "name": "huge.jpg", "size": 11 * 1024 * 1024}]},
        ], "has_more": False},
    }
    prompts: list[str] = []

    async def capture(_task, content, **_kwargs):
        prompts.append(content)
        return True

    reg._prompt = capture  # type: ignore[method-assign]
    task = await reg.spawn_task(
        str(tmp_path), team_id="T1", channel_id="CHOME", owner_user_id="UOWNER",
        root_ts="root.1", prompt="do the work", bind_existing_root=True,
    )
    assert task.mention_required is True
    runtime = await state.get_runtime(in_memory_db, task.task_id)
    assert runtime is not None and runtime.mention_required is True
    restored_reg = TaskRegistry(in_memory_db, bot, FakeSupervisor())
    await restored_reg.load_from_db()
    assert restored_reg.get_by_task_id(task.task_id).mention_required is True
    assert [call["cursor"] for call in bot.thread_calls] == [None, "page-2"]
    assert len(prompts) == 1
    body = prompts[0]
    assert body.index('"body":"old') < body.index('"body":"newest')
    assert "duplicate newest" not in body and "bridge controls" not in body
    assert "@" in body and "picture.png" in body
    assert "unsupported" in body and "per-file size limit" in body
    assert "[initial user prompt]" in body and "do the work" in body
    panel = next(post for post in bot.posts if post.get("root_ts") == "root.1" and post.get("blocks"))
    assert panel["blocks"] and not bot.edits
    with pytest.raises(TaskRoutingError):
        await reg.spawn_task(
            str(tmp_path), team_id="T1", channel_id="CHOME", owner_user_id="UOWNER",
            root_ts="root.1", bind_existing_root=True,
        )
    normal_reg, normal_bot = _registry(in_memory_db)
    await normal_reg.spawn_task(str(tmp_path), team_id="T1", channel_id="CHOME", owner_user_id="UOWNER")
    assert normal_bot.roots and normal_bot.roots[-1]["channel_id"] == "CHOME"
    root_actions = [element for block in normal_bot.roots[-1]["blocks"] for element in block["elements"]]
    assert any(item["action_id"] == "task.configure" for item in root_actions)

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bridge.mcp_api import McpApiError, McpCapability, McpContext, McpFacade, SlidingWindowLimiter


class Bot:
    def health(self):
        return {"bot_connected": True, "team_id": "T1", "bot_token": "never"}


class Registry:
    def __init__(self):
        self.task = SimpleNamespace(
            task_id="task-1", polytoken_session_id="session-1", channel_id="C1",
            root_ts="1.0", status="running", mode="personal", last_activity=10,
            progress_started=True, compaction_pending=False, owner_user_id="UOWNER",
            team_id="T1", credential_file_path="/secret/credential.json", port=1234,
        )
        self.calls = []

    def get_by_task_id(self, task_id):
        return self.task if task_id == self.task.task_id else None

    async def list_tasks(self, owner_user_id=None):
        return [self.task] if owner_user_id == "UOWNER" else []

    async def get_state(self, task_id, owner_user_id):
        return {
            "session_title": "Safe title", "active_model": "model", "turn_in_flight": True,
            "todos": [{"title": "work", "status": "in_progress"}],
            "credential_file_path": "/must/not/leak", "provider_token": "secret",
        }

    async def request_compaction(self, task_id, *, owner_user_id):
        self.calls.append(("compact", task_id, owner_user_id)); return "queued"

    async def set_model(self, task_id, model, *, owner_user_id, reasoning_effort=None):
        self.calls.append(("model", task_id, model, owner_user_id, reasoning_effort))

    async def set_facet(self, task_id, facet, *, owner_user_id):
        self.calls.append(("facet", task_id, facet, owner_user_id))

    async def set_effort(self, task_id, effort, *, owner_user_id):
        self.calls.append(("effort", task_id, effort, owner_user_id))

    async def stop_task(self, task_id, owner_user_id):
        self.calls.append(("stop", task_id, owner_user_id)); return True

    async def clear_context(self, task_id, *, owner_user_id):
        self.calls.append(("clear", task_id, owner_user_id))


@pytest.fixture
def facade():
    return McpFacade(Bot(), Registry(), "UOWNER", "T1")


def ctx(*caps, request="req-1", owner="UOWNER", team="T1"):
    return McpContext(owner, team, request, frozenset(caps))


@pytest.mark.asyncio
async def test_health_and_task_status_are_owner_scoped_and_redacted(facade):
    read = ctx(McpCapability.READ)
    health = await facade.call("bridge_health", {}, read)
    assert health["result"]["active_task_count"] == 1
    assert "token" not in str(health).lower()
    status = await facade.call("bridge_task_status", {"task_id": "task-1"}, ctx(McpCapability.READ, request="req-2"))
    assert status["result"]["task"]["session_id"] == "session-1"
    assert "credential" not in str(status).lower()
    assert "port" not in str(status).lower()


@pytest.mark.asyncio
async def test_principal_and_capability_cannot_be_supplied_in_arguments(facade):
    with pytest.raises(McpApiError) as denied:
        await facade.call("bridge_list_tasks", {"owner_user_id": "UOWNER"}, ctx(McpCapability.READ, owner="UOTHER"))
    assert denied.value.code == "forbidden"
    with pytest.raises(McpApiError) as missing:
        await facade.call("bridge_set_facet", {"task_id": "task-1", "facet": "plan"}, ctx(McpCapability.READ))
    assert missing.value.code == "capability_denied"


@pytest.mark.asyncio
async def test_destructive_actions_require_capability_and_confirmation(facade):
    destructive = ctx(McpCapability.DESTRUCTIVE)
    with pytest.raises(McpApiError) as missing:
        await facade.call("bridge_stop_task", {"task_id": "task-1"}, destructive)
    assert missing.value.code == "confirmation_required"
    result = await facade.call("bridge_stop_task", {"task_id": "task-1", "confirm": True}, ctx(McpCapability.DESTRUCTIVE, request="stop-2"))
    assert result["result"]["stopped"] is True
    assert facade.registry.calls[-1] == ("stop", "task-1", "UOWNER")


@pytest.mark.asyncio
async def test_idempotency_returns_cached_result_and_rejects_conflicts(facade):
    control = ctx(McpCapability.CONTROL, request="same")
    first = await facade.call("bridge_compact_task", {"task_id": "task-1"}, control)
    second = await facade.call("bridge_compact_task", {"task_id": "task-1"}, control)
    assert first == second
    assert facade.registry.calls.count(("compact", "task-1", "UOWNER")) == 1
    with pytest.raises(McpApiError) as conflict:
        await facade.call("bridge_set_facet", {"task_id": "task-1", "facet": "plan"}, control)
    assert conflict.value.code == "idempotency_conflict"


def test_sliding_window_rate_limit():
    limiter = SlidingWindowLimiter(limit=2, window_secs=10)
    limiter.check("x", now=1); limiter.check("x", now=2)
    with pytest.raises(McpApiError) as limited:
        limiter.check("x", now=3)
    assert limited.value.code == "rate_limited"
    limiter.check("x", now=12.1)

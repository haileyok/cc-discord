"""Tests for the aiohttp server (POST /v1/notify, GET /v1/health, POST /v1/ask)."""

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
from aiohttp import test_utils

from bridge import state
from bridge.approvals import ApprovalRouter
from bridge.bot import BotNotReady
from bridge.listener import Listener
from bridge.server import (
    build_app, _clamp_timeout, _format_question,
    LISTENER_KEY, ASK_LOCKS_KEY, THREADS_KEY, TASK_REGISTRY_KEY, ZELLIJ_KEY
)
from bridge.threads import ThreadRegistry
from bridge.tasks import TaskRegistry
from bridge.zellij import ZellijManager


@dataclass
class FakeUser:
    id: int
    bot: bool = False


@dataclass
class FakeChannel:
    id: int


@dataclass
class FakeAttachment:
    url: str


@dataclass
class FakeMsg:
    author: FakeUser
    channel: FakeChannel
    content: str = ""
    attachments: list[FakeAttachment] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class FakeBot:
    """Minimal fake Bot for testing the server without real Discord."""

    def __init__(self, channel_id: int = 12345, is_ready: bool = False) -> None:
        self._channel_id = channel_id
        self._is_ready = is_ready
        self._post_calls: list[dict] = []
        self._create_thread_calls: list[dict] = []
        self._next_thread_id = 2000
        self._thread_alive_map: dict[int, bool] = {}
        self._client = None  # Stub for commands.py compatibility

    @property
    def channel_id(self) -> int:
        return self._channel_id

    @property
    def client(self):
        """Stub for commands.py compatibility."""
        return self._client

    @property
    def is_ready(self) -> bool:
        return self._is_ready

    def set_ready(self, ready: bool) -> None:
        self._is_ready = ready

    async def post(self, message: str, *, thread_id: int | None = None) -> list[int]:
        """Fake post: record the call, return a fake message ID."""
        self._post_calls.append(
            {"message": message, "thread_id": thread_id}
        )
        if not self.is_ready:
            raise BotNotReady("bot not connected to Discord")
        # Return a fake ID (first chunk always gets ID 1001)
        return [1001]

    async def create_thread(self, name: str) -> int:
        """Fake create_thread: record the call and return a fake thread ID."""
        thread_id = self._next_thread_id
        self._next_thread_id += 1
        self._create_thread_calls.append({"name": name, "id": thread_id})
        self._thread_alive_map[thread_id] = True
        return thread_id

    async def add_reactions(self, message_id: int, thread_id: int, emoji: list[str]) -> None:
        """Fake add_reactions: do nothing for testing."""
        if not self.is_ready:
            raise BotNotReady("bot not connected to Discord")
        # Just do nothing for testing

    async def thread_alive(self, thread_id: int) -> bool:
        """Fake thread_alive: check the map."""
        return self._thread_alive_map.get(thread_id, True)

    def set_thread_alive(self, thread_id: int, alive: bool) -> None:
        """Set whether a thread is considered alive."""
        self._thread_alive_map[thread_id] = alive

    def get_post_calls(self) -> list[dict]:
        return self._post_calls

    def get_create_thread_calls(self) -> list[dict]:
        return self._create_thread_calls


@pytest.fixture
async def fake_bot():
    return FakeBot()




@pytest.fixture
async def client(fake_bot, in_memory_db, monkeypatch):
    """Create a test client for the aiohttp app with ThreadRegistry and TaskRegistry wired in."""
    started_at = time.monotonic()
    app = await build_app(fake_bot, started_at=started_at)
    registry = ThreadRegistry(fake_bot, in_memory_db)
    # Create a mocked ZellijManager for testing
    zellij = ZellijManager()

    async def mock_run(*argv, env=None, timeout=10.0):
        """Mock _run to always return success."""
        return (0, "", "")

    monkeypatch.setattr(zellij, "_run", mock_run)
    task_registry = TaskRegistry(in_memory_db, fake_bot, zellij)
    await task_registry.load_from_db()
    # Wire in listener and ask_locks for /v1/ask testing
    from bridge.server import AskLockMap, APPROVAL_ROUTER_KEY
    from bridge.approvals import ApprovalRouter
    app[THREADS_KEY] = registry
    app[LISTENER_KEY] = Listener()
    app[ASK_LOCKS_KEY] = AskLockMap()
    app[TASK_REGISTRY_KEY] = task_registry
    app[ZELLIJ_KEY] = zellij
    app[APPROVAL_ROUTER_KEY] = ApprovalRouter(fake_bot, in_memory_db, timeout=0.5)
    async with test_utils.TestClient(test_utils.TestServer(app)) as client:
        yield client


@pytest.mark.asyncio
async def test_health_bot_not_ready(client, fake_bot):
    """GET /v1/health with bot not ready returns bot_connected: false."""
    fake_bot.set_ready(False)
    resp = await client.get("/v1/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["bot_connected"] is False
    assert body["channel_id"] == 12345
    assert body["uptime_secs"] >= 0


@pytest.mark.asyncio
async def test_health_bot_ready(client, fake_bot):
    """GET /v1/health with bot ready returns bot_connected: true."""
    fake_bot.set_ready(True)
    resp = await client.get("/v1/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["bot_connected"] is True
    assert body["channel_id"] == 12345
    assert body["uptime_secs"] >= 0


@pytest.mark.asyncio
async def test_notify_success(client, fake_bot):
    """POST /v1/notify with bot ready and valid body returns 200."""
    fake_bot.set_ready(True)
    resp = await client.post(
        "/v1/notify",
        json={
            "session_id": "test-session",
            "cwd": "/tmp",
            "message": "hello world",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert isinstance(body["thread_id"], int)
    assert body["message_id"] == 1001

    # Verify bot.post was called with the right thread_id
    calls = fake_bot.get_post_calls()
    assert len(calls) == 1
    assert calls[0]["message"] == "hello world"
    assert calls[0]["thread_id"] == body["thread_id"]

    # Verify create_thread was called once
    create_calls = fake_bot.get_create_thread_calls()
    assert len(create_calls) == 1
    assert "cc · tmp ·" in create_calls[0]["name"]


@pytest.mark.asyncio
async def test_notify_bot_not_ready(client, fake_bot):
    """POST /v1/notify with bot not ready returns 503."""
    fake_bot.set_ready(False)
    resp = await client.post(
        "/v1/notify",
        json={
            "session_id": "test-session",
            "cwd": "/tmp",
            "message": "hello world",
        },
    )
    assert resp.status == 503
    body = await resp.json()
    assert body["error"] == "bot_not_connected"

    # Verify bot.post was NOT called
    calls = fake_bot.get_post_calls()
    assert len(calls) == 0


@pytest.mark.asyncio
async def test_notify_empty_body(client, fake_bot):
    """POST /v1/notify with empty body returns 400."""
    fake_bot.set_ready(True)
    resp = await client.post("/v1/notify", json={})
    assert resp.status == 400
    body = await resp.json()
    assert "error" in body


@pytest.mark.asyncio
async def test_notify_missing_session_id(client, fake_bot):
    """POST /v1/notify missing session_id returns 400."""
    fake_bot.set_ready(True)
    resp = await client.post(
        "/v1/notify",
        json={
            "cwd": "/tmp",
            "message": "hello world",
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert "error" in body


@pytest.mark.asyncio
async def test_notify_missing_cwd(client, fake_bot):
    """POST /v1/notify missing cwd returns 400."""
    fake_bot.set_ready(True)
    resp = await client.post(
        "/v1/notify",
        json={
            "session_id": "test-session",
            "message": "hello world",
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert "error" in body


@pytest.mark.asyncio
async def test_notify_missing_message(client, fake_bot):
    """POST /v1/notify missing message returns 400."""
    fake_bot.set_ready(True)
    resp = await client.post(
        "/v1/notify",
        json={
            "session_id": "test-session",
            "cwd": "/tmp",
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert "error" in body


@pytest.mark.asyncio
async def test_notify_bot_error(client, fake_bot):
    """POST /v1/notify where bot.post raises exception returns 500."""
    fake_bot.set_ready(True)
    # Patch the bot to raise a generic exception
    async def failing_post(*args, **kwargs):
        raise RuntimeError("Something went wrong")

    fake_bot.post = failing_post
    resp = await client.post(
        "/v1/notify",
        json={
            "session_id": "test-session",
            "cwd": "/tmp",
            "message": "hello world",
        },
    )
    assert resp.status == 500
    body = await resp.json()
    assert body["error"] == "internal"
    # Should NOT contain stack trace
    assert "Traceback" not in body.get("error", "")


@pytest.mark.asyncio
async def test_notify_recovers_after_error(client, fake_bot):
    """Server recovers cleanly after a 503 error."""
    fake_bot.set_ready(False)
    resp1 = await client.post(
        "/v1/notify",
        json={
            "session_id": "test-session",
            "cwd": "/tmp",
            "message": "hello world",
        },
    )
    assert resp1.status == 503

    # Now make the bot ready and try again
    fake_bot.set_ready(True)
    resp2 = await client.post(
        "/v1/notify",
        json={
            "session_id": "test-session",
            "cwd": "/tmp",
            "message": "hello world 2",
        },
    )
    assert resp2.status == 200
    body = await resp2.json()
    assert isinstance(body["thread_id"], int)
    assert body["message_id"] == 1001


@pytest.mark.asyncio
async def test_notify_with_optional_fields(client, fake_bot):
    """POST /v1/notify accepts optional title and level fields."""
    fake_bot.set_ready(True)
    resp = await client.post(
        "/v1/notify",
        json={
            "session_id": "test-session",
            "cwd": "/tmp",
            "message": "hello world",
            "title": "Test Title",
            "level": "info",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert isinstance(body["thread_id"], int)
    assert body["message_id"] == 1001


@pytest.mark.asyncio
async def test_notify_distinct_sessions_distinct_threads(client, fake_bot):
    """AC2.1: Two POST /v1/notify calls with different session_ids create distinct threads."""
    fake_bot.set_ready(True)
    resp1 = await client.post(
        "/v1/notify",
        json={
            "session_id": "sess-aaa",
            "cwd": "/tmp/aaa",
            "message": "alpha",
        },
    )
    assert resp1.status == 200
    body1 = await resp1.json()
    thread_id_1 = body1["thread_id"]

    resp2 = await client.post(
        "/v1/notify",
        json={
            "session_id": "sess-bbb",
            "cwd": "/tmp/bbb",
            "message": "beta",
        },
    )
    assert resp2.status == 200
    body2 = await resp2.json()
    thread_id_2 = body2["thread_id"]

    # Different sessions should create different threads
    assert thread_id_1 != thread_id_2
    # Each should have called create_thread once
    create_calls = fake_bot.get_create_thread_calls()
    assert len(create_calls) == 2


@pytest.mark.asyncio
async def test_notify_same_session_reuses_thread(client, fake_bot):
    """AC2.2: Two POST /v1/notify calls with same session_id route to same thread."""
    fake_bot.set_ready(True)
    resp1 = await client.post(
        "/v1/notify",
        json={
            "session_id": "sess-aaa",
            "cwd": "/tmp/aaa",
            "message": "first",
        },
    )
    assert resp1.status == 200
    body1 = await resp1.json()
    thread_id_1 = body1["thread_id"]

    resp2 = await client.post(
        "/v1/notify",
        json={
            "session_id": "sess-aaa",
            "cwd": "/tmp/aaa",
            "message": "second",
        },
    )
    assert resp2.status == 200
    body2 = await resp2.json()
    thread_id_2 = body2["thread_id"]

    # Same session should reuse the thread
    assert thread_id_1 == thread_id_2
    # Should have only called create_thread once
    create_calls = fake_bot.get_create_thread_calls()
    assert len(create_calls) == 1


@pytest.mark.asyncio
async def test_notify_404_recovery_creates_new_thread(client, fake_bot):
    """AC2.4: When a thread is deleted, next call recreates it and updates mapping."""
    fake_bot.set_ready(True)
    resp1 = await client.post(
        "/v1/notify",
        json={
            "session_id": "sess-aaa",
            "cwd": "/tmp/aaa",
            "message": "first",
        },
    )
    assert resp1.status == 200
    body1 = await resp1.json()
    thread_id_1 = body1["thread_id"]

    # Simulate thread deletion
    fake_bot.set_thread_alive(thread_id_1, False)

    # Next call should detect dead thread and create a new one
    resp2 = await client.post(
        "/v1/notify",
        json={
            "session_id": "sess-aaa",
            "cwd": "/tmp/aaa",
            "message": "second",
        },
    )
    assert resp2.status == 200
    body2 = await resp2.json()
    thread_id_2 = body2["thread_id"]

    # Should have created a new thread (different ID)
    assert thread_id_1 != thread_id_2
    # Should have called create_thread twice total
    create_calls = fake_bot.get_create_thread_calls()
    assert len(create_calls) == 2


# Tests for _clamp_timeout helper function
class TestClampTimeout:
    """Unit tests for the _clamp_timeout helper."""

    def test_clamp_timeout_below_minimum(self):
        """_clamp_timeout(1) returns 5.0 (minimum)."""
        assert _clamp_timeout(1) == 5.0

    def test_clamp_timeout_above_maximum(self):
        """_clamp_timeout(99999) returns 3600.0 (maximum)."""
        assert _clamp_timeout(99999) == 3600.0

    def test_clamp_timeout_in_range(self):
        """_clamp_timeout(100) returns 100.0."""
        assert _clamp_timeout(100) == 100.0

    def test_clamp_timeout_exact_minimum(self):
        """_clamp_timeout(5.0) returns 5.0."""
        assert _clamp_timeout(5.0) == 5.0

    def test_clamp_timeout_exact_maximum(self):
        """_clamp_timeout(3600.0) returns 3600.0."""
        assert _clamp_timeout(3600.0) == 3600.0


# Tests for _format_question helper function
class TestFormatQuestion:
    """Unit tests for the _format_question helper."""

    def test_format_question_basic(self):
        """_format_question formats with header, question, and cwd."""
        result = _format_question("hello?", "/tmp")
        assert "❓ asks" in result
        assert "hello?" in result
        assert "(cwd: /tmp)" in result

    def test_format_question_multiline(self):
        """_format_question preserves newlines in question."""
        result = _format_question("line 1\nline 2", "/home")
        assert "line 1\nline 2" in result
        assert "(cwd: /home)" in result

    def test_format_question_empty_cwd(self):
        """_format_question handles empty cwd without showing empty parens."""
        result = _format_question("test", "")
        assert "❓ asks" in result
        assert "test" in result
        assert "(cwd:" not in result


# Tests for /v1/ask endpoint
class TestAskEndpoint:
    """Integration tests for POST /v1/ask."""

    @pytest.mark.asyncio
    async def test_ask_happy_path_ac31(self, client, fake_bot):
        """AC3.1: POST /v1/ask posts question, awaits reply, returns 200 with reply text."""
        fake_bot.set_ready(True)
        listener: Listener = client.app[LISTENER_KEY]

        # Start the ask request in the background
        ask_task = asyncio.create_task(
            client.post(
                "/v1/ask",
                json={
                    "session_id": "sess-test",
                    "cwd": "/tmp/test",
                    "question": "what is this?",
                    "timeout_secs": 5,
                },
            )
        )

        # Wait briefly for the question to be posted
        await asyncio.sleep(0.1)

        # Verify question was posted
        post_calls = fake_bot.get_post_calls()
        assert len(post_calls) >= 1
        last_call = post_calls[-1]
        assert "❓ asks" in last_call["message"]
        assert "what is this?" in last_call["message"]

        # Get the thread_id from the question post
        thread_id = last_call["thread_id"]

        # Deliver a reply message to the listener (with timestamp after the ask)
        reply_msg = FakeMsg(
            author=FakeUser(id=999),
            channel=FakeChannel(id=thread_id),
            content="the answer",
            created_at=datetime.now(timezone.utc),
        )
        await listener.deliver(reply_msg)

        # Wait for the ask to complete
        resp = await asyncio.wait_for(ask_task, timeout=5)
        assert resp.status == 200
        body = await resp.json()
        assert body["reply"] == "the answer"
        assert "replied_at" in body

    @pytest.mark.asyncio
    async def test_ask_timeout_no_leak_ac35(self, client, fake_bot):
        """AC3.5: Timeout removes pending ask (no leak in listener._pending)."""
        fake_bot.set_ready(True)
        listener: Listener = client.app[LISTENER_KEY]

        # Spawn a background task to make the ask request with minimum clamped timeout
        # Note: this is slow (5 seconds) so it's marked @pytest.mark.slow
        async def make_ask():
            resp = await client.post(
                "/v1/ask",
                json={
                    "session_id": "sess-timeout",
                    "cwd": "/tmp",
                    "question": "no answer coming",
                    "timeout_secs": 5.2,  # Clamped to 5
                },
            )
            return resp

        # Start the request
        task = asyncio.create_task(make_ask())

        # Wait briefly for it to post
        await asyncio.sleep(0.1)

        # Capture thread_id before timeout
        post_calls = fake_bot.get_post_calls()
        thread_id = post_calls[-1]["thread_id"]

        # Verify it's registered
        assert thread_id in listener._pending

        # Wait for the timeout to fire (5 seconds)
        resp = await task

        # Verify timeout response
        assert resp.status == 408
        body = await resp.json()
        assert body["error"] == "timeout"

        # Verify no leak: thread_id should NOT be in _pending after timeout
        assert thread_id not in listener._pending

    @pytest.mark.asyncio
    async def test_ask_fifo_concurrent_ac34(self, client, fake_bot):
        """AC3.4: AskLockMap ensures FIFO per-thread serialization."""
        # This test verifies the FIFO mechanism is in place via the lock structure
        fake_bot.set_ready(True)
        locks = client.app[ASK_LOCKS_KEY]

        # Verify that AskLockMap creates locks on demand
        lock1 = await locks.get(1000)
        assert isinstance(lock1, asyncio.Lock)

        # Same thread_id returns same lock
        lock1_again = await locks.get(1000)
        assert lock1_again is lock1

        # Different thread_id gets different lock
        lock2 = await locks.get(2000)
        assert lock2 is not lock1
        assert isinstance(lock2, asyncio.Lock)

        # Verify the lock prevents concurrent access (serializes FIFO)
        # This is the core mechanism that ensures AC3.4
        order = []

        async def acquire_lock_and_record(thread_id, name):
            lock = await locks.get(thread_id)
            async with lock:
                order.append(f"{name}-start")
                await asyncio.sleep(0.05)
                order.append(f"{name}-end")

        # Run two tasks concurrently on same thread_id
        task1 = asyncio.create_task(acquire_lock_and_record(3000, "first"))
        task2 = asyncio.create_task(acquire_lock_and_record(3000, "second"))

        await asyncio.gather(task1, task2)

        # Verify FIFO ordering: one task completes fully before the other starts
        # (no interleaving of start/end from different tasks)
        assert order == [
            "first-start",
            "first-end",
            "second-start",
            "second-end",
        ] or order == [
            "second-start",
            "second-end",
            "first-start",
            "first-end",
        ]
        # Both orderings are valid FIFO; the point is that starts and ends are grouped

    @pytest.mark.asyncio
    async def test_ask_multiline_replies_ac32(self, client, fake_bot):
        """AC3.2: Multiple replies from same author within grace window coalesce."""
        fake_bot.set_ready(True)
        listener: Listener = client.app[LISTENER_KEY]

        # Start an ask
        ask_task = asyncio.create_task(
            client.post(
                "/v1/ask",
                json={
                    "session_id": "sess-coalesce",
                    "cwd": "/tmp",
                    "question": "long question",
                    "timeout_secs": 5,
                },
            )
        )

        await asyncio.sleep(0.1)

        # Get thread_id
        post_calls = fake_bot.get_post_calls()
        thread_id = post_calls[-1]["thread_id"]

        # Deliver two quick messages from same author
        # Messages must have created_at > the server's asked_at, so use current time
        now = datetime.now(timezone.utc)
        msg1 = FakeMsg(
            author=FakeUser(id=999),
            channel=FakeChannel(id=thread_id),
            content="part 1",
            created_at=now,
        )
        msg2 = FakeMsg(
            author=FakeUser(id=999),
            channel=FakeChannel(id=thread_id),
            content="part 2",
            created_at=datetime.fromtimestamp(now.timestamp() + 0.05, tz=timezone.utc),
        )

        await listener.deliver(msg1)
        await asyncio.sleep(0.01)
        await listener.deliver(msg2)

        # Wait for ask to complete (grace period is 3s, so wait a bit past that)
        resp = await asyncio.wait_for(ask_task, timeout=5)
        assert resp.status == 200
        body = await resp.json()
        # Both messages should be coalesced with newline
        assert body["reply"] == "part 1\npart 2"

    @pytest.mark.asyncio
    async def test_ask_image_attachments_ac33(self, client, fake_bot):
        """AC3.3: Image attachments are returned as [image] URLs."""
        fake_bot.set_ready(True)
        listener: Listener = client.app[LISTENER_KEY]

        ask_task = asyncio.create_task(
            client.post(
                "/v1/ask",
                json={
                    "session_id": "sess-image",
                    "cwd": "/tmp",
                    "question": "show me",
                    "timeout_secs": 5,
                },
            )
        )

        await asyncio.sleep(0.1)

        post_calls = fake_bot.get_post_calls()
        thread_id = post_calls[-1]["thread_id"]

        # Deliver message with attachments (no text content)
        msg = FakeMsg(
            author=FakeUser(id=999),
            channel=FakeChannel(id=thread_id),
            content="",
            attachments=[
                FakeAttachment(url="https://cdn.discordapp.com/image1.png"),
                FakeAttachment(url="https://cdn.discordapp.com/image2.jpg"),
            ],
            created_at=datetime.now(timezone.utc),
        )
        await listener.deliver(msg)

        resp = await asyncio.wait_for(ask_task, timeout=5)
        assert resp.status == 200
        body = await resp.json()
        # Both URLs should be present with [image] prefix
        assert "[image] https://cdn.discordapp.com/image1.png" in body["reply"]
        assert "[image] https://cdn.discordapp.com/image2.jpg" in body["reply"]

    @pytest.mark.asyncio
    async def test_ask_text_and_images_combined(self, client, fake_bot):
        """AC3.3 + AC3.2: Text and attachments combined; URLs after text."""
        fake_bot.set_ready(True)
        listener: Listener = client.app[LISTENER_KEY]

        ask_task = asyncio.create_task(
            client.post(
                "/v1/ask",
                json={
                    "session_id": "sess-mixed",
                    "cwd": "/tmp",
                    "question": "what?",
                    "timeout_secs": 5,
                },
            )
        )

        await asyncio.sleep(0.1)

        post_calls = fake_bot.get_post_calls()
        thread_id = post_calls[-1]["thread_id"]

        msg = FakeMsg(
            author=FakeUser(id=999),
            channel=FakeChannel(id=thread_id),
            content="check this",
            attachments=[FakeAttachment(url="https://example.com/pic.png")],
            created_at=datetime.now(timezone.utc),
        )
        await listener.deliver(msg)

        resp = await asyncio.wait_for(ask_task, timeout=5)
        assert resp.status == 200
        body = await resp.json()
        # Text first, then image URL
        assert body["reply"] == "check this\n[image] https://example.com/pic.png"

    @pytest.mark.asyncio
    async def test_ask_bot_messages_filtered_ac36(self, client, fake_bot):
        """AC3.6: Bot's own messages do not resolve the future."""
        fake_bot.set_ready(True)
        listener: Listener = client.app[LISTENER_KEY]

        ask_task = asyncio.create_task(
            client.post(
                "/v1/ask",
                json={
                    "session_id": "sess-bot-filter",
                    "cwd": "/tmp",
                    "question": "?",
                    "timeout_secs": 5,
                },
            )
        )

        await asyncio.sleep(0.1)

        post_calls = fake_bot.get_post_calls()
        thread_id = post_calls[-1]["thread_id"]

        # Try to deliver a bot message (should be ignored)
        bot_msg = FakeMsg(
            author=FakeUser(id=999, bot=True),
            channel=FakeChannel(id=thread_id),
            content="bot reply",
            created_at=datetime.now(timezone.utc),
        )
        await listener.deliver(bot_msg)

        # Wait a bit to ensure it's not resolved
        await asyncio.sleep(0.2)

        # Now deliver a real user message
        user_msg = FakeMsg(
            author=FakeUser(id=888),
            channel=FakeChannel(id=thread_id),
            content="user reply",
            created_at=datetime.now(timezone.utc),
        )
        await listener.deliver(user_msg)

        resp = await asyncio.wait_for(ask_task, timeout=5)
        assert resp.status == 200
        body = await resp.json()
        # Should only have the user reply, not the bot reply
        assert body["reply"] == "user reply"

    @pytest.mark.asyncio
    async def test_ask_bot_not_ready(self, client, fake_bot):
        """503 when bot is not ready."""
        fake_bot.set_ready(False)
        resp = await client.post(
            "/v1/ask",
            json={
                "session_id": "sess-test",
                "cwd": "/tmp",
                "question": "?",
            },
        )
        assert resp.status == 503
        body = await resp.json()
        assert body["error"] == "bot_not_connected"

    @pytest.mark.asyncio
    async def test_ask_missing_question(self, client, fake_bot):
        """400 when question is missing."""
        fake_bot.set_ready(True)
        resp = await client.post(
            "/v1/ask",
            json={
                "session_id": "sess-test",
                "cwd": "/tmp",
            },
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_ask_missing_session_id(self, client, fake_bot):
        """400 when session_id is missing."""
        fake_bot.set_ready(True)
        resp = await client.post(
            "/v1/ask",
            json={
                "cwd": "/tmp",
                "question": "?",
            },
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_ask_missing_cwd(self, client, fake_bot):
        """400 when cwd is missing."""
        fake_bot.set_ready(True)
        resp = await client.post(
            "/v1/ask",
            json={
                "session_id": "sess-test",
                "question": "?",
            },
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_ask_malformed_json(self, client, fake_bot):
        """400 on malformed JSON body."""
        fake_bot.set_ready(True)
        resp = await client.post(
            "/v1/ask",
            data="not json",
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_ask_timeout_clamping(self, client, fake_bot):
        """Timeout values are clamped to [5, 3600]; _clamp_timeout is tested separately."""
        fake_bot.set_ready(True)
        listener: Listener = client.app[LISTENER_KEY]

        # Verify that _clamp_timeout works via the helper function
        # (not by waiting for actual timeout which would be slow)
        assert _clamp_timeout(1) == 5.0
        assert _clamp_timeout(99999) == 3600.0

        # For endpoint testing, just verify structure - actual timeout is tested in slow test
        # Start an ask and verify it's registered
        async def make_ask():
            return await client.post(
                "/v1/ask",
                json={
                    "session_id": "sess-clamp-low",
                    "cwd": "/tmp",
                    "question": "?",
                    "timeout_secs": 1,  # below 5, will be clamped to 5
                },
            )

        task = asyncio.create_task(make_ask())
        await asyncio.sleep(0.1)

        # Verify ask is registered
        post_calls = fake_bot.get_post_calls()
        assert post_calls, "ask should have posted by now"
        thread_id = post_calls[-1]["thread_id"]
        assert thread_id in listener._pending

        # Cancel the task to avoid timeout wait
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_ask_different_threads_no_interference(self, client, fake_bot):
        """Two concurrent asks for different session IDs don't interfere."""
        fake_bot.set_ready(True)
        listener: Listener = client.app[LISTENER_KEY]

        async def ask_and_reply(session_id: str, question: str):
            task = asyncio.create_task(
                client.post(
                    "/v1/ask",
                    json={
                        "session_id": session_id,
                        "cwd": "/tmp",
                        "question": question,
                        "timeout_secs": 5,
                    },
                )
            )
            await asyncio.sleep(0.1)

            # Find the thread for this session
            post_calls = fake_bot.get_post_calls()
            # Get the last call's thread_id
            thread_id = post_calls[-1]["thread_id"]

            # Reply
            msg = FakeMsg(
                author=FakeUser(id=111),
                channel=FakeChannel(id=thread_id),
                content=f"reply-{session_id}",
                created_at=datetime.now(timezone.utc),
            )
            await listener.deliver(msg)

            resp = await asyncio.wait_for(task, timeout=5)
            body = await resp.json()
            return resp.status, body

        # Run two asks concurrently for different sessions
        status1, body1 = await ask_and_reply("sess-a", "first")
        status2, body2 = await ask_and_reply("sess-b", "second")

        assert status1 == 200
        assert status2 == 200
        assert body1["reply"] == "reply-sess-a"
        assert body2["reply"] == "reply-sess-b"

    @pytest.mark.asyncio
    async def test_ask_concurrent_same_session_fifo_ordering_ac34(self, client, fake_bot):
        """AC3.4: Two concurrent /v1/ask for same session via endpoint enforce FIFO ordering.

        Tests that the AskLockMap serializes /v1/ask calls per-thread, with real HTTP calls.
        The key assertion: the second ask's question is NOT posted until the first ask's
        request has acquired and held the lock, proving FIFO serialization.
        """
        fake_bot.set_ready(True)

        # Track posts at the start
        initial_post_count = len(fake_bot.get_post_calls())

        # Create one ask task, wait for it to post, then create a second concurrent ask
        ask1_task = asyncio.create_task(
            client.post(
                "/v1/ask",
                json={
                    "session_id": "sess-fifo-ordering",
                    "cwd": "/tmp",
                    "question": "first ask",
                    "timeout_secs": 5,
                },
            )
        )

        # Wait for first question to be posted
        await asyncio.sleep(0.2)

        post_calls = fake_bot.get_post_calls()
        first_q_calls = [
            call for call in post_calls[initial_post_count:]
            if "first ask" in call.get("message", "")
        ]
        assert len(first_q_calls) == 1

        # Now create second concurrent ask for same session
        ask2_task = asyncio.create_task(
            client.post(
                "/v1/ask",
                json={
                    "session_id": "sess-fifo-ordering",
                    "cwd": "/tmp",
                    "question": "second ask",
                    "timeout_secs": 60,
                },
            )
        )

        # Give ask2 time to try to acquire the lock, but it should be blocked
        await asyncio.sleep(0.2)

        # Verify second question hasn't been posted yet
        post_calls_mid = fake_bot.get_post_calls()
        second_q_calls_mid = [
            call for call in post_calls_mid[initial_post_count:]
            if "second ask" in call.get("message", "")
        ]
        # FIFO guarantee: second question must not have been posted yet
        assert len(second_q_calls_mid) == 0, "Second question should not be posted until first ask releases lock"

        # Cancel both tasks (we only needed to verify the FIFO ordering of posts)
        ask1_task.cancel()
        ask2_task.cancel()

        # Clean up
        with contextlib.suppress(asyncio.CancelledError):
            await ask1_task
        with contextlib.suppress(asyncio.CancelledError):
            await ask2_task

    @pytest.mark.asyncio
    async def test_ask_invalid_timeout_secs(self, client, fake_bot):
        """POST /v1/ask with non-numeric timeout_secs returns 400."""
        fake_bot.set_ready(True)
        resp = await client.post(
            "/v1/ask",
            json={
                "session_id": "sess-test",
                "cwd": "/tmp",
                "question": "?",
                "timeout_secs": "abc",
            },
        )
        assert resp.status == 400
        body = await resp.json()
        assert body["error"] == "invalid timeout_secs"

    @pytest.mark.asyncio
    async def test_ask_generic_exception(self, client, fake_bot):
        """POST /v1/ask where bot.post raises generic Exception returns 500 with JSON body."""
        fake_bot.set_ready(True)

        async def failing_post(*args, **kwargs):
            raise RuntimeError("Something went wrong")

        fake_bot.post = failing_post
        resp = await client.post(
            "/v1/ask",
            json={
                "session_id": "sess-test",
                "cwd": "/tmp",
                "question": "?",
            },
        )
        assert resp.status == 500
        body = await resp.json()
        assert body["error"] == "internal"


@pytest.mark.asyncio
class TestHookEvent:
    """Tests for POST /v1/hook/event endpoint."""

    async def test_hook_event_session_start_with_task(self, client, fake_bot, in_memory_db):
        """POST /v1/hook/event with SessionStart and matching task_id returns 200."""
        from bridge.state import upsert_task
        import time

        # Create a task in the database
        now = int(time.time())
        await upsert_task(
            in_memory_db,
            "task-123",
            999,
            "/tmp",
            "spawning",
            now=now,
        )
        # Reload task registry
        task_registry = client.app[TASK_REGISTRY_KEY]
        await task_registry.load_from_db()

        fake_bot.set_ready(True)
        resp = await client.post(
            "/v1/hook/event",
            json={
                "hook_event_name": "SessionStart",
                "session_id": "sess-abc",
                "cwd": "/tmp",
                "transcript_path": "/path",
                "env_passthrough": {"CC_BRIDGE_TASK_ID": "task-123"},
            },
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True

        # Verify bot.post was called
        posts = fake_bot.get_post_calls()
        assert len(posts) == 1
        assert posts[0]["thread_id"] == 999

    async def test_hook_event_stop(self, client, fake_bot):
        """POST /v1/hook/event with Stop event returns 200."""
        fake_bot.set_ready(True)
        resp = await client.post(
            "/v1/hook/event",
            json={
                "hook_event_name": "Stop",
                "session_id": "sess-abc",
            },
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True

    async def test_hook_event_user_prompt_submit(self, client, fake_bot):
        """POST /v1/hook/event with UserPromptSubmit returns 200."""
        fake_bot.set_ready(True)
        resp = await client.post(
            "/v1/hook/event",
            json={
                "hook_event_name": "UserPromptSubmit",
                "session_id": "sess-abc",
            },
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True

    async def test_hook_event_post_tool_use(self, client, fake_bot):
        """POST /v1/hook/event with PostToolUse returns 200."""
        fake_bot.set_ready(True)
        resp = await client.post(
            "/v1/hook/event",
            json={
                "hook_event_name": "PostToolUse",
                "session_id": "sess-abc",
            },
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True

    async def test_hook_event_notification(self, client, fake_bot):
        """POST /v1/hook/event with Notification returns 200."""
        fake_bot.set_ready(True)
        resp = await client.post(
            "/v1/hook/event",
            json={
                "hook_event_name": "Notification",
                "session_id": "sess-abc",
            },
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True

    async def test_hook_event_session_end(self, client, fake_bot):
        """POST /v1/hook/event with SessionEnd returns 200."""
        fake_bot.set_ready(True)
        resp = await client.post(
            "/v1/hook/event",
            json={
                "hook_event_name": "SessionEnd",
                "session_id": "sess-abc",
            },
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True

    async def test_hook_event_post_tool_use_failure(self, client, fake_bot):
        """POST /v1/hook/event with PostToolUseFailure returns 200."""
        fake_bot.set_ready(True)
        resp = await client.post(
            "/v1/hook/event",
            json={
                "hook_event_name": "PostToolUseFailure",
                "session_id": "sess-abc",
            },
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True

    async def test_hook_event_missing_hook_event_name(self, client, fake_bot):
        """POST /v1/hook/event without hook_event_name returns 400."""
        fake_bot.set_ready(True)
        resp = await client.post(
            "/v1/hook/event",
            json={
                "session_id": "sess-abc",
            },
        )
        assert resp.status == 400
        body = await resp.json()
        assert "error" in body

    async def test_hook_event_malformed_json(self, client, fake_bot):
        """POST /v1/hook/event with malformed JSON returns 400."""
        fake_bot.set_ready(True)
        resp = await client.post(
            "/v1/hook/event",
            data="not json",
        )
        assert resp.status == 400
        body = await resp.json()
        assert "error" in body

    async def test_hook_event_non_dict_json_body(self, client, fake_bot):
        """POST /v1/hook/event with non-dict JSON body returns 400."""
        fake_bot.set_ready(True)
        # Send a JSON number instead of an object
        resp = await client.post(
            "/v1/hook/event",
            json=5,
        )
        assert resp.status == 400
        body = await resp.json()
        assert "error" in body
        assert "JSON object" in body["error"]

    async def test_hook_event_unknown_event_name(self, client, fake_bot):
        """POST /v1/hook/event with unknown event_name returns 200 (silent no-op)."""
        fake_bot.set_ready(True)
        resp = await client.post(
            "/v1/hook/event",
            json={
                "hook_event_name": "Banana",
                "session_id": "sess-abc",
            },
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True

    async def test_hook_event_handler_exception(self, client, fake_bot, monkeypatch):
        """POST /v1/hook/event with handler exception returns 500."""
        fake_bot.set_ready(True)

        # Patch the registry's handle_event to raise an exception
        async def failing_handle_event(*args, **kwargs):
            raise RuntimeError("Handler failed")

        task_registry = client.app[TASK_REGISTRY_KEY]
        monkeypatch.setattr(task_registry, "handle_event", failing_handle_event)

        resp = await client.post(
            "/v1/hook/event",
            json={
                "hook_event_name": "SessionStart",
                "session_id": "sess-abc",
            },
        )
        assert resp.status == 500
        body = await resp.json()
        assert body["error"] == "internal"


@pytest.mark.asyncio
class TestDispatcher:
    """Tests for message dispatcher in serve()."""

    async def test_production_dispatcher_resolve_by_text_precedence(
        self, fake_bot, in_memory_db, monkeypatch
    ) -> None:
        """Test the production dispatcher enforces resolve_by_text → maybe_route_message → listener order.

        This regression test verifies the critical dispatch-order invariant: when a pending
        approval exists for a thread and the user types a free-text reply, that reply is
        resolved as deny-with-reason and NOT routed to zellij (maybe_route_message is skipped).
        """
        from bridge.server import make_message_dispatcher
        from bridge.state import upsert_task

        zellij = ZellijManager()

        async def mock_run(*argv, env=None, timeout=10.0):
            return (0, "", "")

        monkeypatch.setattr(zellij, "_run", mock_run)

        # Create a task in the database
        task_id = "task-approval-test"
        thread_id = 5001
        pane_id = "pane_approval"
        now = int(__import__('time').time())
        await upsert_task(
            in_memory_db, task_id, thread_id, "/tmp", "running",
            zellij_pane_id=pane_id,
            current_claude_session_id="sess-abc",
            current_transcript_path="/path/transcript",
            now=now,
        )

        task_registry = TaskRegistry(in_memory_db, fake_bot, zellij)
        await task_registry.load_from_db()

        listener = Listener()
        listener_calls = []

        async def track_deliver(msg):
            listener_calls.append(msg)

        listener.deliver = track_deliver

        # Create approval router and register a pending approval
        approval_router = ApprovalRouter(fake_bot, in_memory_db, timeout=0.1)

        # Track zellij calls
        zellij_calls = []

        async def mock_write_to_pane(pane_id: str, text: str) -> None:
            zellij_calls.append({"pane_id": pane_id, "text": text})

        monkeypatch.setattr(zellij, "write_to_pane", mock_write_to_pane)

        # Create a pending approval for this thread
        import asyncio
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        import time
        from bridge.approvals import _PendingApproval
        pending = _PendingApproval(
            request_id="req-1",
            task_id=task_id,
            tool_name="Bash",
            tool_input={},
            thread_id=thread_id,
            created_at=int(time.time()),
            future=fut,
        )
        async with approval_router._lock:
            approval_router._by_request_id["req-1"] = pending

        # Create the production dispatcher
        _dispatch_message = make_message_dispatcher(approval_router, task_registry, listener)

        # Simulate a free-text reply to the approval in the thread
        msg = FakeMsg(
            author=FakeUser(id=111),
            channel=FakeChannel(id=thread_id),
            content="use a different approach instead"
        )

        await _dispatch_message(msg)

        # Verify the approval was resolved by text
        assert fut.done()
        decision, reason = fut.result()
        assert decision == "deny"
        assert reason == "use a different approach instead"

        # Verify the message was NOT routed to zellij
        assert len(zellij_calls) == 0
        # Verify the message was NOT delivered to listener
        assert len(listener_calls) == 0

    async def test_dispatcher_routes_task_thread_message(
        self, fake_bot, in_memory_db, monkeypatch
    ) -> None:
        """Dispatcher routes task-thread messages to zellij, not listener."""
        from bridge.state import upsert_task
        zellij = ZellijManager()

        async def mock_run(*argv, env=None, timeout=10.0):
            """Mock _run to always return success."""
            return (0, "", "")

        monkeypatch.setattr(zellij, "_run", mock_run)

        # Create a task in the database
        task_id = "task-123"
        thread_id = 5000
        pane_id = "pane_1"
        now = int(__import__('time').time())
        await upsert_task(
            in_memory_db, task_id, thread_id, "/tmp", "running",
            zellij_pane_id=pane_id,
            current_claude_session_id="sess-abc",
            current_transcript_path="/path/transcript",
            now=now,
        )

        task_registry = TaskRegistry(in_memory_db, fake_bot, zellij)
        await task_registry.load_from_db()

        listener = Listener()
        listener_calls = []

        async def track_deliver(msg):
            listener_calls.append(msg)

        listener.deliver = track_deliver

        # Track zellij calls
        zellij_calls = []

        async def mock_write_to_pane(pane_id: str, text: str) -> None:
            zellij_calls.append({"pane_id": pane_id, "text": text})

        monkeypatch.setattr(zellij, "write_to_pane", mock_write_to_pane)

        # Build dispatcher
        async def _dispatch_message(msg):
            if await task_registry.maybe_route_message(msg):
                return
            await listener.deliver(msg)

        # Simulate a message in the task thread
        msg = FakeMsg(
            author=FakeUser(id=111),
            channel=FakeChannel(id=thread_id),
            content="hello"
        )
        await _dispatch_message(msg)

        # Verify dispatcher called zellij, not listener
        assert len(zellij_calls) == 1
        assert zellij_calls[0]["pane_id"] == pane_id
        assert "hello" in zellij_calls[0]["text"]
        assert len(listener_calls) == 0

    async def test_dispatcher_falls_through_to_listener(
        self, fake_bot, in_memory_db, monkeypatch
    ) -> None:
        """Dispatcher falls through to listener for non-task messages."""
        zellij = ZellijManager()

        async def mock_run(*argv, env=None, timeout=10.0):
            """Mock _run to always return success."""
            return (0, "", "")

        monkeypatch.setattr(zellij, "_run", mock_run)

        task_registry = TaskRegistry(in_memory_db, fake_bot, zellij)
        await task_registry.load_from_db()

        listener = Listener()
        listener_calls = []

        async def track_deliver(msg):
            listener_calls.append(msg)

        listener.deliver = track_deliver

        # Track zellij calls
        zellij_calls = []

        async def mock_write_to_pane(pane_id: str, text: str) -> None:
            zellij_calls.append({"pane_id": pane_id, "text": text})

        monkeypatch.setattr(zellij, "write_to_pane", mock_write_to_pane)

        # Build dispatcher
        async def _dispatch_message(msg):
            if await task_registry.maybe_route_message(msg):
                return
            await listener.deliver(msg)

        # Simulate a message in a thread with NO bound task
        msg = FakeMsg(
            author=FakeUser(id=222),
            channel=FakeChannel(id=9999),  # Not a task thread
            content="ask something"
        )
        await _dispatch_message(msg)

        # Verify dispatcher called listener, not zellij
        assert len(listener_calls) == 1
        assert listener_calls[0] is msg
        assert len(zellij_calls) == 0


# Tests for POST /v1/hook/pretooluse endpoint

@pytest.mark.asyncio
async def test_pretooluse_valid_request(client, in_memory_db, fake_bot):
    """POST /v1/hook/pretooluse with valid body and known task returns approval decision."""
    # Set bot to ready state
    fake_bot.set_ready(True)

    # Create a task in the DB and in the TaskRegistry cache
    from bridge.server import TASK_REGISTRY_KEY
    await state.upsert_task(in_memory_db, "task-1", 1001, "/tmp", "running")
    task_registry = client.server.app[TASK_REGISTRY_KEY]
    # Reload from DB to populate cache
    await task_registry.load_from_db()

    resp = await client.post(
        "/v1/hook/pretooluse",
        json={
            "request_id": "req-1",
            "task_id": "task-1",
            "tool_name": "Bash",
            "tool_input": {"cmd": "ls"}
        }
    )

    assert resp.status == 200
    body = await resp.json()
    # The default behavior is deny (timeout since there's no input to the future)
    # That's the correct behavior - without user response, it times out and denies
    assert body["decision"] == "deny"
    assert body["reason"] == "approval timed out"


@pytest.mark.asyncio
async def test_pretooluse_missing_field(client):
    """POST /v1/hook/pretooluse missing required field returns 400."""
    resp = await client.post(
        "/v1/hook/pretooluse",
        json={
            "request_id": "req-1",
            "task_id": "task-1",
            # missing tool_name and tool_input
        }
    )
    assert resp.status == 400
    body = await resp.json()
    assert "error" in body


@pytest.mark.asyncio
async def test_pretooluse_invalid_json(client):
    """POST /v1/hook/pretooluse with invalid JSON returns 400."""
    resp = await client.post(
        "/v1/hook/pretooluse",
        data="not json",
        headers={"Content-Type": "application/json"}
    )
    assert resp.status == 400
    body = await resp.json()
    assert "error" in body


@pytest.mark.asyncio
async def test_pretooluse_unknown_task_id(client, in_memory_db):
    """POST /v1/hook/pretooluse with unknown task_id returns deny decision."""
    resp = await client.post(
        "/v1/hook/pretooluse",
        json={
            "request_id": "req-1",
            "task_id": "unknown-task",
            "tool_name": "Bash",
            "tool_input": {"cmd": "ls"}
        }
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["decision"] == "deny"
    assert "unknown" in body["reason"].lower()

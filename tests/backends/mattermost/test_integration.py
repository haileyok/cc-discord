"""Integration tests for the Mattermost backend with the platform-agnostic dispatcher.

Verifies that Mattermost posts (plain dicts) flow through the dispatcher,
task router, and listener without crashing — the core bug that was silently
swallowing non-command messages from Mattermost because _dispatch_message
expected Discord-style attribute access (.channel.id, .content, .author.bot).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest import mock

import pytest

from bridge.approvals import ApprovalRouter
from bridge.backends.mattermost.bot import MattermostBot, MattermostMessageAdapter
from bridge.listener import Listener
from bridge.server import make_message_dispatcher
from bridge.state import upsert_task
from bridge.tasks import TaskRegistry
from bridge.zellij import ZellijManager
from tests.fakes import FakePlatform, FakeZellij


class TestMattermostMessageAdapter:
    """Unit tests for the dict→MessageLike adapter."""

    def test_basic_fields(self):
        post = {
            "id": "msg1",
            "message": "hello world",
            "user_id": "user1",
            "channel_id": "chan1",
            "root_id": "thread1",
        }
        adapted = MattermostMessageAdapter(post, bot_user_id="bot123")

        assert adapted.content == "hello world"
        assert adapted.channel.id == "thread1"
        assert adapted.author.bot is False
        assert adapted.attachments == []
        assert adapted.id == "msg1"

    def test_bot_user_detected(self):
        post = {"id": "msg1", "message": "hi", "user_id": "bot123"}
        adapted = MattermostMessageAdapter(post, bot_user_id="bot123")

        assert adapted.author.bot is True

    def test_top_level_message_uses_post_id_as_channel(self):
        post = {"id": "msg1", "message": "hi", "user_id": "user1"}
        adapted = MattermostMessageAdapter(post)

        assert adapted.channel.id == "msg1"

    def test_thread_message_uses_root_id_as_channel(self):
        post = {
            "id": "msg2",
            "message": "reply",
            "user_id": "user1",
            "root_id": "msg1",
        }
        adapted = MattermostMessageAdapter(post)

        assert adapted.channel.id == "msg1"

    def test_created_at_from_create_at_field(self):
        post = {
            "id": "msg1",
            "message": "hi",
            "user_id": "user1",
            "create_at": 1700000000000,
        }
        adapted = MattermostMessageAdapter(post)

        assert isinstance(adapted.created_at, datetime)
        assert adapted.created_at.tzinfo == timezone.utc

    def test_created_at_defaults_to_now(self):
        post = {"id": "msg1", "message": "hi", "user_id": "user1"}
        before = datetime.now(tz=timezone.utc)
        adapted = MattermostMessageAdapter(post)
        after = datetime.now(tz=timezone.utc)

        assert before <= adapted.created_at <= after

    def test_missing_message_defaults_to_empty(self):
        post = {"id": "msg1", "user_id": "user1"}
        adapted = MattermostMessageAdapter(post)

        assert adapted.content == ""


@pytest.mark.asyncio
class TestMattermostDispatcherIntegration:
    """Integration tests: Mattermost posts flowing through make_message_dispatcher."""

    async def test_adapted_message_reaches_task_router(
        self, in_memory_db, monkeypatch
    ):
        """A Mattermost thread reply to a bound task routes to zellij."""
        zellij = ZellijManager()

        async def mock_run(*argv, env=None, timeout=10.0):
            return (0, "", "")

        monkeypatch.setattr(zellij, "_run", mock_run)

        task_id = "task-mm-1"
        thread_id = "mm-thread-1"
        pane_id = "pane_mm_1"
        now = int(time.time())

        await upsert_task(
            in_memory_db, task_id, thread_id, "/tmp", "running",
            zellij_pane_id=pane_id,
            current_claude_session_id="sess-mm-1",
            current_transcript_path="/path/transcript",
            now=now,
        )

        platform = FakePlatform()
        task_registry = TaskRegistry(in_memory_db, platform, zellij)
        await task_registry.load_from_db()

        listener = Listener()
        listener_calls: list = []
        listener.deliver = lambda msg: listener_calls.append(msg) or __import__("asyncio").sleep(0)

        zellij_calls: list[dict] = []

        async def mock_write_to_pane(pane_id: str, text: str) -> None:
            zellij_calls.append({"pane_id": pane_id, "text": text})

        monkeypatch.setattr(zellij, "write_to_pane", mock_write_to_pane)

        approval_router = ApprovalRouter(platform, in_memory_db, timeout=0.1)
        dispatcher = make_message_dispatcher(approval_router, task_registry, listener)

        post = {
            "id": "msg2",
            "message": "please fix the bug",
            "user_id": "user1",
            "channel_id": "chan1",
            "root_id": thread_id,
        }
        adapted = MattermostMessageAdapter(post, bot_user_id="bot123")
        await dispatcher(adapted)

        assert len(zellij_calls) == 1
        assert zellij_calls[0]["pane_id"] == pane_id
        assert "please fix the bug" in zellij_calls[0]["text"]
        assert len(listener_calls) == 0

    async def test_adapted_message_falls_through_to_listener(
        self, in_memory_db, monkeypatch
    ):
        """A Mattermost message not in a task thread falls through to listener."""
        zellij = ZellijManager()

        async def mock_run(*argv, env=None, timeout=10.0):
            return (0, "", "")

        monkeypatch.setattr(zellij, "_run", mock_run)

        platform = FakePlatform()
        task_registry = TaskRegistry(in_memory_db, platform, zellij)
        await task_registry.load_from_db()

        listener = Listener()
        listener_calls: list = []

        async def track_deliver(msg):
            listener_calls.append(msg)

        listener.deliver = track_deliver

        approval_router = ApprovalRouter(platform, in_memory_db, timeout=0.1)
        dispatcher = make_message_dispatcher(approval_router, task_registry, listener)

        post = {
            "id": "msg1",
            "message": "general question",
            "user_id": "user1",
            "channel_id": "chan1",
        }
        adapted = MattermostMessageAdapter(post, bot_user_id="bot123")
        await dispatcher(adapted)

        assert len(listener_calls) == 1
        assert listener_calls[0].content == "general question"

    async def test_adapted_bot_message_not_treated_as_approval(
        self, in_memory_db, monkeypatch
    ):
        """A bot's own message doesn't resolve approvals (author.bot=True)."""
        zellij = ZellijManager()

        async def mock_run(*argv, env=None, timeout=10.0):
            return (0, "", "")

        monkeypatch.setattr(zellij, "_run", mock_run)

        platform = FakePlatform()
        task_registry = TaskRegistry(in_memory_db, platform, zellij)
        await task_registry.load_from_db()

        listener = Listener()
        listener_calls: list = []

        async def track_deliver(msg):
            listener_calls.append(msg)

        listener.deliver = track_deliver

        approval_router = ApprovalRouter(platform, in_memory_db, timeout=0.1)
        dispatcher = make_message_dispatcher(approval_router, task_registry, listener)

        post = {
            "id": "msg1",
            "message": "echo from bot",
            "user_id": "bot123",
            "channel_id": "chan1",
        }
        adapted = MattermostMessageAdapter(post, bot_user_id="bot123")
        await dispatcher(adapted)

        assert len(listener_calls) == 1
        assert listener_calls[0].author.bot is True


@pytest.mark.asyncio
class TestMattermostBotFullFlow:
    """End-to-end flow tests: Mattermost bot dispatching commands and messages."""

    async def test_start_command_spawns_task(self):
        """!start triggers spawn_task via the Mattermost command path."""
        registry = mock.MagicMock()
        task = mock.MagicMock()
        task.task_id = "11111111-aaaa-bbbb-cccc-dddddddddddd"
        registry.spawn_task = mock.AsyncMock(return_value=task)
        registry.get_by_task_id = mock.MagicMock(return_value=task)
        task.current_claude_session_id = "sess-1"
        registry.write_initial_prompt = mock.AsyncMock()

        bot = MattermostBot("https://mm.example.com", "token", "channel-id")
        bot._bot_user_id = "bot123"
        bot._registry = registry
        bot.post = mock.AsyncMock(return_value=["msg1"])

        post = {
            "id": "msg1",
            "message": "!start /workspace hello world",
            "user_id": "user1",
            "channel_id": "channel-id",
            "root_id": None,
        }

        await bot._handle_event("posted", {"post": post})

        registry.spawn_task.assert_called_once_with(cwd="/workspace", prompt="hello world")
        bot.post.assert_called_once()
        result_msg = bot.post.call_args[0][0]
        assert "Started" in result_msg or "✅" in result_msg

    async def test_list_command_returns_empty(self):
        """!list with no active tasks returns appropriate message."""
        registry = mock.MagicMock()
        registry.list_tasks = mock.AsyncMock(return_value=[])

        bot = MattermostBot("https://mm.example.com", "token", "channel-id")
        bot._bot_user_id = "bot123"
        bot._registry = registry
        bot.post = mock.AsyncMock(return_value=["msg1"])

        post = {
            "id": "msg1",
            "message": "!list",
            "user_id": "user1",
            "channel_id": "channel-id",
        }

        await bot._handle_event("posted", {"post": post})

        bot.post.assert_called_once()
        assert "No active tasks" in bot.post.call_args[0][0]

    async def test_non_command_message_reaches_on_message_callback(self):
        """Non-command messages in a thread reach on_message callback."""
        on_msg = mock.AsyncMock()
        registry = mock.MagicMock()

        bot = MattermostBot(
            "https://mm.example.com", "token", "channel-id",
            on_message=on_msg,
        )
        bot._bot_user_id = "bot123"
        bot._registry = registry

        post = {
            "id": "msg2",
            "message": "fix the bug please",
            "user_id": "user1",
            "channel_id": "channel-id",
            "root_id": "msg1",
        }

        await bot._handle_event("posted", {"post": post})

        on_msg.assert_called_once_with(post)

    async def test_non_command_message_without_registry_reaches_callback(self):
        """Non-command messages work when no registry is bound."""
        on_msg = mock.AsyncMock()

        bot = MattermostBot(
            "https://mm.example.com", "token", "channel-id",
            on_message=on_msg,
        )
        bot._bot_user_id = "bot123"

        post = {
            "id": "msg1",
            "message": "hello",
            "user_id": "user1",
            "channel_id": "channel-id",
        }

        await bot._handle_event("posted", {"post": post})

        on_msg.assert_called_once_with(post)

    async def test_start_with_prompt_includes_prompt_in_result(self):
        """!start /workspace hello passes prompt handling correctly."""
        from bridge.backends.mattermost.commands import dispatch_text_command, parse_text_command

        parsed = parse_text_command("!start /workspace hello world")
        assert parsed is not None
        command, args = parsed
        assert command == "start"
        assert args == ["/workspace", "hello", "world"]

    async def test_unknown_command_returns_error(self):
        """Unknown !commands return error messages."""
        registry = mock.MagicMock()

        bot = MattermostBot("https://mm.example.com", "token", "channel-id")
        bot._bot_user_id = "bot123"
        bot._registry = registry
        bot.post = mock.AsyncMock(return_value=["msg1"])

        post = {
            "id": "msg1",
            "message": "!bogus",
            "user_id": "user1",
            "channel_id": "channel-id",
        }

        await bot._handle_event("posted", {"post": post})

        bot.post.assert_called_once()
        assert "Unknown command" in bot.post.call_args[0][0]


@pytest.mark.asyncio
class TestMattermostApprovalIntegration:
    """Approval resolution flow via adapted Mattermost messages."""

    async def test_approval_resolved_by_text_via_adapter(
        self, in_memory_db, monkeypatch
    ):
        """A Mattermost text reply resolves a pending approval via the dispatcher."""
        import asyncio
        from bridge.approvals import _PendingApproval

        zellij = ZellijManager()

        async def mock_run(*argv, env=None, timeout=10.0):
            return (0, "", "")

        monkeypatch.setattr(zellij, "_run", mock_run)

        task_id = "task-approval-mm"
        thread_id = "mm-thread-approval"
        pane_id = "pane_approval_mm"
        now = int(time.time())

        await upsert_task(
            in_memory_db, task_id, thread_id, "/tmp", "running",
            zellij_pane_id=pane_id,
            current_claude_session_id="sess-approval",
            current_transcript_path="/path/transcript",
            now=now,
        )

        platform = FakePlatform()
        task_registry = TaskRegistry(in_memory_db, platform, zellij)
        await task_registry.load_from_db()

        approval_router = ApprovalRouter(platform, in_memory_db, timeout=5.0)

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        pending = _PendingApproval(
            request_id="req-mm-1",
            task_id=task_id,
            tool_name="Bash",
            tool_input={},
            thread_id=thread_id,
            created_at=int(time.time()),
            future=fut,
        )
        async with approval_router._lock:
            approval_router._by_request_id["req-mm-1"] = pending

        listener = Listener()
        dispatcher = make_message_dispatcher(approval_router, task_registry, listener)

        post = {
            "id": "msg-reply",
            "message": "no, too dangerous",
            "user_id": "user1",
            "channel_id": "chan1",
            "root_id": thread_id,
        }
        adapted = MattermostMessageAdapter(post, bot_user_id="bot123")
        await dispatcher(adapted)

        assert fut.done()
        decision, reason = fut.result()
        assert decision == "deny"
        assert reason == "no, too dangerous"

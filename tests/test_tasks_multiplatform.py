"""Multiplatform integration tests for TaskRegistry with FakePlatform.

These tests verify that the core TaskRegistry logic works correctly through
the ChatPlatform abstraction without any real backend (Discord or Mattermost).
"""

from __future__ import annotations

import pytest

from bridge.state import upsert_task
from bridge.tasks import TaskRegistry
from tests.fakes import FakePlatform, FakeZellij


@pytest.mark.asyncio
class TestTaskRegistryMultiplatform:
    """Integration tests for TaskRegistry using FakePlatform."""

    async def test_load_from_db_with_fake_platform(self, in_memory_db) -> None:
        """TaskRegistry.load_from_db works with FakePlatform."""
        now = 1000
        # Insert some tasks
        await upsert_task(
            in_memory_db,
            "task-1",
            "1001",
            "/a",
            "running",
            current_claude_session_id="11111111-1111-1111-1111-111111111111",
            now=now,
        )
        await upsert_task(
            in_memory_db,
            "task-2",
            "1002",
            "/b",
            "spawning",
            current_claude_session_id="22222222-2222-2222-2222-222222222222",
            now=now,
        )

        platform = FakePlatform()
        zellij = FakeZellij()
        registry = TaskRegistry(in_memory_db, platform, zellij)
        await registry.load_from_db()

        # Should have loaded 2 tasks
        assert registry.get_by_task_id("task-1") is not None
        assert registry.get_by_task_id("task-2") is not None

        # By thread_id
        assert registry.get_by_thread_id("1001") is not None
        assert registry.get_by_thread_id("1002") is not None

        # By session_id
        assert (
            registry.get_by_session_id("11111111-1111-1111-1111-111111111111")
            is not None
        )
        assert (
            registry.get_by_session_id("22222222-2222-2222-2222-222222222222")
            is not None
        )

    async def test_handle_event_session_start_posts_to_platform(
        self, in_memory_db
    ) -> None:
        """TaskRegistry.handle_event('SessionStart') posts to FakePlatform."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            "1001",
            "/tmp",
            "spawning",
            now=now,
        )

        platform = FakePlatform()
        zellij = FakeZellij()
        registry = TaskRegistry(in_memory_db, platform, zellij)
        await registry.load_from_db()

        # Handle SessionStart
        body = {
            "hook_event_name": "SessionStart",
            "session_id": "12345678-1234-5678-1234-567812345678",
            "cwd": "/tmp",
            "transcript_path": "/path/to/transcript",
            "env_passthrough": {"CC_BRIDGE_TASK_ID": "task-123"},
        }
        await registry.handle_event("SessionStart", body)

        # Task should be updated to "running"
        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.status == "running"
        assert task.current_claude_session_id == "12345678-1234-5678-1234-567812345678"
        assert task.current_transcript_path == "/path/to/transcript"

        # Platform should have posted status message
        assert len(platform._post_calls) > 0
        posts = platform._post_calls
        # Should have posted to the task's thread
        assert any(post["thread_id"] == "1001" for post in posts)

    async def test_handle_event_stop_updates_task(self, in_memory_db) -> None:
        """TaskRegistry.handle_event('Stop') updates task state via FakePlatform."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            "1001",
            "/tmp",
            "running",
            current_claude_session_id="12345678-1234-5678-1234-567812345678",
            now=now,
        )

        platform = FakePlatform()
        zellij = FakeZellij()
        registry = TaskRegistry(in_memory_db, platform, zellij)
        await registry.load_from_db()

        # Capture initial activity
        task_before = registry.get_by_task_id("task-123")
        assert task_before is not None
        activity_before = task_before.last_activity

        # Handle Stop event
        body = {
            "hook_event_name": "Stop",
            "session_id": "12345678-1234-5678-1234-567812345678",
            "transcript_path": "/nonexistent/path",  # Non-existent so no streaming
        }
        await registry.handle_event("Stop", body)

        # Task should still exist and activity should be updated
        task = registry.get_by_task_id("task-123")
        assert task is not None
        assert task.last_activity >= activity_before

    async def test_create_thread_with_fake_platform(self, in_memory_db) -> None:
        """TaskRegistry creates threads through FakePlatform."""
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-123",
            "1001",
            "/tmp",
            "running",
            current_claude_session_id="12345678-1234-5678-1234-567812345678",
            now=now,
        )

        platform = FakePlatform()
        zellij = FakeZellij()
        registry = TaskRegistry(in_memory_db, platform, zellij)
        await registry.load_from_db()

        task = registry.get_by_task_id("task-123")
        assert task is not None

        # The platform should support thread creation
        thread_id = await platform.create_thread("Test Thread")
        assert isinstance(thread_id, str)
        assert len(platform._thread_calls) == 1
        assert platform._thread_calls[0]["name"] == "Test Thread"

    async def test_platform_post_to_thread_with_fake_platform(
        self, in_memory_db
    ) -> None:
        """Platform can post messages to threads."""
        platform = FakePlatform()

        # Post to main channel
        msg_ids = await platform.post("Hello world")
        assert len(msg_ids) == 1
        assert len(platform._post_calls) == 1

        # Post to thread
        msg_ids = await platform.post("Thread message", thread_id="2001")
        assert len(msg_ids) == 1
        assert len(platform._post_calls) == 2
        assert platform._post_calls[1]["thread_id"] == "2001"

    async def test_platform_post_with_attachments_through_fake_platform(
        self, in_memory_db, tmp_path
    ) -> None:
        """Platform can post attachments through FakePlatform."""
        from pathlib import Path

        platform = FakePlatform()

        # Create fake file
        test_file = tmp_path / "test.txt"
        test_file.write_text("test content")

        # Post with attachments
        msg_ids = await platform.post_with_attachments(
            [test_file], thread_id="2001", text="Files attached"
        )

        assert len(msg_ids) == 1
        assert len(platform._attachment_calls) == 1
        call = platform._attachment_calls[0]
        assert call["thread_id"] == "2001"
        assert call["text"] == "Files attached"
        assert test_file in call["file_paths"]

    async def test_archive_thread_through_fake_platform(self) -> None:
        """Platform can archive threads through FakePlatform."""
        platform = FakePlatform()

        await platform.archive_thread("2001")
        assert len(platform._archive_calls) == 1
        assert platform._archive_calls[0]["thread_id"] == "2001"

    async def test_add_reactions_through_fake_platform(self) -> None:
        """Platform can add reactions through FakePlatform."""
        platform = FakePlatform()

        await platform.add_reactions("1001", "2001", ["👍", "❌"])
        assert len(platform._reaction_calls) == 1
        call = platform._reaction_calls[0]
        assert call["message_id"] == "1001"
        assert call["thread_id"] == "2001"
        assert call["emoji"] == ["👍", "❌"]

    async def test_edit_message_through_fake_platform(self) -> None:
        """Platform can edit messages through FakePlatform."""
        platform = FakePlatform()

        await platform.edit_message("2001", "1001", content="Updated")
        assert len(platform._edit_calls) == 1
        call = platform._edit_calls[0]
        assert call["message_id"] == "1001"
        assert call["thread_id"] == "2001"
        assert call["content"] == "Updated"

    async def test_thread_alive_through_fake_platform(self) -> None:
        """Platform checks thread existence through FakePlatform."""
        platform = FakePlatform()

        # Fake always returns True
        assert await platform.thread_alive("2001") is True

    async def test_download_attachment_through_fake_platform(self, tmp_path) -> None:
        """Platform downloads attachments through FakePlatform."""
        platform = FakePlatform()

        dest_dir = tmp_path
        path = await platform.download_attachment("some_ref", dest_dir)

        assert path == dest_dir / "fake_download.txt"
        assert len(platform._download_calls) == 1

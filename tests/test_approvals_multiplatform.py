"""Multiplatform integration tests for ApprovalRouter with FakePlatform.

These tests verify that the ApprovalRouter works correctly through the ChatPlatform
abstraction without any real backend (Discord or Mattermost).
"""

from __future__ import annotations

import asyncio

import pytest

from bridge.approvals import ApprovalRouter
from bridge.state import upsert_task
from tests.fakes import FakePlatform


@pytest.mark.asyncio
class TestApprovalRouterMultiplatform:
    """Integration tests for ApprovalRouter using FakePlatform."""

    async def test_request_permission_with_fake_platform(
        self, in_memory_db
    ) -> None:
        """ApprovalRouter.request_permission posts to FakePlatform."""
        platform = FakePlatform()

        # Create a task in the database
        now = 1000
        await upsert_task(
            in_memory_db,
            "task-1",
            "1001",
            "/tmp",
            "running",
            current_claude_session_id="12345678-1234-5678-1234-567812345678",
            now=now,
        )

        router = ApprovalRouter(platform, in_memory_db, timeout=10.0)

        async def trigger_approval():
            """Simulate approval after a short delay."""
            await asyncio.sleep(0.1)
            # Get the pending request and resolve it
            pending_list = list(router._by_request_id.values())
            if pending_list:
                pending = pending_list[0]
                if pending.message_id:
                    await router.resolve_by_reaction(pending.message_id, "✅", user_is_self_bot=False)

        task = asyncio.create_task(trigger_approval())

        try:
            decision, reason = await router.request_permission(
                request_id="req-1",
                task_id="task-1",
                thread_id="1001",
                tool_name="Bash",
                tool_input={"cmd": "ls"},
            )

            await task
            assert decision == "allow"
            assert "reaction" in reason.lower() or "approved" in reason.lower()

            # Platform should have posted the question
            assert len(platform._post_calls) > 0
            posts = platform._post_calls
            # Should have posted to the task's thread
            assert any(post["thread_id"] == "1001" for post in posts)
        finally:
            await task

    async def test_resolve_by_reaction_allow_with_fake_platform(
        self, in_memory_db
    ) -> None:
        """ApprovalRouter.resolve_by_reaction with ✅ returns allow."""
        platform = FakePlatform()

        now = 1000
        await upsert_task(
            in_memory_db,
            "task-1",
            "1001",
            "/tmp",
            "running",
            now=now,
        )

        router = ApprovalRouter(platform, in_memory_db, timeout=10.0)

        async def trigger_reaction():
            await asyncio.sleep(0.1)
            pending_list = list(router._by_request_id.values())
            if pending_list:
                pending = pending_list[0]
                if pending.message_id:
                    await router.resolve_by_reaction(pending.message_id, "✅", user_is_self_bot=False)

        task = asyncio.create_task(trigger_reaction())

        try:
            decision, reason = await router.request_permission(
                request_id="req-1",
                task_id="task-1",
                thread_id="1001",
                tool_name="Bash",
                tool_input={"cmd": "ls"},
            )

            await task
            assert decision == "allow"
        finally:
            await task

    async def test_resolve_by_reaction_deny_with_fake_platform(
        self, in_memory_db
    ) -> None:
        """ApprovalRouter.resolve_by_reaction with ❌ returns deny."""
        platform = FakePlatform()

        now = 1000
        await upsert_task(
            in_memory_db,
            "task-1",
            "1001",
            "/tmp",
            "running",
            now=now,
        )

        router = ApprovalRouter(platform, in_memory_db, timeout=10.0)

        async def trigger_reaction():
            await asyncio.sleep(0.1)
            pending_list = list(router._by_request_id.values())
            if pending_list:
                pending = pending_list[0]
                if pending.message_id:
                    await router.resolve_by_reaction(pending.message_id, "❌", False)

        task = asyncio.create_task(trigger_reaction())

        try:
            decision, reason = await router.request_permission(
                request_id="req-1",
                task_id="task-1",
                thread_id="1001",
                tool_name="Bash",
                tool_input={"cmd": "ls"},
            )

            await task
            assert decision == "deny"
        finally:
            await task

    async def test_resolve_by_text_with_fake_platform(self, in_memory_db) -> None:
        """ApprovalRouter.resolve_by_text resolves pending approvals."""
        platform = FakePlatform()

        router = ApprovalRouter(platform, in_memory_db, timeout=10.0)

        # Create a pending approval request in the background
        async def trigger_denial():
            await asyncio.sleep(0.1)
            # Try to resolve via text in the same thread
            await router.resolve_by_text(
                "2001", "nope", author_is_bot=False
            )

        task = asyncio.create_task(trigger_denial())

        try:
            # Start a request in a different thread to avoid conflicts
            decision, reason = await asyncio.wait_for(
                router.request_permission(
                    request_id="req-1",
                    task_id="task-1",
                    thread_id="2001",
                    tool_name="Bash",
                    tool_input={"cmd": "ls"},
                ),
                timeout=5.0,
            )

            await task
            assert decision == "deny"
        finally:
            await task

    async def test_resolve_tui_by_text_with_fake_platform(
        self, in_memory_db
    ) -> None:
        """ApprovalRouter.resolve_tui_by_text handles TUI prompts."""
        platform = FakePlatform()

        router = ApprovalRouter(platform, in_memory_db, timeout=10.0)

        # TUI prompt resolution should handle text responses
        result = await router.resolve_tui_by_text(
            "thread-1", "option 1", author_is_bot=False
        )

        # This method may return True/False depending on whether a TUI is pending
        # We just verify it doesn't crash
        assert isinstance(result, bool)

    async def test_fake_platform_posts_question_with_reactions(
        self, in_memory_db
    ) -> None:
        """Fake platform correctly tracks approval questions and reactions."""
        platform = FakePlatform()

        now = 1000
        await upsert_task(
            in_memory_db,
            "task-1",
            "1001",
            "/tmp",
            "running",
            now=now,
        )

        router = ApprovalRouter(platform, in_memory_db, timeout=10.0)

        async def trigger_approval():
            await asyncio.sleep(0.1)
            pending_list = list(router._by_request_id.values())
            if pending_list:
                pending = pending_list[0]
                if pending.message_id:
                    await router.resolve_by_reaction(pending.message_id, "✅", user_is_self_bot=False)

        task = asyncio.create_task(trigger_approval())

        try:
            decision, reason = await router.request_permission(
                request_id="req-1",
                task_id="task-1",
                thread_id="1001",
                tool_name="Bash",
                tool_input={"cmd": "ls"},
            )

            await task

            # Platform should have posted the approval question
            posts = platform._post_calls
            assert len(posts) > 0

            # Platform should have added reactions
            reactions = platform._reaction_calls
            assert len(reactions) > 0
            # At least one reaction should be added
            reaction = reactions[0]
            assert set(reaction["emoji"]) >= {"✅", "❌"}

        finally:
            await task

    async def test_timeout_handling_with_fake_platform(
        self, in_memory_db
    ) -> None:
        """ApprovalRouter handles timeouts gracefully."""
        platform = FakePlatform()

        router = ApprovalRouter(platform, in_memory_db, timeout=0.2)

        # Request permission and don't respond — should timeout and return deny
        decision, reason = await router.request_permission(
            request_id="req-1",
            task_id="task-1",
            thread_id="2001",
            tool_name="Bash",
            tool_input={"cmd": "ls"},
        )

        assert decision == "deny"
        # The reason should contain "timed out" or similar
        assert any(
            word in reason.lower()
            for word in ["timeout", "timed"]
        ), f"Expected timeout-related reason, got: {reason}"

    async def test_multiple_platforms_independent(
        self, in_memory_db
    ) -> None:
        """Multiple FakePlatform instances maintain independent state."""
        platform1 = FakePlatform()
        platform2 = FakePlatform()

        # Post to first platform
        msg_ids1 = await platform1.post("Message 1")
        assert len(msg_ids1) == 1
        assert len(platform1._post_calls) == 1
        assert len(platform2._post_calls) == 0

        # Post to second platform
        msg_ids2 = await platform2.post("Message 2")
        assert len(msg_ids2) == 1
        assert len(platform1._post_calls) == 1
        assert len(platform2._post_calls) == 1

        # Verify independence
        assert platform1._post_calls[0]["content"] == "Message 1"
        assert platform2._post_calls[0]["content"] == "Message 2"

    async def test_approval_routing_through_platform_abstraction(
        self, in_memory_db
    ) -> None:
        """Full approval request-response cycle through ChatPlatform."""
        platform = FakePlatform()

        now = 1000
        await upsert_task(
            in_memory_db,
            "task-multi",
            "thread-multi",
            "/tmp",
            "running",
            now=now,
        )

        router = ApprovalRouter(platform, in_memory_db, timeout=10.0)

        async def approve_request():
            await asyncio.sleep(0.1)
            # Find and approve the pending request
            for req_id, pending in router._by_request_id.items():
                if pending.message_id:
                    await router.resolve_by_reaction(
                        pending.message_id, "✅", user_is_self_bot=False
                    )
                    break

        task = asyncio.create_task(approve_request())

        try:
            decision, reason = await router.request_permission(
                request_id="req-multi",
                task_id="task-multi",
                thread_id="thread-multi",
                tool_name="Edit",
                tool_input={"path": "/tmp/file.txt", "old": "x", "new": "y"},
            )

            await task
            assert decision == "allow"
            assert len(platform._post_calls) > 0
            assert len(platform._reaction_calls) > 0

        finally:
            if not task.done():
                await task

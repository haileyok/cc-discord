from __future__ import annotations

import pytest
from mcp import Client

import bridge.mcp_server as mcp_server


@pytest.mark.asyncio
async def test_mcp_server_registers_only_explicit_tools(monkeypatch):
    calls = []

    async def rpc(tool, arguments):
        calls.append((tool, arguments))
        return {"tool": tool, "arguments": arguments}

    monkeypatch.setattr(mcp_server, "_rpc", rpc)
    async with Client(mcp_server.server) as client:
        listed = await client.list_tools()
        names = {tool.name for tool in listed.tools}
        assert names == {
            "bridge_health", "bridge_list_tasks", "bridge_task_status",
            "bridge_compact_task", "bridge_cancel_turn", "bridge_promote_task",
            "bridge_set_model", "bridge_set_facet",
            "bridge_set_effort", "bridge_stop_task", "bridge_clear_context",
            "slack_read_thread", "slack_read_channel_history", "slack_search_task_messages",
            "slack_post_message", "slack_upload_file", "slack_download_thread_file",
            "slack_create_canvas", "slack_edit_canvas", "slack_set_channel_metadata",
            "slack_invite_participants", "slack_remove_participants", "slack_add_bookmark",
            "slack_remove_bookmark", "slack_schedule_message", "slack_list_scheduled_messages",
            "slack_cancel_scheduled_message", "slack_create_poll", "slack_create_approval",
            "slack_get_poll_results", "slack_edit_message", "slack_add_reaction",
            "slack_remove_reaction",
            "slack_delete_message",
        }
        result = await client.call_tool("bridge_task_status", {"task_id": "task-1"})
        assert result.structured_content == {"tool": "bridge_task_status", "arguments": {"task_id": "task-1"}}
    assert calls == [("bridge_task_status", {"task_id": "task-1"})]


@pytest.mark.asyncio
async def test_destructive_tool_schema_defaults_confirmation_false(monkeypatch):
    calls = []

    async def rpc(tool, arguments):
        calls.append((tool, arguments)); return {"ok": True}

    monkeypatch.setattr(mcp_server, "_rpc", rpc)
    async with Client(mcp_server.server) as client:
        await client.call_tool("bridge_stop_task", {"task_id": "task-1"})
    assert calls == [("bridge_stop_task", {"task_id": "task-1", "confirm": False})]

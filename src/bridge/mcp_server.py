"""Local stdio MCP server that proxies explicit tools to the live bridge."""
from __future__ import annotations

import os
import uuid
from typing import Any

import aiohttp
from mcp.server import MCPServer

from bridge.mcp_auth import MCP_TOKEN_FILE, load_or_create_mcp_token

BRIDGE_RPC_URL = os.environ.get("BRIDGE_MCP_RPC_URL", "http://127.0.0.1:8787/v1/mcp/call")
server = MCPServer(
    "slack_bridge",
    title="Slack Bridge",
    description="Owner-scoped Slack and Polytoken bridge operations.",
    instructions="Use current task/thread scope by default. Destructive tools require confirm=true.",
)


async def _rpc(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    token = load_or_create_mcp_token(MCP_TOKEN_FILE)
    payload = {"tool": tool, "arguments": arguments, "request_id": str(uuid.uuid4())}
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            BRIDGE_RPC_URL,
            json=payload,
            headers={"Authorization": f"Bearer {token}", "X-Bridge-Origin": "polytoken-mcp"},
        ) as response:
            data = await response.json(content_type=None)
            if response.status >= 400 or not data.get("ok"):
                error = data.get("error") if isinstance(data, dict) else None
                code = str((error or {}).get("code") or "bridge_rpc_failed")
                message = str((error or {}).get("message") or "Bridge MCP request failed")
                raise RuntimeError(f"{code}: {message}")
            return data["result"]


@server.tool()
async def bridge_health() -> dict[str, Any]:
    """Return sanitized Slack bridge readiness and active task count."""
    return await _rpc("bridge_health", {})


@server.tool()
async def bridge_list_tasks() -> dict[str, Any]:
    """List active Polytoken tasks owned by the configured Slack owner."""
    return await _rpc("bridge_list_tasks", {})


@server.tool()
async def bridge_task_status(task_id: str) -> dict[str, Any]:
    """Return sanitized state for one owned task, including current turn/todos."""
    return await _rpc("bridge_task_status", {"task_id": task_id})


@server.tool()
async def bridge_compact_task(task_id: str) -> dict[str, Any]:
    """Request or queue context compaction for one owned task."""
    return await _rpc("bridge_compact_task", {"task_id": task_id})


@server.tool()
async def bridge_set_model(task_id: str, model: str, reasoning_effort: str | None = None) -> dict[str, Any]:
    """Set the active model and optional reasoning effort for one owned task."""
    return await _rpc("bridge_set_model", {"task_id": task_id, "model": model, "reasoning_effort": reasoning_effort})


@server.tool()
async def bridge_set_facet(task_id: str, facet: str) -> dict[str, Any]:
    """Switch the active facet for one owned task."""
    return await _rpc("bridge_set_facet", {"task_id": task_id, "facet": facet})


@server.tool()
async def bridge_set_effort(task_id: str, effort: str) -> dict[str, Any]:
    """Set reasoning effort for the active model on one owned task."""
    return await _rpc("bridge_set_effort", {"task_id": task_id, "effort": effort})


@server.tool()
async def bridge_stop_task(task_id: str, confirm: bool = False) -> dict[str, Any]:
    """Stop and terminate an owned task. Requires confirm=true."""
    return await _rpc("bridge_stop_task", {"task_id": task_id, "confirm": confirm})


@server.tool()
async def bridge_clear_context(task_id: str, confirm: bool = False) -> dict[str, Any]:
    """Clear an idle task's context. Requires confirm=true."""
    return await _rpc("bridge_clear_context", {"task_id": task_id, "confirm": confirm})


@server.tool()
async def slack_read_thread(task_id: str, limit: int = 100) -> dict[str, Any]:
    """Read a bounded, sanitized history of an owned task's Slack thread."""
    return await _rpc("slack_read_thread", {"task_id": task_id, "limit": limit})


@server.tool()
async def slack_post_message(task_id: str, text: str) -> dict[str, Any]:
    """Post a message in an owned task thread."""
    return await _rpc("slack_post_message", {"task_id": task_id, "text": text})


@server.tool()
async def slack_edit_message(task_id: str, message_ts: str, text: str) -> dict[str, Any]:
    """Edit a bot-authored message within an owned task thread."""
    return await _rpc("slack_edit_message", {"task_id": task_id, "message_ts": message_ts, "text": text})


@server.tool()
async def slack_add_reaction(task_id: str, message_ts: str, emoji: str) -> dict[str, Any]:
    """Add a reaction to a message within an owned task thread."""
    return await _rpc("slack_add_reaction", {"task_id": task_id, "message_ts": message_ts, "emoji": emoji})


@server.tool()
async def slack_remove_reaction(task_id: str, message_ts: str, emoji: str) -> dict[str, Any]:
    """Remove the bot's reaction from a message within an owned task thread."""
    return await _rpc("slack_remove_reaction", {"task_id": task_id, "message_ts": message_ts, "emoji": emoji})


@server.tool()
async def slack_delete_message(task_id: str, message_ts: str, confirm: bool = False) -> dict[str, Any]:
    """Delete a bot-authored message in an owned task thread. Requires confirm=true."""
    return await _rpc("slack_delete_message", {"task_id": task_id, "message_ts": message_ts, "confirm": confirm})


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()

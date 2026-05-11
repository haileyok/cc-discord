"""Tests for the event.py hook script."""

import asyncio
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from aiohttp import test_utils, web


@pytest.fixture
async def fake_bridge():
    """Create a fake bridge server for testing (using test_utils.TestServer)."""
    seen = []

    async def handle_hook_event(req):
        seen.append(await req.json())
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/v1/hook/event", handle_hook_event)

    server = test_utils.TestServer(app)
    await server.start_server()

    base_url = f"http://{server.host}:{server.port}"

    try:
        yield {"url": base_url, "seen": seen}
    finally:
        await server.close()


async def _run_hook(
    payload: dict,
    bridge_url: str | None = None,
    env_overrides: dict | None = None,
) -> subprocess.CompletedProcess:
    """
    Run the event.py hook with the given payload and environment.
    Runs in an executor to avoid blocking the event loop.
    Returns the completed process.
    """
    stdin_data = json.dumps(payload)

    # Build environment - start with minimal env to avoid leaking real home
    import os

    env = {
        "PATH": os.environ.get("PATH", ""),
    }
    if bridge_url:
        env["BRIDGE_URL"] = bridge_url
    if env_overrides:
        env.update(env_overrides)

    # Run the hook script in an executor to avoid blocking the event loop
    hook_path = Path(__file__).parent.parent / "hooks" / "event.py"

    def _run():
        return subprocess.run(
            ["python3", str(hook_path)],
            input=stdin_data.encode("utf-8"),
            capture_output=True,
            env=env,
        )

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as executor:
        result = await loop.run_in_executor(executor, _run)
    return result


@pytest.mark.asyncio
async def test_session_start_event(fake_bridge):
    """
    Hook forwards SessionStart event to bridge.
    Hook exits 0; bridge sees POST with matching hook_event_name.
    """
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "test-session-1",
        "cwd": "/tmp/work",
        "transcript_path": "/tmp/transcript.txt",
    }

    result = await _run_hook(payload, bridge_url=fake_bridge["url"])

    assert result.returncode == 0
    assert len(fake_bridge["seen"]) == 1
    body = fake_bridge["seen"][0]
    assert body["hook_event_name"] == "SessionStart"
    assert body["session_id"] == "test-session-1"
    assert body["cwd"] == "/tmp/work"


@pytest.mark.asyncio
async def test_user_prompt_submit_event(fake_bridge):
    """Test UserPromptSubmit event is forwarded correctly."""
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "test-session-2",
        "cwd": "/tmp/work",
    }

    result = await _run_hook(payload, bridge_url=fake_bridge["url"])

    assert result.returncode == 0
    assert len(fake_bridge["seen"]) == 1
    body = fake_bridge["seen"][0]
    assert body["hook_event_name"] == "UserPromptSubmit"


@pytest.mark.asyncio
async def test_post_tool_use_event(fake_bridge):
    """Test PostToolUse event is forwarded correctly."""
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "test-session-3",
        "cwd": "/tmp/work",
    }

    result = await _run_hook(payload, bridge_url=fake_bridge["url"])

    assert result.returncode == 0
    assert len(fake_bridge["seen"]) == 1
    body = fake_bridge["seen"][0]
    assert body["hook_event_name"] == "PostToolUse"


@pytest.mark.asyncio
async def test_post_tool_use_failure_event(fake_bridge):
    """Test PostToolUseFailure event is forwarded correctly."""
    payload = {
        "hook_event_name": "PostToolUseFailure",
        "session_id": "test-session-4",
        "cwd": "/tmp/work",
    }

    result = await _run_hook(payload, bridge_url=fake_bridge["url"])

    assert result.returncode == 0
    assert len(fake_bridge["seen"]) == 1
    body = fake_bridge["seen"][0]
    assert body["hook_event_name"] == "PostToolUseFailure"


@pytest.mark.asyncio
async def test_stop_event(fake_bridge):
    """Test Stop event is forwarded correctly."""
    payload = {
        "hook_event_name": "Stop",
        "session_id": "test-session-5",
        "cwd": "/tmp/work",
    }

    result = await _run_hook(payload, bridge_url=fake_bridge["url"])

    assert result.returncode == 0
    assert len(fake_bridge["seen"]) == 1
    body = fake_bridge["seen"][0]
    assert body["hook_event_name"] == "Stop"


@pytest.mark.asyncio
async def test_notification_event(fake_bridge):
    """Test Notification event is forwarded correctly."""
    payload = {
        "hook_event_name": "Notification",
        "session_id": "test-session-6",
        "cwd": "/tmp/work",
    }

    result = await _run_hook(payload, bridge_url=fake_bridge["url"])

    assert result.returncode == 0
    assert len(fake_bridge["seen"]) == 1
    body = fake_bridge["seen"][0]
    assert body["hook_event_name"] == "Notification"


@pytest.mark.asyncio
async def test_session_end_event(fake_bridge):
    """Test SessionEnd event is forwarded correctly."""
    payload = {
        "hook_event_name": "SessionEnd",
        "session_id": "test-session-7",
        "cwd": "/tmp/work",
    }

    result = await _run_hook(payload, bridge_url=fake_bridge["url"])

    assert result.returncode == 0
    assert len(fake_bridge["seen"]) == 1
    body = fake_bridge["seen"][0]
    assert body["hook_event_name"] == "SessionEnd"


@pytest.mark.asyncio
async def test_subagent_stop_event(fake_bridge):
    """Test SubagentStop event is forwarded correctly."""
    payload = {
        "hook_event_name": "SubagentStop",
        "session_id": "test-session-8",
        "cwd": "/tmp/work",
    }

    result = await _run_hook(payload, bridge_url=fake_bridge["url"])

    assert result.returncode == 0
    assert len(fake_bridge["seen"]) == 1
    body = fake_bridge["seen"][0]
    assert body["hook_event_name"] == "SubagentStop"


@pytest.mark.asyncio
async def test_pre_compact_event(fake_bridge):
    """Test PreCompact event is forwarded correctly."""
    payload = {
        "hook_event_name": "PreCompact",
        "session_id": "test-session-9",
        "cwd": "/tmp/work",
    }

    result = await _run_hook(payload, bridge_url=fake_bridge["url"])

    assert result.returncode == 0
    assert len(fake_bridge["seen"]) == 1
    body = fake_bridge["seen"][0]
    assert body["hook_event_name"] == "PreCompact"


@pytest.mark.asyncio
async def test_env_passthrough_both_vars(fake_bridge):
    """
    When CC_DISCORD_TASK_ID and CLAUDE_PROJECT_DIR are set,
    both are included in body["env_passthrough"].
    """
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "test-session-10",
        "cwd": "/tmp/work",
    }

    result = await _run_hook(
        payload,
        bridge_url=fake_bridge["url"],
        env_overrides={
            "CC_DISCORD_TASK_ID": "task-abc",
            "CLAUDE_PROJECT_DIR": "/work",
        },
    )

    assert result.returncode == 0
    assert len(fake_bridge["seen"]) == 1
    body = fake_bridge["seen"][0]
    assert "env_passthrough" in body
    assert body["env_passthrough"]["CC_DISCORD_TASK_ID"] == "task-abc"
    assert body["env_passthrough"]["CLAUDE_PROJECT_DIR"] == "/work"


@pytest.mark.asyncio
async def test_env_passthrough_partial(fake_bridge):
    """
    When only CC_DISCORD_TASK_ID is set,
    only that var appears in env_passthrough.
    """
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "test-session-11",
        "cwd": "/tmp/work",
    }

    result = await _run_hook(
        payload,
        bridge_url=fake_bridge["url"],
        env_overrides={
            "CC_DISCORD_TASK_ID": "task-xyz",
        },
    )

    assert result.returncode == 0
    assert len(fake_bridge["seen"]) == 1
    body = fake_bridge["seen"][0]
    assert "env_passthrough" in body
    assert body["env_passthrough"]["CC_DISCORD_TASK_ID"] == "task-xyz"
    assert "CLAUDE_PROJECT_DIR" not in body["env_passthrough"]


@pytest.mark.asyncio
async def test_env_passthrough_none_omitted(fake_bridge):
    """
    When neither CC_DISCORD_TASK_ID nor CLAUDE_PROJECT_DIR are set,
    env_passthrough key is omitted entirely.
    """
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "test-session-12",
        "cwd": "/tmp/work",
    }

    result = await _run_hook(
        payload,
        bridge_url=fake_bridge["url"],
        env_overrides={},
    )

    assert result.returncode == 0
    assert len(fake_bridge["seen"]) == 1
    body = fake_bridge["seen"][0]
    assert "env_passthrough" not in body


@pytest.mark.asyncio
async def test_bridge_unreachable(fake_bridge):
    """
    Bridge is unreachable (closed port) → script exits 0; no exception.
    """
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "test-session-13",
        "cwd": "/tmp/work",
    }

    result = await _run_hook(
        payload,
        bridge_url="http://127.0.0.1:54321",  # closed port
    )

    assert result.returncode == 0
    # No exception should have been raised or printed


@pytest.mark.asyncio
async def test_malformed_stdin_json():
    """
    Malformed JSON on stdin → hook exits 0, no POST.
    """
    hook_path = Path(__file__).parent.parent / "hooks" / "event.py"

    def _run():
        return subprocess.run(
            ["python3", str(hook_path)],
            input=b"not json at all",
            capture_output=True,
            env={"PATH": "/usr/bin:/bin"},
        )

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as executor:
        result = await loop.run_in_executor(executor, _run)

    assert result.returncode == 0
    # No POST should have been attempted


@pytest.mark.asyncio
async def test_non_2xx_from_bridge(fake_bridge):
    """
    Bridge returns non-2xx status → script exits 0 (no fallback).
    """

    async def handle_error(req):
        return web.json_response({"error": "internal"}, status=500)

    app = web.Application()
    app.router.add_post("/v1/hook/event", handle_error)

    server = test_utils.TestServer(app)
    await server.start_server()

    try:
        bridge_url = f"http://{server.host}:{server.port}"

        payload = {
            "hook_event_name": "SessionStart",
            "session_id": "test-session-14",
            "cwd": "/tmp/work",
        }

        result = await _run_hook(
            payload,
            bridge_url=bridge_url,
        )

        assert result.returncode == 0
        # No exception, just silent exit
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_env_passthrough_new_cc_bridge_task_id(fake_bridge):
    """
    When CC_BRIDGE_TASK_ID is set, it appears in env_passthrough.
    """
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "test-session-bridge",
        "cwd": "/tmp/work",
    }

    result = await _run_hook(
        payload,
        bridge_url=fake_bridge["url"],
        env_overrides={
            "CC_BRIDGE_TASK_ID": "task-bridge-new",
        },
    )

    assert result.returncode == 0
    assert len(fake_bridge["seen"]) == 1
    body = fake_bridge["seen"][0]
    assert "env_passthrough" in body
    assert body["env_passthrough"]["CC_BRIDGE_TASK_ID"] == "task-bridge-new"


@pytest.mark.asyncio
async def test_env_passthrough_both_old_and_new_task_ids(fake_bridge):
    """
    When both CC_BRIDGE_TASK_ID and CC_DISCORD_TASK_ID are set,
    both appear in env_passthrough.
    """
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "test-session-both",
        "cwd": "/tmp/work",
    }

    result = await _run_hook(
        payload,
        bridge_url=fake_bridge["url"],
        env_overrides={
            "CC_BRIDGE_TASK_ID": "task-new-var",
            "CC_DISCORD_TASK_ID": "task-old-var",
        },
    )

    assert result.returncode == 0
    assert len(fake_bridge["seen"]) == 1
    body = fake_bridge["seen"][0]
    assert "env_passthrough" in body
    assert body["env_passthrough"]["CC_BRIDGE_TASK_ID"] == "task-new-var"
    assert body["env_passthrough"]["CC_DISCORD_TASK_ID"] == "task-old-var"

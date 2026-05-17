"""Tests for the notify-stop.py hook script."""

import asyncio
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiohttp import test_utils, web


@pytest.fixture
async def fake_bridge():
    """Create a fake bridge server for testing (using test_utils.TestServer)."""
    seen = []

    async def handle_notify(req):
        seen.append(await req.json())
        return web.json_response({"thread_id": 999, "message_id": 123})

    app = web.Application()
    app.router.add_post("/v1/notify", handle_notify)

    server = test_utils.TestServer(app)
    await server.start_server()

    # Extract base URL (without the path) from server
    base_url = f"http://{server.host}:{server.port}"

    try:
        yield {"url": base_url, "seen": seen}
    finally:
        await server.close()


@pytest.fixture
async def fake_webhook():
    """Create a fake webhook server for testing (using test_utils.TestServer)."""
    seen = []

    async def handle_webhook(req):
        seen.append(await req.json())
        return web.json_response({})

    app = web.Application()
    app.router.add_post("/", handle_webhook)

    server = test_utils.TestServer(app)
    await server.start_server()

    # Extract base URL for webhook
    base_url = f"http://{server.host}:{server.port}"

    try:
        yield {"url": base_url, "seen": seen}
    finally:
        await server.close()


def _build_transcript(last_user_timestamp: str) -> str:
    """Build a minimal JSONL transcript with one user message at the given timestamp."""
    user_entry = {
        "type": "user",
        "isSidechain": False,
        "isMeta": False,
        "message": {"content": "test prompt"},
        "timestamp": last_user_timestamp,
    }
    return json.dumps(user_entry)


async def _run_hook(
    payload: dict,
    transcript_path: Path | None = None,
    bridge_url: str | None = None,
    home_dir: Path | None = None,
) -> subprocess.CompletedProcess:
    """
    Run the notify-stop.py hook with the given payload and environment.
    Runs in an executor to avoid blocking the event loop.
    Returns the completed process.
    """
    # Build the stdin payload
    if transcript_path:
        payload["transcript_path"] = str(transcript_path)

    stdin_data = json.dumps(payload)

    # Build environment - start with minimal env to avoid leaking real home
    import os
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home_dir) if home_dir else "/tmp",
    }
    if bridge_url:
        env["BRIDGE_URL"] = bridge_url

    # Run the hook script in an executor to avoid blocking the event loop
    hook_path = Path(__file__).parent.parent / "hooks" / "notify-stop.py"

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
async def test_ac41_bridge_path_800s_ago(fake_bridge, tmp_path):
    """
    AC4.1 — bridge path: Last user message 800s ago.
    Hook exits 0; bridge sees exactly one POST with correct elapsed time.
    """
    # Build transcript with user message 800s ago
    now = datetime.now(timezone.utc)
    last_user_ts = (now - timedelta(seconds=800)).isoformat()
    transcript_path = tmp_path / "transcript.jsonl"
    transcript_path.write_text(_build_transcript(last_user_ts))

    # Build payload
    payload = {
        "session_id": "smoke",
        "cwd": "/tmp/aaa",
    }

    # Run hook with fake bridge
    result = await _run_hook(
        payload,
        transcript_path=transcript_path,
        bridge_url=fake_bridge["url"],
    )

    # Verify exit code
    assert result.returncode == 0

    # Verify bridge received exactly one POST
    assert len(fake_bridge["seen"]) == 1
    body = fake_bridge["seen"][0]

    # Check payload structure
    assert body["session_id"] == "smoke"
    assert body["cwd"] == "/tmp/aaa"
    assert "13m 20s" in body["message"]
    assert "Claude finished a long turn" in body["message"]


@pytest.mark.asyncio
async def test_ac42_short_turn_60s(fake_bridge, tmp_path):
    """
    AC4.2 — short turn: Last user message 60s ago.
    Hook exits 0; no POST made (below threshold).
    """
    # Build transcript with user message 60s ago
    now = datetime.now(timezone.utc)
    last_user_ts = (now - timedelta(seconds=60)).isoformat()
    transcript_path = tmp_path / "transcript.jsonl"
    transcript_path.write_text(_build_transcript(last_user_ts))

    # Build payload
    payload = {
        "session_id": "short",
        "cwd": "/tmp/bbb",
    }

    # Run hook with fake bridge
    result = await _run_hook(
        payload,
        transcript_path=transcript_path,
        bridge_url=fake_bridge["url"],
    )

    # Verify exit code
    assert result.returncode == 0

    # Verify no POST was made (below threshold)
    assert len(fake_bridge["seen"]) == 0


@pytest.mark.asyncio
async def test_ac43_bridge_down_webhook_fallback(
    fake_bridge, fake_webhook, tmp_path
):
    """
    AC4.3 — bridge down → webhook fallback.
    Bridge port is unused (nothing listening).
    Hook falls back to webhook, exits 0.
    """
    # Find a port that's open but no service is listening
    closed_port = 54321  # Use a high port unlikely to be in use

    # Build transcript with user message 800s ago
    now = datetime.now(timezone.utc)
    last_user_ts = (now - timedelta(seconds=800)).isoformat()
    transcript_path = tmp_path / "transcript.jsonl"
    transcript_path.write_text(_build_transcript(last_user_ts))

    # Create a fake home directory with webhook file
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    webhook_file = claude_dir / "discord-notify-webhook"
    webhook_file.write_text(fake_webhook["url"])

    # Build payload
    payload = {
        "session_id": "fallback-test",
        "cwd": "/tmp/ccc",
    }

    # Run hook with unreachable bridge and fake webhook
    result = await _run_hook(
        payload,
        transcript_path=transcript_path,
        bridge_url=f"http://127.0.0.1:{closed_port}",
        home_dir=fake_home,
    )

    # Verify exit code is 0 (even though bridge failed)
    assert result.returncode == 0

    # Verify webhook received the fallback POST
    assert len(fake_webhook["seen"]) == 1
    body = fake_webhook["seen"][0]
    assert "Claude finished a long turn" in body["content"]


@pytest.mark.asyncio
async def test_no_webhook_file_bridge_down(fake_bridge, tmp_path):
    """
    No webhook file present → bridge down → hook still exits 0.
    Nothing is recorded anywhere.
    """
    # Build transcript with user message 800s ago
    now = datetime.now(timezone.utc)
    last_user_ts = (now - timedelta(seconds=800)).isoformat()
    transcript_path = tmp_path / "transcript.jsonl"
    transcript_path.write_text(_build_transcript(last_user_ts))

    # Create a fake home with NO webhook file
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()

    # Build payload
    payload = {
        "session_id": "no-webhook",
        "cwd": "/tmp/ddd",
    }

    # Find an unused port (closed)
    closed_port = 54322

    # Run hook with unreachable bridge and no webhook file
    result = await _run_hook(
        payload,
        transcript_path=transcript_path,
        bridge_url=f"http://127.0.0.1:{closed_port}",
        home_dir=fake_home,
    )

    # Verify exit code is 0
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_malformed_transcript_jsonl_line(fake_bridge, tmp_path):
    """
    Malformed JSONL line in the middle → hook tolerates and finds last valid user message.
    """
    # Build transcript with some valid and invalid lines
    now = datetime.now(timezone.utc)
    last_user_ts = (now - timedelta(seconds=800)).isoformat()

    valid_entry = json.dumps(
        {
            "type": "user",
            "isSidechain": False,
            "isMeta": False,
            "message": {"content": "test prompt"},
            "timestamp": last_user_ts,
        }
    )

    transcript_path = tmp_path / "transcript.jsonl"
    transcript_path.write_text(
        valid_entry + "\n" + "this is not valid json\n" + valid_entry
    )

    # Build payload
    payload = {
        "session_id": "malformed",
        "cwd": "/tmp/eee",
    }

    # Run hook
    result = await _run_hook(
        payload,
        transcript_path=transcript_path,
        bridge_url=fake_bridge["url"],
    )

    # Verify exit code
    assert result.returncode == 0

    # Verify bridge still received the POST (found the last valid entry)
    assert len(fake_bridge["seen"]) == 1


@pytest.mark.asyncio
async def test_nonexistent_transcript_path(fake_bridge, tmp_path):
    """
    Non-existent transcript path → hook exits 0, no POST.
    """
    # Build payload with non-existent path
    payload = {
        "session_id": "nonexistent",
        "cwd": "/tmp/fff",
        "transcript_path": str(tmp_path / "does-not-exist.jsonl"),
    }

    # Run hook
    result = await _run_hook(
        payload,
        bridge_url=fake_bridge["url"],
    )

    # Verify exit code
    assert result.returncode == 0

    # Verify no POST was made
    assert len(fake_bridge["seen"]) == 0


@pytest.mark.asyncio
async def test_malformed_stdin_json(fake_bridge):
    """
    Stdin is malformed JSON → hook exits 0 silently.
    """
    # Run hook with invalid JSON by passing empty/malformed payload
    # We need to call the subprocess directly here since we're testing malformed input
    hook_path = Path(__file__).parent.parent / "hooks" / "notify-stop.py"

    def _run():
        return subprocess.run(
            ["python3", str(hook_path)],
            input=b"this is not json",
            capture_output=True,
            env={"BRIDGE_URL": fake_bridge["url"], "HOME": "/tmp", "PATH": "/usr/bin:/bin"},
        )

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as executor:
        result = await loop.run_in_executor(executor, _run)

    # Verify exit code is 0
    assert result.returncode == 0

    # Verify no POST was made
    assert len(fake_bridge["seen"]) == 0


@pytest.mark.asyncio
async def test_webhook_url_whitespace_stripped(fake_webhook, tmp_path):
    """
    Webhook URL has surrounding whitespace → hook strips it before posting.
    """
    # Build transcript with user message 800s ago
    now = datetime.now(timezone.utc)
    last_user_ts = (now - timedelta(seconds=800)).isoformat()
    transcript_path = tmp_path / "transcript.jsonl"
    transcript_path.write_text(_build_transcript(last_user_ts))

    # Create fake home with webhook file containing whitespace
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    webhook_file = claude_dir / "discord-notify-webhook"
    # Write URL with surrounding whitespace and newlines
    webhook_file.write_text(f"  \n{fake_webhook['url']}\n  ")

    # Build payload
    payload = {
        "session_id": "whitespace-test",
        "cwd": "/tmp/ggg",
    }

    # Use a closed port so bridge fails and fallback is triggered
    closed_port = 54323

    # Run hook
    result = await _run_hook(
        payload,
        transcript_path=transcript_path,
        bridge_url=f"http://127.0.0.1:{closed_port}",
        home_dir=fake_home,
    )

    # Verify exit code
    assert result.returncode == 0

    # Verify webhook received the POST (URL was correctly stripped)
    assert len(fake_webhook["seen"]) == 1


@pytest.mark.asyncio
async def test_webhook_file_permissions_enforced(fake_webhook, tmp_path):
    """
    Webhook file with insecure permissions (not 0600) → hook fixes permissions.
    """
    import stat

    # Build transcript with user message 800s ago
    now = datetime.now(timezone.utc)
    last_user_ts = (now - timedelta(seconds=800)).isoformat()
    transcript_path = tmp_path / "transcript.jsonl"
    transcript_path.write_text(_build_transcript(last_user_ts))

    # Create fake home with webhook file with insecure permissions
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    webhook_file = claude_dir / "discord-notify-webhook"
    webhook_file.write_text(fake_webhook["url"])
    # Set to world-readable (0644) - insecure
    webhook_file.chmod(0o644)

    # Verify file starts with insecure permissions
    initial_mode = webhook_file.stat().st_mode & 0o777
    assert initial_mode == 0o644

    # Build payload
    payload = {
        "session_id": "perms-test",
        "cwd": "/tmp/hhh",
    }

    # Use a closed port so bridge fails and fallback webhook is triggered
    closed_port = 54324

    # Run hook
    result = await _run_hook(
        payload,
        transcript_path=transcript_path,
        bridge_url=f"http://127.0.0.1:{closed_port}",
        home_dir=fake_home,
    )

    # Verify exit code
    assert result.returncode == 0

    # Verify webhook received the POST
    assert len(fake_webhook["seen"]) == 1

    # Verify file permissions were fixed to 0600
    final_mode = webhook_file.stat().st_mode & 0o777
    assert final_mode == 0o600

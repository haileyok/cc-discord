#!/usr/bin/env python3
"""Stop hook: notify Discord via the bridge daemon (with webhook fallback)."""
from __future__ import annotations
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


THRESHOLD_SECS = 600
BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8787")
BRIDGE_API_SECRET = os.environ.get("BRIDGE_API_SECRET", "")
WEBHOOK_FILE = Path.home() / ".claude" / "discord-notify-webhook"
HTTP_TIMEOUT = 5  # seconds — the daemon should respond instantly; longer would block Stop


class _BridgeUnavailable(Exception):
    """Raised when the bridge is unreachable or returns non-2xx."""
    pass


def _post(url: str, body: dict) -> None:
    """POST JSON body to url. Raise _BridgeUnavailable on any failure."""
    try:
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if BRIDGE_API_SECRET:
            headers["Authorization"] = f"Bearer {BRIDGE_API_SECRET}"
        req = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            if not (200 <= resp.status < 300):
                raise _BridgeUnavailable(f"HTTP {resp.status}")
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        ConnectionRefusedError,
    ) as e:
        raise _BridgeUnavailable(f"Request failed: {e}") from e
    except Exception as e:
        raise _BridgeUnavailable(f"Unexpected error: {e}") from e


def _try_webhook_fallback(msg: str) -> None:
    """Try to POST to the webhook URL if the file exists."""
    if not WEBHOOK_FILE.exists():
        return
    # Enforce secure permissions on webhook file
    current_mode = WEBHOOK_FILE.stat().st_mode & 0o777
    if current_mode != 0o600:
        WEBHOOK_FILE.chmod(0o600)
    url = WEBHOOK_FILE.read_text().strip()
    if not url:
        return
    try:
        _post(url, {"content": msg})
    except _BridgeUnavailable:
        pass  # silent — design says "loud and non-fatal"; here, non-fatal wins


def _find_last_user_prompt_timestamp(transcript_path: Path) -> str | None:
    """
    Return the timestamp of the most recent real user message in the transcript.
    Reads the file once into a list and walks backward, stopping at the first
    match — O(scan-from-end) rather than O(N) walk-and-overwrite. Important
    because Stop fires often and transcripts grow unbounded over a long
    session.
    """
    try:
        with open(transcript_path) as f:
            lines = f.readlines()
    except (IOError, OSError):
        return None
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if (
            entry.get("type") == "user"
            and entry.get("isSidechain", False) is False
            and entry.get("isMeta", False) is not True
        ):
            msg = entry.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                ts = entry.get("timestamp")
                if ts:
                    return ts
    return None


def main() -> None:
    """Main entry point."""
    # Read stdin as JSON
    try:
        event = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        return  # Malformed JSON — exit silently

    # Resolve transcript path
    transcript_path_str = event.get("transcript_path")
    if not transcript_path_str:
        return
    transcript_path = Path(transcript_path_str)
    if not transcript_path.exists():
        return

    # Find the last user prompt timestamp
    last_prompt_ts = _find_last_user_prompt_timestamp(transcript_path)
    if not last_prompt_ts:
        return

    # Parse timestamp (ISO8601)
    try:
        # Python 3.7+ supports fromisoformat but may fail on some formats
        # Try to parse with and without 'Z' suffix
        ts_str = last_prompt_ts
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        last_dt = datetime.fromisoformat(ts_str)
        last_epoch = last_dt.timestamp()
    except (ValueError, TypeError):
        return  # Could not parse timestamp

    # Compute elapsed time
    now_epoch = time.time()
    elapsed = int(now_epoch - last_epoch)

    # Check threshold
    if elapsed < THRESHOLD_SECS:
        return

    # Build message
    mins = elapsed // 60
    secs = elapsed % 60
    cwd = event.get("cwd", "?")
    session_short = event.get("session_id", "")[:8]
    msg = f"Claude finished a long turn — {mins}m {secs:02d}s in `{cwd}` (session {session_short})"

    # Try bridge first
    session_id = event.get("session_id", "")
    if session_id:
        bridge_body = {"session_id": session_id, "cwd": cwd, "message": msg, "level": "info"}
        try:
            _post(BRIDGE_URL + "/v1/notify", bridge_body)
            return
        except _BridgeUnavailable:
            pass  # Fall through to webhook fallback

    # Fallback to webhook
    _try_webhook_fallback(msg)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # Guarantee exit 0 — the Bash predecessor does the same via || true
    finally:
        sys.exit(0)

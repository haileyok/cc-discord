#!/usr/bin/env python3
"""Notification hook: surface Claude Code idle/permission prompts to Discord."""
from __future__ import annotations
import json
import os
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path


BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8787")
BRIDGE_API_SECRET = os.environ.get("BRIDGE_API_SECRET", "")
WEBHOOK_FILE = Path.home() / ".claude" / "discord-notify-webhook"
HTTP_TIMEOUT = 5


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


def main() -> None:
    """Main entry point."""
    # Read stdin as JSON
    try:
        event = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        return  # Malformed JSON — exit silently

    # Extract fields
    session_id = event.get("session_id", "")
    cwd = event.get("cwd", "?")

    # Get notification text: try 'notification' first, then 'message', then default
    notification_text = event.get("notification")
    if notification_text is None:
        notification_text = event.get("message")
    if notification_text is None:
        notification_text = "awaiting input"

    # Try bridge first
    if session_id:
        bridge_body = {
            "session_id": session_id,
            "cwd": cwd,
            "title": "⏸ awaiting input",
            "message": notification_text,
            "level": "warn",
        }
        try:
            _post(BRIDGE_URL + "/v1/notify", bridge_body)
            return
        except _BridgeUnavailable:
            pass  # Fall through to webhook fallback

    # Fallback to webhook
    session_short = session_id[:8] if session_id else "?"
    webhook_msg = f"⏸ awaiting input — {notification_text} ({cwd}, session {session_short})"
    _try_webhook_fallback(webhook_msg)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # Guarantee exit 0
    finally:
        sys.exit(0)

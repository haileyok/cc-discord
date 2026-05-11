#!/usr/bin/env python3
"""Event hook: POST every hook event to the bridge."""
from __future__ import annotations
import json
import os
import sys
import urllib.error
import urllib.request

BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8787")
HTTP_TIMEOUT = 5


class _BridgeUnavailable(Exception):
    """Raised when the bridge is unreachable or returns non-2xx."""
    pass


def _post(url: str, body: dict) -> None:
    """POST JSON body to url. Raise _BridgeUnavailable on any failure."""
    try:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
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


def main() -> None:
    """Main entry point."""
    # Read stdin as JSON
    try:
        event = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        return  # Malformed JSON — exit silently

    # Augment with env passthrough
    env_passthrough = {}
    for var in ("CC_BRIDGE_TASK_ID", "CC_DISCORD_TASK_ID", "CLAUDE_PROJECT_DIR"):
        if var in os.environ:
            env_passthrough[var] = os.environ[var]
    if env_passthrough:
        event["env_passthrough"] = env_passthrough

    # POST to bridge
    try:
        _post(BRIDGE_URL + "/v1/hook/event", event)
    except _BridgeUnavailable:
        pass  # Suppress — bridge unreachable means a single missed event


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # Guarantee exit 0
    finally:
        sys.exit(0)

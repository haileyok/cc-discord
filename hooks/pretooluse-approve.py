#!/usr/bin/env python3
"""PreToolUse hook: ask Discord for approval. Fail-closed on every error path."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid


BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8787")
HTTP_TIMEOUT = 605  # 600s router timeout + 5s slack — must exceed it


def _emit(decision: str, reason: str) -> None:
    """Write the hookSpecificOutput JSON to stdout per Claude Code spec, then exit 0."""
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(out))


def main() -> None:
    # Read stdin
    try:
        body = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        _emit("deny", "approval bridge received malformed input")
        return

    task_id = os.environ.get("CC_BRIDGE_TASK_ID") or os.environ.get("CC_DISCORD_TASK_ID")
    if not task_id:
        # Not a bridge-driven session — fall through to default Claude permission UI.
        # Emit "ask" to keep behavior identical to no-hook.
        _emit("ask", "no bridge task_id; falling back to default prompt")
        return

    request_id = str(uuid.uuid4())
    payload = {
        "request_id": request_id,
        "task_id": task_id,
        "tool_name": body.get("tool_name", "?"),
        "tool_input": body.get("tool_input") or {},
    }

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            BRIDGE_URL + "/v1/hook/pretooluse",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                if not (200 <= resp.status < 300):
                    _emit("deny", f"approval bridge returned HTTP {resp.status}")
                    return
                try:
                    resp_body = json.loads(resp.read().decode("utf-8"))
                except (ValueError, json.JSONDecodeError):
                    _emit("deny", "approval bridge returned malformed JSON")
                    return
                decision = resp_body.get("decision")
                reason = resp_body.get("reason") or ""
                if decision not in ("allow", "deny", "ask"):
                    _emit("deny", f"approval bridge returned unexpected decision: {decision}")
                    return
                _emit(decision, reason)
        except urllib.error.HTTPError as e:
            # HTTPError is raised for non-2xx status codes
            _emit("deny", f"approval bridge returned HTTP {e.code}")
    except (
        urllib.error.URLError,
        TimeoutError,
        ConnectionRefusedError,
        OSError,
    ):
        _emit("deny", "approval bridge unavailable")
    except Exception as e:  # noqa: BLE001
        _emit("deny", f"approval bridge unexpected error: {type(e).__name__}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Last-resort fail-closed
        try:
            _emit("deny", "approval bridge crashed")
        except Exception:
            pass
    finally:
        sys.exit(0)

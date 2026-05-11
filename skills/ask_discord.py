#!/usr/bin/env python3
"""
ask_discord.py — CLI script for the /ask-discord skill.

Posts a question to the bridge daemon and prints the reply (or a graceful fallback string) to stdout.
Always exits 0 and always writes exactly one human-readable line to stdout.
"""

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _parse(argv: list[str]) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Ask a question via Discord and wait for a reply"
    )
    parser.add_argument("question", help="The question to ask")
    parser.add_argument(
        "--session-id",
        default=None,
        help="Claude session ID (overrides CLAUDE_SESSION_ID env var)",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="Current working directory (defaults to pwd)",
    )
    parser.add_argument(
        "--timeout-secs",
        type=int,
        default=900,
        help="Timeout in seconds (defaults to 900, clamped to [5, 3600])",
    )
    return parser.parse_args(argv)


def _get_session_id(cli_value: str | None) -> tuple[str, str | None]:
    """
    Resolve session_id from CLI, env, or fallback.
    Returns (session_id, stderr_message_or_none).
    """
    if cli_value:
        return cli_value, None

    env_value = os.environ.get("CLAUDE_SESSION_ID", "").strip()
    if env_value:
        return env_value, None

    warning = "warning: CLAUDE_SESSION_ID not set; using fallback 'unknown-session'"
    return "unknown-session", warning


def _clamp_timeout(timeout_secs: int) -> int:
    """Clamp timeout to [5, 3600] seconds."""
    return max(5, min(3600, timeout_secs))


def main(argv: list[str]) -> int:
    """Main entry point."""
    args = _parse(argv)

    # Resolve session_id
    session_id, session_warning = _get_session_id(args.session_id)
    if session_warning:
        print(session_warning, file=sys.stderr)

    # Resolve cwd
    cwd = args.cwd if args.cwd else Path.cwd().as_posix()

    # Clamp timeout
    timeout_secs = _clamp_timeout(args.timeout_secs)

    # Build payload
    payload = {
        "session_id": session_id,
        "cwd": cwd,
        "question": args.question,
        "timeout_secs": timeout_secs,
    }

    # Get bridge URL
    bridge_base = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8787")
    url = bridge_base + "/v1/ask"

    try:
        # Serialize and POST
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}
        )

        # Give the network 30s headroom over the bridge's own timeout
        network_timeout = timeout_secs + 30

        with urllib.request.urlopen(req, timeout=network_timeout) as resp:
            data = json.loads(resp.read().decode())
            reply = data.get("reply", "")
            print(reply)

    except urllib.error.HTTPError as e:
        if e.code == 408:
            # Timeout fallback (AC5.2)
            mins = max(1, timeout_secs // 60)
            print(f"no reply within {mins}m; proceeding with best-guess")
        elif e.code == 503:
            # Bot not connected (AC5.3)
            print(
                "bridge daemon is reachable but Discord bot is not connected; check `cc-bridge serve` logs"
            )
        else:
            # Other HTTP error
            print(f"ask-discord error: HTTP {e.code}")

    except (urllib.error.URLError, ConnectionError, TimeoutError, socket.timeout):
        # Bridge unreachable (AC5.3)
        print(
            f"bridge daemon is not reachable at {url}; is `cc-bridge serve` running?"
        )

    except Exception as e:
        # Generic error
        print(f"ask-discord error: {type(e).__name__}: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

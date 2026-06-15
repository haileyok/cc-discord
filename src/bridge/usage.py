"""Session usage rendering from the Polytoken daemon's ``/state`` snapshot.

The daemon exposes ``context_usage = {used_tokens, limit_tokens}`` (current
context-window occupancy). It does not expose per-turn input/output token
tallies, so a precise dollar cost is not derivable; ``MODEL_PRICES`` is kept
dormant for if/when that data becomes available.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


# Anthropic API list prices in $ / 1M tokens. Snapshot 2026-05-11. Kept for
# future use — the daemon does not currently surface per-turn token counts.
MODEL_PRICES: dict[str, dict[str, float]] = {
    "claude-opus-4-7": {"input": 5.0, "output": 25.0, "cache_creation": 6.25, "cache_read": 0.50},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0, "cache_creation": 6.25, "cache_read": 0.50},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_creation": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_creation": 1.25, "cache_read": 0.10},
}


def context_limit_override() -> int | None:
    """Return the ``BRIDGE_CONTEXT_LIMIT`` override in tokens, if set and valid."""
    override = os.environ.get("BRIDGE_CONTEXT_LIMIT")
    if override:
        try:
            return int(override)
        except ValueError:
            pass
    return None


def format_state_summary(state: dict) -> str:
    """Render a one-line Discord stats footer from a ``/state`` snapshot."""
    model = state.get("active_model") or "?"
    parts = [f"🤖 `{model}`"]

    effort = state.get("active_reasoning_effort")
    if effort:
        parts.append(f"effort `{effort}`")

    cu = state.get("context_usage")
    override = context_limit_override()
    if isinstance(cu, dict):
        used = cu.get("used_tokens")
        limit = override or cu.get("limit_tokens")
        if isinstance(used, int) and isinstance(limit, int) and limit > 0:
            pct = used / limit * 100
            parts.append(f"{_humanize_tokens(used)} / {_humanize_tokens(limit)} ({pct:.1f}%)")
        elif isinstance(used, int):
            parts.append(f"{_humanize_tokens(used)} tokens")
    else:
        parts.append("context usage not available yet")

    return " · ".join(parts)


def _humanize_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)

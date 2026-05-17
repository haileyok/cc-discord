"""Token-usage and session-cost computation from Claude Code transcript JSONL.

Reads per-turn `usage` blocks from the transcript and aggregates totals for
the current session. Pricing and context-window limits are hardcoded and must
be refreshed when Anthropic publishes new rates or models.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from bridge import transcript

logger = logging.getLogger(__name__)


# Anthropic API list prices in $ / 1M tokens. Snapshot 2026-05-11.
# Source: https://platform.claude.com/docs/en/about-claude/pricing
# Update when prices change.
MODEL_PRICES: dict[str, dict[str, float]] = {
    "claude-opus-4-7": {
        "input": 5.0,
        "output": 25.0,
        "cache_creation": 6.25,
        "cache_read": 0.50,
    },
    "claude-opus-4-6": {
        "input": 5.0,
        "output": 25.0,
        "cache_creation": 6.25,
        "cache_read": 0.50,
    },
    "claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_creation": 3.75,
        "cache_read": 0.30,
    },
    "claude-haiku-4-5": {
        "input": 1.0,
        "output": 5.0,
        "cache_creation": 1.25,
        "cache_read": 0.10,
    },
}

# Default context window in tokens, by model id. The 1M-context variants are
# selected via the `[1m]` model alias at the user-settings level — the alias
# isn't present in the transcript, so we infer it from `~/.claude/settings.json`
# (see `_detect_one_m_default`, called per `context_limit()`).
MODEL_CONTEXT: dict[str, int] = {
    "claude-opus-4-7": 200_000,
    "claude-opus-4-6": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5": 200_000,
}


def _detect_one_m_default() -> int | None:
    """If the user's settings.json model alias ends with `[1m]`, return 1M.

    Returns None if the file isn't present, isn't readable, or doesn't carry
    the alias. Used as the fallback context limit for models whose entry in
    `MODEL_CONTEXT` would otherwise be wrong for this user.
    """
    p = Path.home() / ".claude" / "settings.json"
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None
    model = data.get("model") if isinstance(data, dict) else None
    if isinstance(model, str) and model.endswith("[1m]"):
        return 1_000_000
    return None


def context_limit(model: str | None) -> int | None:
    """Return the context window for `model`, in tokens.

    Resolution order:
      1. `BRIDGE_CONTEXT_LIMIT` env var (always wins).
      2. User-default detected from ~/.claude/settings.json's `[1m]` alias —
         a user opted into a long-context build via that alias, so use it.
         Re-read on each call so toggling the alias takes effect without a
         daemon restart.
      3. `MODEL_CONTEXT[model]` if set.
      4. None — caller should skip the percentage display.
    """
    override = os.environ.get("BRIDGE_CONTEXT_LIMIT")
    if override:
        try:
            return int(override)
        except ValueError:
            pass
    user_default = _detect_one_m_default()
    if user_default is not None:
        return user_default
    if model and model in MODEL_CONTEXT:
        return MODEL_CONTEXT[model]
    return None


@dataclass
class Stats:
    """Aggregated usage for a single Claude Code session (one transcript file)."""

    model: str | None
    total_input: int
    total_output: int
    total_cache_creation: int
    total_cache_read: int
    last_context_size: int  # input window the model saw on the most recent turn
    cost_usd: float | None  # None if pricing unknown for `model`
    context_window: int | None  # None if unknown


def compute_stats(transcript_path: Path) -> Stats | None:
    """Walk the transcript and aggregate per-turn usage. Returns None if no
    assistant entries with `usage` blocks are present."""
    entries = list(transcript.read_entries(transcript_path))
    if not entries:
        return None

    model: str | None = None
    total_input = 0
    total_output = 0
    total_cache_create = 0
    total_cache_read = 0
    last_context_size = 0
    saw_any_usage = False

    for e in entries:
        if e.get("type") != "assistant":
            continue
        msg = e.get("message")
        if not isinstance(msg, dict):
            continue
        m = msg.get("model")
        if isinstance(m, str):
            model = m
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        saw_any_usage = True

        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        cache_create = int(usage.get("cache_creation_input_tokens") or 0)
        cache_read = int(usage.get("cache_read_input_tokens") or 0)

        total_input += input_tokens
        total_output += output_tokens
        total_cache_create += cache_create
        total_cache_read += cache_read

        # The total input window the model received on this turn.
        last_context_size = input_tokens + cache_create + cache_read

    if not saw_any_usage:
        return None

    cost = _compute_cost(
        model, total_input, total_output, total_cache_create, total_cache_read
    )
    return Stats(
        model=model,
        total_input=total_input,
        total_output=total_output,
        total_cache_creation=total_cache_create,
        total_cache_read=total_cache_read,
        last_context_size=last_context_size,
        cost_usd=cost,
        context_window=context_limit(model),
    )


def _compute_cost(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_creation: int,
    cache_read: int,
) -> float | None:
    """Compute cost in USD using MODEL_PRICES. Returns None if model unknown."""
    if not model or model not in MODEL_PRICES:
        return None
    p = MODEL_PRICES[model]
    return (
        input_tokens * p["input"]
        + output_tokens * p["output"]
        + cache_creation * p["cache_creation"]
        + cache_read * p["cache_read"]
    ) / 1_000_000


def format_summary(stats: Stats) -> str:
    """Render a one-line footer with the session's stats for the chat platform."""
    model = stats.model or "?"
    used_str = _humanize_tokens(stats.last_context_size)
    if stats.context_window:
        limit_str = _humanize_tokens(stats.context_window)
        pct = (stats.last_context_size / stats.context_window) * 100
        ctx_part = f"{used_str} / {limit_str} ({pct:.1f}%)"
    else:
        ctx_part = f"{used_str} tokens"
    cost_part = (
        f" · ${stats.cost_usd:.2f}" if stats.cost_usd is not None else ""
    )
    return f"🤖 `{model}` · {ctx_part}{cost_part}"


def _humanize_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)

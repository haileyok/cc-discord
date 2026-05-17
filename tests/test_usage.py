"""Tests for token-usage and session-cost computation (bridge/usage.py)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest

from bridge.usage import (
    MODEL_CONTEXT,
    MODEL_PRICES,
    Stats,
    _compute_cost,
    _detect_one_m_default,
    _humanize_tokens,
    compute_stats,
    context_limit,
    format_summary,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_transcript(tmp_path: Path, entries: list[dict]) -> Path:
    """Write a JSONL transcript file and return its Path."""
    p = tmp_path / "transcript.jsonl"
    with open(p, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return p


def _assistant_entry(
    model: str = "claude-sonnet-4-6",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_creation: int = 0,
    cache_read: int = 0,
) -> dict:
    """Build a minimal assistant transcript entry with usage block."""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "response"}],
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
            },
        },
    }


# ---------------------------------------------------------------------------
# _humanize_tokens
# ---------------------------------------------------------------------------


class TestHumanizeTokens:
    def test_small_number_returns_plain_string(self) -> None:
        assert _humanize_tokens(0) == "0"
        assert _humanize_tokens(999) == "999"

    def test_thousands_use_k_suffix(self) -> None:
        assert _humanize_tokens(1_000) == "1.0k"
        assert _humanize_tokens(1_500) == "1.5k"
        assert _humanize_tokens(999_999) == "1000.0k"

    def test_millions_use_m_suffix(self) -> None:
        assert _humanize_tokens(1_000_000) == "1.0M"
        assert _humanize_tokens(2_500_000) == "2.5M"


# ---------------------------------------------------------------------------
# _compute_cost
# ---------------------------------------------------------------------------


class TestComputeCost:
    def test_returns_none_for_unknown_model(self) -> None:
        result = _compute_cost("unknown-model", 1000, 500, 0, 0)
        assert result is None

    def test_returns_none_for_none_model(self) -> None:
        result = _compute_cost(None, 1000, 500, 0, 0)
        assert result is None

    def test_zero_tokens_returns_zero_cost(self) -> None:
        result = _compute_cost("claude-sonnet-4-6", 0, 0, 0, 0)
        assert result == 0.0

    def test_input_tokens_only(self) -> None:
        # 1M input tokens at $3.00/M = $3.00
        result = _compute_cost("claude-sonnet-4-6", 1_000_000, 0, 0, 0)
        assert result == pytest.approx(3.0)

    def test_output_tokens_only(self) -> None:
        # 1M output tokens at $15.00/M = $15.00
        result = _compute_cost("claude-sonnet-4-6", 0, 1_000_000, 0, 0)
        assert result == pytest.approx(15.0)

    def test_cache_creation_tokens(self) -> None:
        # 1M cache creation tokens at $3.75/M = $3.75
        result = _compute_cost("claude-sonnet-4-6", 0, 0, 1_000_000, 0)
        assert result == pytest.approx(3.75)

    def test_cache_read_tokens(self) -> None:
        # 1M cache read tokens at $0.30/M = $0.30
        result = _compute_cost("claude-sonnet-4-6", 0, 0, 0, 1_000_000)
        assert result == pytest.approx(0.30)

    def test_combined_cost_calculation(self) -> None:
        # 100k input @ $3/M + 50k output @ $15/M = $0.30 + $0.75 = $1.05
        result = _compute_cost("claude-sonnet-4-6", 100_000, 50_000, 0, 0)
        assert result == pytest.approx(0.30 + 0.75)

    def test_opus_model_pricing(self) -> None:
        # 1M input tokens for opus at $15.00/M = $15.00
        result = _compute_cost("claude-opus-4-6", 1_000_000, 0, 0, 0)
        assert result == pytest.approx(15.0)

    def test_haiku_model_pricing(self) -> None:
        # 1M input tokens for haiku at $1.00/M = $1.00
        result = _compute_cost("claude-haiku-4-5", 1_000_000, 0, 0, 0)
        assert result == pytest.approx(1.0)

    def test_all_token_types_combined(self) -> None:
        prices = MODEL_PRICES["claude-sonnet-4-6"]
        inp, out, cc, cr = 10_000, 5_000, 2_000, 1_000
        expected = (
            inp * prices["input"]
            + out * prices["output"]
            + cc * prices["cache_creation"]
            + cr * prices["cache_read"]
        ) / 1_000_000
        result = _compute_cost("claude-sonnet-4-6", inp, out, cc, cr)
        assert result == pytest.approx(expected)


# ---------------------------------------------------------------------------
# _detect_one_m_default
# ---------------------------------------------------------------------------


class TestDetectOneMDefault:
    def test_returns_none_when_file_missing(self, tmp_path: Path) -> None:
        with mock.patch("pathlib.Path.home", return_value=tmp_path):
            result = _detect_one_m_default()
        assert result is None

    def test_returns_none_when_model_has_no_1m_suffix(self, tmp_path: Path) -> None:
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir()
        (settings_dir / "settings.json").write_text(
            json.dumps({"model": "claude-sonnet-4-6"})
        )
        with mock.patch("pathlib.Path.home", return_value=tmp_path):
            result = _detect_one_m_default()
        assert result is None

    def test_returns_1m_when_model_ends_with_1m_alias(self, tmp_path: Path) -> None:
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir()
        (settings_dir / "settings.json").write_text(
            json.dumps({"model": "claude-sonnet-4-6[1m]"})
        )
        with mock.patch("pathlib.Path.home", return_value=tmp_path):
            result = _detect_one_m_default()
        assert result == 1_000_000

    def test_returns_none_when_settings_json_is_invalid(self, tmp_path: Path) -> None:
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir()
        (settings_dir / "settings.json").write_text("not valid json")
        with mock.patch("pathlib.Path.home", return_value=tmp_path):
            result = _detect_one_m_default()
        assert result is None

    def test_returns_none_when_model_key_missing(self, tmp_path: Path) -> None:
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir()
        (settings_dir / "settings.json").write_text(json.dumps({"theme": "dark"}))
        with mock.patch("pathlib.Path.home", return_value=tmp_path):
            result = _detect_one_m_default()
        assert result is None

    def test_returns_none_when_model_is_not_a_string(self, tmp_path: Path) -> None:
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir()
        (settings_dir / "settings.json").write_text(json.dumps({"model": 42}))
        with mock.patch("pathlib.Path.home", return_value=tmp_path):
            result = _detect_one_m_default()
        assert result is None


# ---------------------------------------------------------------------------
# context_limit
# ---------------------------------------------------------------------------


class TestContextLimit:
    def test_env_var_wins_over_everything(self, tmp_path: Path) -> None:
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir()
        (settings_dir / "settings.json").write_text(
            json.dumps({"model": "claude-sonnet-4-6[1m]"})
        )
        with mock.patch("pathlib.Path.home", return_value=tmp_path):
            with mock.patch.dict(os.environ, {"BRIDGE_CONTEXT_LIMIT": "512000"}):
                result = context_limit("claude-sonnet-4-6")
        assert result == 512_000

    def test_invalid_env_var_falls_through(self, tmp_path: Path) -> None:
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir()
        (settings_dir / "settings.json").write_text(json.dumps({}))
        with mock.patch("pathlib.Path.home", return_value=tmp_path):
            with mock.patch.dict(os.environ, {"BRIDGE_CONTEXT_LIMIT": "notanumber"}):
                result = context_limit("claude-sonnet-4-6")
        assert result == MODEL_CONTEXT["claude-sonnet-4-6"]

    def test_1m_alias_overrides_model_context(self, tmp_path: Path) -> None:
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir()
        (settings_dir / "settings.json").write_text(
            json.dumps({"model": "claude-opus-4-6[1m]"})
        )
        with mock.patch("pathlib.Path.home", return_value=tmp_path):
            result = context_limit("claude-opus-4-6")
        assert result == 1_000_000

    def test_known_model_returns_model_context(self, tmp_path: Path) -> None:
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir()
        (settings_dir / "settings.json").write_text(json.dumps({}))
        with mock.patch("pathlib.Path.home", return_value=tmp_path):
            result = context_limit("claude-sonnet-4-6")
        assert result == 200_000

    def test_unknown_model_returns_none(self, tmp_path: Path) -> None:
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir()
        (settings_dir / "settings.json").write_text(json.dumps({}))
        with mock.patch("pathlib.Path.home", return_value=tmp_path):
            result = context_limit("unknown-model-xyz")
        assert result is None

    def test_none_model_returns_none(self, tmp_path: Path) -> None:
        settings_dir = tmp_path / ".claude"
        settings_dir.mkdir()
        (settings_dir / "settings.json").write_text(json.dumps({}))
        with mock.patch("pathlib.Path.home", return_value=tmp_path):
            result = context_limit(None)
        assert result is None


# ---------------------------------------------------------------------------
# compute_stats
# ---------------------------------------------------------------------------


class TestComputeStats:
    def test_returns_none_for_empty_transcript(self, tmp_path: Path) -> None:
        p = _write_transcript(tmp_path, [])
        result = compute_stats(p)
        assert result is None

    def test_returns_none_for_nonexistent_file(self, tmp_path: Path) -> None:
        result = compute_stats(tmp_path / "nonexistent.jsonl")
        assert result is None

    def test_returns_none_when_no_assistant_usage_blocks(self, tmp_path: Path) -> None:
        entries = [
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hello"}],
                    # no "usage" key
                },
            },
        ]
        p = _write_transcript(tmp_path, entries)
        result = compute_stats(p)
        assert result is None

    def test_basic_single_turn(self, tmp_path: Path) -> None:
        p = _write_transcript(
            tmp_path,
            [_assistant_entry("claude-sonnet-4-6", input_tokens=1000, output_tokens=200)],
        )
        with mock.patch("bridge.usage._detect_one_m_default", return_value=None):
            result = compute_stats(p)
        assert result is not None
        assert result.model == "claude-sonnet-4-6"
        assert result.total_input == 1000
        assert result.total_output == 200
        assert result.total_cache_creation == 0
        assert result.total_cache_read == 0

    def test_accumulates_tokens_across_multiple_turns(self, tmp_path: Path) -> None:
        entries = [
            _assistant_entry("claude-sonnet-4-6", input_tokens=100, output_tokens=50),
            _assistant_entry("claude-sonnet-4-6", input_tokens=200, output_tokens=80),
        ]
        p = _write_transcript(tmp_path, entries)
        with mock.patch("bridge.usage._detect_one_m_default", return_value=None):
            result = compute_stats(p)
        assert result is not None
        assert result.total_input == 300
        assert result.total_output == 130

    def test_cache_tokens_accumulated(self, tmp_path: Path) -> None:
        entries = [
            _assistant_entry(
                "claude-sonnet-4-6",
                input_tokens=100,
                output_tokens=50,
                cache_creation=20,
                cache_read=10,
            ),
            _assistant_entry(
                "claude-sonnet-4-6",
                input_tokens=100,
                output_tokens=50,
                cache_creation=5,
                cache_read=15,
            ),
        ]
        p = _write_transcript(tmp_path, entries)
        with mock.patch("bridge.usage._detect_one_m_default", return_value=None):
            result = compute_stats(p)
        assert result is not None
        assert result.total_cache_creation == 25
        assert result.total_cache_read == 25

    def test_last_context_size_is_from_final_turn(self, tmp_path: Path) -> None:
        entries = [
            _assistant_entry(
                "claude-sonnet-4-6",
                input_tokens=100,
                output_tokens=50,
                cache_creation=0,
                cache_read=0,
            ),
            _assistant_entry(
                "claude-sonnet-4-6",
                input_tokens=500,
                output_tokens=100,
                cache_creation=50,
                cache_read=25,
            ),
        ]
        p = _write_transcript(tmp_path, entries)
        with mock.patch("bridge.usage._detect_one_m_default", return_value=None):
            result = compute_stats(p)
        assert result is not None
        # last_context_size = input + cache_creation + cache_read of the last turn
        assert result.last_context_size == 500 + 50 + 25

    def test_model_detected_from_transcript(self, tmp_path: Path) -> None:
        p = _write_transcript(
            tmp_path, [_assistant_entry("claude-haiku-4-5", input_tokens=100, output_tokens=10)]
        )
        with mock.patch("bridge.usage._detect_one_m_default", return_value=None):
            result = compute_stats(p)
        assert result is not None
        assert result.model == "claude-haiku-4-5"

    def test_last_model_wins_when_model_changes(self, tmp_path: Path) -> None:
        entries = [
            _assistant_entry("claude-sonnet-4-6", input_tokens=100, output_tokens=10),
            _assistant_entry("claude-opus-4-6", input_tokens=200, output_tokens=20),
        ]
        p = _write_transcript(tmp_path, entries)
        with mock.patch("bridge.usage._detect_one_m_default", return_value=None):
            result = compute_stats(p)
        assert result is not None
        assert result.model == "claude-opus-4-6"

    def test_cost_is_none_for_unknown_model(self, tmp_path: Path) -> None:
        entry = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "unknown-model-xyz",
                "content": [],
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        }
        p = _write_transcript(tmp_path, [entry])
        with mock.patch("bridge.usage._detect_one_m_default", return_value=None):
            result = compute_stats(p)
        assert result is not None
        assert result.cost_usd is None

    def test_cost_calculated_for_known_model(self, tmp_path: Path) -> None:
        p = _write_transcript(
            tmp_path,
            [
                _assistant_entry(
                    "claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=0
                )
            ],
        )
        with mock.patch("bridge.usage._detect_one_m_default", return_value=None):
            result = compute_stats(p)
        assert result is not None
        # 1M input tokens at $3/M = $3.00
        assert result.cost_usd == pytest.approx(3.0)

    def test_skips_non_assistant_entries(self, tmp_path: Path) -> None:
        entries = [
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            {"type": "system", "content": "system prompt"},
            _assistant_entry("claude-sonnet-4-6", input_tokens=100, output_tokens=10),
        ]
        p = _write_transcript(tmp_path, entries)
        with mock.patch("bridge.usage._detect_one_m_default", return_value=None):
            result = compute_stats(p)
        assert result is not None
        assert result.total_input == 100
        assert result.total_output == 10

    def test_missing_usage_tokens_default_to_zero(self, tmp_path: Path) -> None:
        entry = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [],
                "usage": {
                    # only input_tokens present
                    "input_tokens": 500,
                },
            },
        }
        p = _write_transcript(tmp_path, [entry])
        with mock.patch("bridge.usage._detect_one_m_default", return_value=None):
            result = compute_stats(p)
        assert result is not None
        assert result.total_input == 500
        assert result.total_output == 0
        assert result.total_cache_creation == 0
        assert result.total_cache_read == 0

    def test_null_usage_tokens_treated_as_zero(self, tmp_path: Path) -> None:
        entry = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [],
                "usage": {
                    "input_tokens": None,
                    "output_tokens": None,
                    "cache_creation_input_tokens": None,
                    "cache_read_input_tokens": None,
                },
            },
        }
        p = _write_transcript(tmp_path, [entry])
        with mock.patch("bridge.usage._detect_one_m_default", return_value=None):
            result = compute_stats(p)
        assert result is not None
        assert result.total_input == 0
        assert result.total_output == 0

    def test_context_window_populated_for_known_model(self, tmp_path: Path) -> None:
        p = _write_transcript(
            tmp_path, [_assistant_entry("claude-sonnet-4-6", input_tokens=100, output_tokens=10)]
        )
        with mock.patch("bridge.usage._detect_one_m_default", return_value=None):
            result = compute_stats(p)
        assert result is not None
        assert result.context_window == 200_000

    def test_context_window_none_for_unknown_model(self, tmp_path: Path) -> None:
        entry = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "unknown-model-xyz",
                "content": [],
                "usage": {"input_tokens": 100, "output_tokens": 10},
            },
        }
        p = _write_transcript(tmp_path, [entry])
        with mock.patch("bridge.usage._detect_one_m_default", return_value=None):
            result = compute_stats(p)
        assert result is not None
        assert result.context_window is None


# ---------------------------------------------------------------------------
# format_summary
# ---------------------------------------------------------------------------


class TestFormatSummary:
    def _make_stats(
        self,
        model: str | None = "claude-sonnet-4-6",
        last_context_size: int = 50_000,
        context_window: int | None = 200_000,
        cost_usd: float | None = 0.50,
    ) -> Stats:
        return Stats(
            model=model,
            total_input=50_000,
            total_output=1_000,
            total_cache_creation=0,
            total_cache_read=0,
            last_context_size=last_context_size,
            cost_usd=cost_usd,
            context_window=context_window,
        )

    def test_contains_model_name(self) -> None:
        stats = self._make_stats(model="claude-sonnet-4-6")
        result = format_summary(stats)
        assert "claude-sonnet-4-6" in result

    def test_unknown_model_shows_question_mark(self) -> None:
        stats = self._make_stats(model=None)
        result = format_summary(stats)
        assert "`?`" in result

    def test_includes_cost_when_present(self) -> None:
        stats = self._make_stats(cost_usd=1.23)
        result = format_summary(stats)
        assert "$1.23" in result

    def test_omits_cost_when_none(self) -> None:
        stats = self._make_stats(cost_usd=None)
        result = format_summary(stats)
        assert "$" not in result

    def test_includes_percentage_when_context_window_set(self) -> None:
        stats = self._make_stats(last_context_size=50_000, context_window=200_000)
        result = format_summary(stats)
        # 50k/200k = 25.0%
        assert "25.0%" in result

    def test_no_percentage_when_context_window_none(self) -> None:
        stats = self._make_stats(context_window=None, last_context_size=50_000)
        result = format_summary(stats)
        assert "%" not in result
        assert "tokens" in result

    def test_context_size_humanized_with_k_suffix(self) -> None:
        stats = self._make_stats(last_context_size=50_000, context_window=200_000)
        result = format_summary(stats)
        assert "50.0k" in result

    def test_context_window_humanized_with_k_suffix(self) -> None:
        stats = self._make_stats(last_context_size=50_000, context_window=200_000)
        result = format_summary(stats)
        assert "200.0k" in result

    def test_context_window_humanized_with_m_suffix(self) -> None:
        stats = self._make_stats(last_context_size=500_000, context_window=1_000_000)
        result = format_summary(stats)
        assert "1.0M" in result

    def test_zero_tokens_context_window_shows_zero_percent(self) -> None:
        stats = self._make_stats(last_context_size=0, context_window=200_000)
        result = format_summary(stats)
        assert "0.0%" in result

    def test_full_context_usage_shows_100_percent(self) -> None:
        stats = self._make_stats(last_context_size=200_000, context_window=200_000)
        result = format_summary(stats)
        assert "100.0%" in result

    def test_output_starts_with_robot_emoji(self) -> None:
        stats = self._make_stats()
        result = format_summary(stats)
        assert result.startswith("🤖")

    def test_model_in_backticks(self) -> None:
        stats = self._make_stats(model="claude-haiku-4-5")
        result = format_summary(stats)
        assert "`claude-haiku-4-5`" in result

    def test_zero_cost_shows_zero_dollars(self) -> None:
        stats = self._make_stats(cost_usd=0.0)
        result = format_summary(stats)
        assert "$0.00" in result

    def test_small_token_count_no_suffix(self) -> None:
        stats = self._make_stats(last_context_size=500, context_window=None)
        result = format_summary(stats)
        assert "500 tokens" in result


# ---------------------------------------------------------------------------
# MODEL_PRICES and MODEL_CONTEXT sanity checks
# ---------------------------------------------------------------------------


class TestModelMapsConsistency:
    def test_all_price_entries_have_required_keys(self) -> None:
        required = {"input", "output", "cache_creation", "cache_read"}
        for model, prices in MODEL_PRICES.items():
            assert required <= prices.keys(), f"{model} missing keys"

    def test_all_price_values_are_positive(self) -> None:
        for model, prices in MODEL_PRICES.items():
            for key, value in prices.items():
                assert value > 0, f"{model}.{key} must be positive"

    def test_model_context_values_are_positive_integers(self) -> None:
        for model, ctx in MODEL_CONTEXT.items():
            assert isinstance(ctx, int) and ctx > 0, f"{model} context must be a positive int"

"""Polytoken version guard for the bridge.

The bridge drives a per-task Polytoken daemon over its HTTP + event-stream
contracts. Those contracts are verified against a specific version; a mismatched
``polytoken`` binary can change paths/fields/flags and break the bridge silently.

This module owns the pin and the version-detection/compare logic so both
``cli doctor`` (a hard check) and ``serve`` (a startup warning) can share it.

The supported range is the **0.3.x stable line at or above 0.3.3**. The bridge
refuses pre-release/unstable and other minor lines (e.g. 0.4.0) because the
daemon contracts are not verified against them. Patch releases within 0.3.x
(0.3.4, ...) are allowed.
"""

from __future__ import annotations

import logging
import re
import subprocess

log = logging.getLogger(__name__)

# The major.minor series the bridge is verified against.
BRIDGE_POLYTOKEN_SERIES: tuple[int, int] = (0, 3)
# Minimum version within that series.
BRIDGE_POLYTOKEN_MIN_VERSION: tuple[int, int, int] = (0, 3, 3)
# Human-readable expected version, for messages.
BRIDGE_POLYTOKEN_VERSION = ".".join(str(n) for n in BRIDGE_POLYTOKEN_MIN_VERSION)

# Parses `polytoken 0.3.3` and similar. Pre-release suffixes
# (`-unstable.1`, `a1`, `.dev0`) are stripped before numeric comparison.
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_polytoken_version(output: str) -> tuple[int, int, int] | None:
    """Parse a version from ``polytoken --version`` output.

    Accepts lines like ``polytoken 0.3.3`` and extracts ``(major, minor, patch)``.
    Returns ``None`` if no ``N.N.N`` triple is present. Pre-release suffixes are
    ignored for comparison (``0.4.0-unstable.1`` -> ``(0, 4, 0)``).
    """
    if not output:
        return None
    m = _VERSION_RE.search(output)
    if m is None:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def detect_polytoken_version(binary: str = "polytoken", *, timeout: float = 5.0) -> tuple[int, int, int] | None:
    """Run ``<binary> --version`` and return the parsed ``(major, minor, patch)``.

    Returns ``None`` if the binary is missing, exits non-zero, or its output has
    no parseable version. Never raises.
    """
    try:
        result = subprocess.run(
            [binary, "--version"], capture_output=True, timeout=timeout, text=True
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    return parse_polytoken_version(result.stdout or result.stderr)


def check_polytoken_version(version: tuple[int, int, int] | None) -> tuple[bool, str]:
    """Return ``(ok, message)`` for ``version`` against the supported pin.

    ``ok`` is True only when the version is within the supported series at or
    above the minimum. ``version=None`` (unparseable/missing) is treated as a
    failure with a clear message. The message is suitable for doctor output and
    log warnings either way.
    """
    if version is None:
        return False, (
            f"could not determine the polytoken version; bridge requires "
            f"{BRIDGE_POLYTOKEN_VERSION}+ on the {BRIDGE_POLYTOKEN_SERIES[0]}."
            f"{BRIDGE_POLYTOKEN_SERIES[1]} line"
        )
    major, minor, patch = version
    expected_major, expected_minor = BRIDGE_POLYTOKEN_SERIES
    if major != expected_major or minor != expected_minor:
        return False, (
            f"polytoken {major}.{minor}.{patch} is outside the supported "
            f"{expected_major}.{expected_minor}.x line; bridge requires "
            f"{BRIDGE_POLYTOKEN_VERSION}+"
        )
    if (major, minor, patch) < BRIDGE_POLYTOKEN_MIN_VERSION:
        return False, (
            f"polytoken {major}.{minor}.{patch} is older than the required "
            f"{BRIDGE_POLYTOKEN_VERSION}; please upgrade polytoken"
        )
    return True, (
        f"polytoken {major}.{minor}.{patch} is supported (requires "
        f"{BRIDGE_POLYTOKEN_VERSION}+ on the {expected_major}.{expected_minor}.x line)"
    )

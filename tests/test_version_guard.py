"""Tests for the Polytoken version guard (the 0.3.3 pin)."""

from bridge import version_guard as vg


class TestParseVersion:
    def test_exact(self) -> None:
        assert vg.parse_polytoken_version("polytoken 0.3.3") == (0, 3, 3)

    def test_with_log_noise(self) -> None:
        out = "2026-06-28T WARN loader: hi\nsession stuff\npolytoken 0.3.3\n"
        assert vg.parse_polytoken_version(out) == (0, 3, 3)

    def test_prerelease_suffix_stripped(self) -> None:
        # 0.4.0-unstable.1 collapses to (0, 4, 0) for numeric comparison.
        assert vg.parse_polytoken_version("polytoken 0.4.0-unstable.1") == (0, 4, 0)

    def test_patch_within_series(self) -> None:
        assert vg.parse_polytoken_version("polytoken 0.3.7") == (0, 3, 7)

    def test_garbage(self) -> None:
        assert vg.parse_polytoken_version("not a version") is None

    def test_empty(self) -> None:
        assert vg.parse_polytoken_version("") is None
        assert vg.parse_polytoken_version(None) is None  # type: ignore[arg-type]


class TestCheckVersion:
    def test_pinned_version_ok(self) -> None:
        ok, msg = vg.check_polytoken_version((0, 3, 3))
        assert ok is True
        assert "0.3.3" in msg

    def test_patch_within_series_ok(self) -> None:
        # A future 0.3.4 patch is allowed.
        ok, _ = vg.check_polytoken_version((0, 3, 4))
        assert ok is True

    def test_older_than_min_fails(self) -> None:
        ok, msg = vg.check_polytoken_version((0, 3, 2))
        assert ok is False
        assert "older" in msg

    def test_newer_minor_fails(self) -> None:
        # 0.4.0 (even as a pre-release, which collapses to (0,4,0)) is rejected.
        ok, msg = vg.check_polytoken_version((0, 4, 0))
        assert ok is False
        assert "0.4" in msg or "supported" in msg

    def test_different_major_fails(self) -> None:
        ok, _ = vg.check_polytoken_version((1, 0, 0))
        assert ok is False

    def test_none_unparseable_fails(self) -> None:
        ok, msg = vg.check_polytoken_version(None)
        assert ok is False
        assert "could not determine" in msg


class TestDetectVersion:
    def test_detect_real_or_missing(self) -> None:
        # detect_polytoken_version never raises; it either finds a version or None.
        v = vg.detect_polytoken_version("definitely-not-a-real-binary-xyz")
        assert v is None

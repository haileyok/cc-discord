"""Tests for tool_summary module."""

import pytest

from bridge.tool_summary import is_failure, summarize


@pytest.mark.asyncio
class TestToolSummary:
    """Tests for pure tool summary formatter."""

    async def test_bash_success(self) -> None:
        """Bash tool with exit 0."""
        result = summarize("Bash", {"command": "pytest -q"}, {"exit_code": 0, "stdout": "..."})
        assert "✓" in result
        assert "pytest -q" in result
        assert "exit" not in result

    async def test_bash_failure(self) -> None:
        """Bash tool with non-zero exit code."""
        result = summarize("Bash", {"command": "pytest"}, {"exit_code": 1})
        assert "✗" in result
        assert "pytest" in result
        assert "exit 1" in result

    async def test_bash_interrupted(self) -> None:
        """Bash tool with interrupted flag."""
        result = summarize("Bash", {"command": "long"}, {"interrupted": True})
        assert "✗" in result

    async def test_bash_truncate_long_command(self) -> None:
        """Bash truncates long commands."""
        long_cmd = "x" * 200
        result = summarize("Bash", {"command": long_cmd}, {"exit_code": 0})
        # Should contain truncation marker
        assert "…" in result or len(result) < 150

    async def test_edit_success(self) -> None:
        """Edit tool success."""
        result = summarize(
            "Edit",
            {"file_path": "src/foo.py", "old_string": "a\nb", "new_string": "x\ny\nz"},
            {"is_error": False},
        )
        assert "✏" in result
        assert "src/foo.py" in result
        assert "+3" in result
        assert "-2" in result

    async def test_edit_failure(self) -> None:
        """Edit tool failure."""
        result = summarize(
            "Edit",
            {"file_path": "src/foo.py", "old_string": "a", "new_string": "b"},
            {"is_error": True},
        )
        assert "✗" in result

    async def test_write_success(self) -> None:
        """Write tool success."""
        result = summarize(
            "Write",
            {"file_path": "file.txt", "content": "hello world"},
            None,
        )
        assert "📝" in result
        assert "file.txt" in result
        assert "11 chars" in result

    async def test_read_success(self) -> None:
        """Read tool success."""
        result = summarize("Read", {"file_path": "config.yaml"}, None)
        assert "📖" in result
        assert "config.yaml" in result

    async def test_glob_success(self) -> None:
        """Glob tool success."""
        result = summarize("Glob", {"pattern": "**/*.py"}, None)
        assert "🔍" in result
        assert "**/*.py" in result

    async def test_grep_with_path(self) -> None:
        """Grep tool with path."""
        result = summarize("Grep", {"pattern": "todo", "path": "src/"}, None)
        assert "🔍" in result
        assert "todo" in result
        assert "src/" in result

    async def test_grep_with_multiple_paths(self) -> None:
        """Polytoken grep accepts a list of search roots."""
        result = summarize(
            "Grep", {"pattern": "todo", "path": ["src/", "tests/"]}, None,
        )
        assert "🔍" in result
        assert "src/" in result and "tests/" in result

    async def test_grep_without_path(self) -> None:
        """Grep tool without path."""
        result = summarize("Grep", {"pattern": "error"}, None)
        assert "🔍" in result
        assert "error" in result

    async def test_webfetch_success(self) -> None:
        """WebFetch tool success."""
        result = summarize(
            "WebFetch",
            {"url": "https://example.com"},
            None,
        )
        assert "🌐" in result
        assert "example.com" in result

    async def test_websearch_success(self) -> None:
        """WebSearch tool success."""
        result = summarize(
            "WebSearch",
            {"query": "python async"},
            None,
        )
        assert "🌐" in result
        assert "async" in result

    async def test_task_tool_success(self) -> None:
        """Task (subagent) tool success."""
        result = summarize(
            "Task",
            {"description": "check tests"},
            None,
        )
        assert "🤖" in result
        assert "check tests" in result

    async def test_task_tool_default_description(self) -> None:
        """Task tool with missing description."""
        result = summarize("Task", {}, None)
        assert "🤖" in result
        assert "subagent" in result

    async def test_unknown_tool_success(self) -> None:
        """Unknown tool type."""
        result = summarize("UnknownTool", {}, None)
        assert "•" in result
        assert "UnknownTool" in result

    async def test_unknown_tool_failure(self) -> None:
        """Unknown tool with failure marker."""
        result = summarize("UnknownTool", {}, {"is_error": True})
        assert "✗" in result

    async def test_is_failure_with_is_error_flag(self) -> None:
        """is_failure detects is_error=True."""
        assert is_failure({"is_error": True}, "Bash") is True

    async def test_is_failure_bash_exit_code(self) -> None:
        """is_failure detects Bash exit code != 0."""
        assert is_failure({"exit_code": 1}, "Bash") is True
        assert is_failure({"exit_code": 0}, "Bash") is False

    async def test_is_failure_bash_interrupted(self) -> None:
        """is_failure detects Bash interrupted."""
        assert is_failure({"interrupted": True}, "Bash") is True

    async def test_is_failure_error_string(self) -> None:
        """is_failure detects error string."""
        assert is_failure({"error": "something went wrong"}, "WebFetch") is True
        assert is_failure({"error": ""}, "WebFetch") is False
        assert is_failure({"error": None}, "WebFetch") is False

    async def test_is_failure_empty_response(self) -> None:
        """is_failure handles None response."""
        assert is_failure(None, "Bash") is False

    async def test_short_path_short_path(self) -> None:
        """_short_path returns short paths unchanged."""
        from bridge.tool_summary import _short_path
        result = _short_path("/a/b.py")
        assert result == "/a/b.py"

    async def test_short_path_long_path(self) -> None:
        """_short_path abbreviates long paths."""
        from bridge.tool_summary import _short_path
        # Create a path that's definitely longer than 50 chars
        long_path = "/very/long/path/structure/that/exceeds/fifty/characters/limit/to/file.py"
        result = _short_path(long_path)
        assert result.startswith("...")
        assert "file.py" in result

    async def test_truncate_short_string(self) -> None:
        """_truncate returns short strings unchanged."""
        from bridge.tool_summary import _truncate
        result = _truncate("hello", 10)
        assert result == "hello"

    async def test_truncate_long_string(self) -> None:
        """_truncate adds ellipsis to long strings."""
        from bridge.tool_summary import _truncate
        result = _truncate("hello world", 5)
        assert result.endswith("…")
        assert len(result) <= 7

    async def test_truncate_removes_newlines(self) -> None:
        """_truncate converts newlines to spaces."""
        from bridge.tool_summary import _truncate
        result = _truncate("hello\nworld", 20)
        assert "\n" not in result
        assert "hello world" in result

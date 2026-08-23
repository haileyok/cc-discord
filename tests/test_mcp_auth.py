from __future__ import annotations

import os

import pytest

from bridge.mcp_auth import McpTokenError, load_or_create_mcp_token


def test_token_is_created_privately_and_stable(tmp_path):
    path = tmp_path / "private" / "mcp-token"
    first = load_or_create_mcp_token(path)
    second = load_or_create_mcp_token(path)
    assert first == second
    assert len(first) >= 43
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent).st_mode & 0o777 == 0o700


def test_token_rejects_insecure_file_and_symlink(tmp_path):
    target = tmp_path / "token"
    target.write_text("x" * 43)
    target.chmod(0o644)
    with pytest.raises(McpTokenError):
        load_or_create_mcp_token(target)
    target.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(McpTokenError):
        load_or_create_mcp_token(link)


def test_token_rejects_malformed_value(tmp_path):
    path = tmp_path / "token"
    path.write_text("too-short\n")
    path.chmod(0o600)
    with pytest.raises(McpTokenError):
        load_or_create_mcp_token(path)

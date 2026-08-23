from __future__ import annotations

import logging

import pytest

from bridge.bot import MAX_UPLOAD_AGGREGATE_BYTES
from bridge.commands import CommandDispatcher
from bridge.redaction import redact, safe_error


def test_redact_removes_credentials_queries_private_urls_and_paths() -> None:
    value = (
        "Authorization: Bearer super-secret-token xoxb-123456 "
        "https://files.slack.com/files-pri/F1/download?token=private "
        "/home/operator/private.txt user supplied text"
    )
    output = redact(value)
    assert "super-secret-token" not in output
    assert "xoxb-123456" not in output
    assert "files.slack.com" not in output
    assert "?token=private" not in output
    assert "/home/operator/private.txt" not in output
    assert "user supplied text" in output


def test_safe_error_never_includes_exception_text() -> None:
    exc = RuntimeError("Bearer secret https://files.slack.com/x?token=private /home/operator/file body")
    assert safe_error(exc, "request failed") == "request failed"


def test_redact_bounds_log_values(caplog) -> None:
    secret = "Bearer super-secret https://files.slack.com/x?token=value /home/private/file"
    with caplog.at_level(logging.WARNING):
        logging.getLogger("test-redaction").warning("%s", redact(secret))
    assert "super-secret" not in caplog.text
    assert "files.slack.com" not in caplog.text
    assert "/home/private/file" not in caplog.text
    assert len(redact("x" * 1000)) <= 241


@pytest.mark.asyncio
async def test_command_response_does_not_echo_exception_text() -> None:
    class Bot:
        team_id = "T1"
        owner_user_id = "U1"

    class Registry:
        async def list_tasks(self, actor):
            raise RuntimeError("Bearer secret https://files.slack.com/x?token=v /home/private/file")

    response = await CommandDispatcher(Bot(), Registry()).dispatch({
        "type": "slash_commands", "team_id": "T1", "command": "/agent",
        "text": "list", "user_id": "U1",
    })
    assert "Bearer" not in response.text
    assert "files.slack.com" not in response.text
    assert "/home/private/file" not in response.text
    assert response.ephemeral


def test_upload_aggregate_limit_is_configured() -> None:
    assert MAX_UPLOAD_AGGREGATE_BYTES > 0

"""Mattermost text command parser and dispatch.

This module provides:
- parse_text_command: Parse !-prefixed commands from message text
- dispatch_text_command: Dispatch parsed commands to shared command handlers
- slash_handler: HTTP handler for Mattermost slash commands
"""

from __future__ import annotations

import json
import logging
import shlex
from typing import Any

from aiohttp import web

from bridge.command_handlers import (
    CommandResult,
    handle_kill,
    handle_list,
    handle_model,
    handle_rename,
    handle_restart,
    handle_skill,
    handle_start,
    handle_stats,
    handle_stop,
    handle_tasks,
)

logger = logging.getLogger(__name__)

COMMAND_PREFIX = "!"


def parse_text_command(message: str) -> tuple[str, list[str]] | None:
    """Parse a !command from message text.

    Args:
        message: Raw message text to parse

    Returns:
        Tuple of (command_name, args) if message starts with !, else None
    """
    if not message.startswith(COMMAND_PREFIX):
        return None
    try:
        parts = shlex.split(message[len(COMMAND_PREFIX) :])
    except ValueError:
        parts = message[len(COMMAND_PREFIX) :].split()
    if not parts:
        return None
    return parts[0].lower(), parts[1:]


async def dispatch_text_command(
    command: str,
    args: list[str],
    registry: Any,
    thread_id: str | None,
) -> CommandResult:
    """Dispatch a parsed text command to the shared handler.

    Args:
        command: Command name (e.g., "start")
        args: Command arguments
        registry: TaskRegistry instance
        thread_id: Current thread ID (for context)

    Returns:
        CommandResult from the handler
    """
    if command == "start":
        if not args:
            return CommandResult(success=False, message="Usage: !start <cwd> [prompt]")
        cwd = args[0]
        prompt = " ".join(args[1:]) if len(args) > 1 else None
        return await handle_start(registry, cwd, prompt=prompt)

    elif command == "stop":
        task_id = args[0] if args else None
        return await handle_stop(registry, thread_id=thread_id, task_id=task_id)

    elif command == "kill":
        task_id = args[0] if args else None
        return await handle_kill(registry, thread_id=thread_id, task_id=task_id)

    elif command == "list":
        return await handle_list(registry)

    elif command == "restart":
        task_id = args[0] if args else None
        return await handle_restart(registry, thread_id=thread_id, task_id=task_id)

    elif command == "stats":
        task_id = args[0] if args else None
        return await handle_stats(registry, thread_id=thread_id, task_id=task_id)

    elif command in ("rename", "retitle"):
        name = " ".join(args) if args else None
        return await handle_rename(registry, thread_id=thread_id, name=name)

    elif command == "skill":
        if not args:
            return CommandResult(
                success=False, message="Usage: !skill <name> [args]"
            )
        skill_name = args[0]
        skill_args = " ".join(args[1:]) if len(args) > 1 else None
        if not thread_id:
            return CommandResult(
                success=False, message="!skill must be used in a task thread"
            )
        return await handle_skill(registry, thread_id, skill_name, args=skill_args)

    elif command == "model":
        if not args:
            return CommandResult(
                success=False, message="Usage: !model <name>"
            )
        if not thread_id:
            return CommandResult(
                success=False, message="!model must be used in a task thread"
            )
        return await handle_model(registry, thread_id=thread_id, model_name=args[0])

    elif command == "tasks":
        task_id = args[0] if args else None
        return await handle_tasks(registry, thread_id=thread_id, task_id=task_id)

    else:
        return CommandResult(success=False, message=f"Unknown command: !{command}")


def _slash_response(text: str, *, ephemeral: bool = False) -> web.Response:
    """Build a Mattermost slash command JSON response."""
    return web.Response(
        text=json.dumps({
            "text": text,
            "response_type": "ephemeral" if ephemeral else "in_channel",
        }),
        content_type="application/json",
        status=200,
    )


SLASH_COMMANDS: dict[str, str] = {
    "start": "Start a new Claude task in a fresh thread",
    "stop": "Gracefully stop a task",
    "kill": "Immediately kill a task (close its pane)",
    "list": "List active tasks",
    "restart": "Restart a task with --resume",
    "skill": "Invoke a Claude Code skill in the task's session",
    "retitle": "Rename the task's thread",
    "stats": "Show model / token / cost stats for a task",
    "tasks": "Show claude's session task list",
    "model": "Switch the Claude model for a running task",
}

SLASH_HINTS: dict[str, str] = {
    "start": "<cwd> [prompt]",
    "stop": "[task_id]",
    "kill": "[task_id]",
    "list": "",
    "restart": "[task_id]",
    "skill": "<name> [args]",
    "retitle": "[name]",
    "stats": "[task_id]",
    "tasks": "[task_id]",
    "model": "<name>",
}


async def handle_slash_request(
    request: web.Request,
    command: str,
    registry: Any,
    slash_tokens: list[str] | None = None,
) -> web.Response:
    """HTTP handler for Mattermost slash commands.

    Each slash command (e.g. /start, /stop) is routed here with the command
    name extracted from the URL path. Mattermost POSTs form-encoded data
    with fields: command, text, channel_id, user_id, token, etc.

    Args:
        request: aiohttp Request object
        command: Command name extracted from URL path (e.g., "start")
        registry: TaskRegistry instance
        slash_tokens: Valid verification tokens (None to skip validation)

    Returns:
        JSON response with text and response_type fields
    """
    try:
        data = await request.post()

        if slash_tokens and data.get("token") not in slash_tokens:
            return _slash_response("Unauthorized", ephemeral=True)

        channel_id = data.get("channel_id", "")
        text = data.get("text", "")
        try:
            args = shlex.split(text) if text else []
        except ValueError:
            args = text.split() if text else []

        result = await dispatch_text_command(
            command, args, registry, thread_id=channel_id or None,
        )

        return _slash_response(result.message, ephemeral=not result.success)

    except Exception as e:
        logger.exception("slash handler error for /%s", command)
        return _slash_response(f"Internal error: {e}", ephemeral=True)

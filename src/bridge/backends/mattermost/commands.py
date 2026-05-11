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

    elif command == "rename":
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

    elif command == "tasks":
        task_id = args[0] if args else None
        return await handle_tasks(registry, thread_id=thread_id, task_id=task_id)

    else:
        return CommandResult(success=False, message=f"Unknown command: !{command}")


async def slash_handler(
    request: web.Request,
    command: str,
    registry: Any,
) -> web.Response:
    """HTTP handler for Mattermost slash commands.

    Receives form-encoded request body with command, text, channel_id, etc.
    Returns JSON response with text and response_type.

    Args:
        request: aiohttp Request object
        command: Command name (e.g., "start")
        registry: TaskRegistry instance

    Returns:
        JSON response with text and response_type fields
    """
    try:
        data = await request.post()
        text = data.get("text", "")
        args = shlex.split(text) if text else []

        # Dispatch the command
        result = await dispatch_text_command(command, args, registry, thread_id=None)

        # Determine response type: ephemeral for errors, in_channel for success
        response_type = "in_channel" if result.success else "ephemeral"

        response_body = json.dumps(
            {
                "text": result.message,
                "response_type": response_type,
            }
        )

        return web.Response(
            text=response_body,
            content_type="application/json",
            status=200,
        )

    except Exception as e:
        logger.exception("slash_handler error for command %s", command)
        error_body = json.dumps(
            {
                "text": f"❌ Internal error: {e}",
                "response_type": "ephemeral",
            }
        )
        return web.Response(
            text=error_body,
            content_type="application/json",
            status=200,
        )

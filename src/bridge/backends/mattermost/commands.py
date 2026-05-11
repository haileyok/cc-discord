"""Mattermost text command parser and dispatch.

This module provides:
- parse_text_command: Parse !-prefixed commands from message text
- dispatch_text_command: Dispatch parsed commands to shared command handlers
"""

from __future__ import annotations

import logging
import shlex
from typing import Any

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

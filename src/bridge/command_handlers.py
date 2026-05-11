"""Platform-agnostic command handlers.

These handlers contain business logic shared across all chat platforms.
They take plain typed parameters and return CommandResult objects.
No Discord types, no platform-specific imports.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bridge.tasks import TaskRegistry

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    """Result from a command handler, consumed by platform-specific formatters.

    Attributes:
        success: Whether the command succeeded.
        message: Human-readable message to display.
        task: Single Task object (for commands that return one task).
        tasks: List of Task objects (for commands that return multiple tasks).
        embed_data: Platform-agnostic structured data for rich formatting.
    """
    success: bool
    message: str
    task: Any | None = None
    tasks: list[Any] | None = None
    embed_data: dict | None = None


def _humanize_age(epoch: int) -> str:
    """Format an epoch timestamp as a human-readable age string.

    Args:
        epoch: Unix timestamp in seconds

    Returns:
        Human-readable age string (e.g., "5m ago", "2h ago")
    """
    delta = datetime.now(timezone.utc).timestamp() - epoch
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


async def _wait_for_session_bind(
    registry: TaskRegistry, task_id: str, *, timeout: float
) -> None:
    """Poll until task.current_claude_session_id is set or timeout.

    Args:
        registry: TaskRegistry instance
        task_id: Task ID to wait for
        timeout: Maximum seconds to wait

    Raises:
        asyncio.TimeoutError: If session doesn't bind within timeout
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        task = registry.get_by_task_id(task_id)
        if task is not None and task.current_claude_session_id is not None:
            return
        await asyncio.sleep(0.1)
    raise asyncio.TimeoutError()


async def handle_start(
    registry: TaskRegistry,
    cwd: str,
    prompt: str | None = None,
) -> CommandResult:
    """Start a new Claude task in a fresh thread.

    Args:
        registry: TaskRegistry instance
        cwd: Working directory (must exist)
        prompt: Optional first message to send after binding

    Returns:
        CommandResult with task object on success
    """
    from bridge.tasks import TaskSpawnError

    try:
        task = await registry.spawn_task(cwd=cwd, prompt=prompt)
    except TaskSpawnError as e:
        return CommandResult(success=False, message=f"❌ {e}")

    return CommandResult(
        success=True,
        message=f"✅ Started task `{task.task_id[:8]}`",
        task=task,
    )


async def handle_stop(
    registry: TaskRegistry,
    thread_id: str | None = None,
    task_id: str | None = None,
) -> CommandResult:
    """Gracefully stop a task.

    Args:
        registry: TaskRegistry instance
        thread_id: Thread ID to resolve task from (if task_id not provided)
        task_id: Task ID to stop

    Returns:
        CommandResult indicating success or failure
    """
    from bridge.tasks import TaskNotFound

    task = _resolve_task(registry, thread_id, task_id)
    if task is None:
        return CommandResult(
            success=False,
            message="❌ This command must run in a task thread (or pass task_id).",
        )

    try:
        stopped = await registry.stop_task(task.task_id)
    except TaskNotFound:
        return CommandResult(success=False, message="❌ Task not found")

    if stopped:
        return CommandResult(
            success=True,
            message=f"✅ Stopped `{task.task_id[:8]}`",
        )
    else:
        return CommandResult(
            success=True,
            message=f"⚠️ Stop timed out for `{task.task_id[:8]}`. Use `/kill` to force.",
        )


async def handle_kill(
    registry: TaskRegistry,
    thread_id: str | None = None,
    task_id: str | None = None,
) -> CommandResult:
    """Immediately kill a task (close its pane).

    Args:
        registry: TaskRegistry instance
        thread_id: Thread ID to resolve task from (if task_id not provided)
        task_id: Task ID to kill

    Returns:
        CommandResult indicating success or failure
    """
    from bridge.tasks import TaskNotFound

    task = _resolve_task(registry, thread_id, task_id)
    if task is None:
        return CommandResult(
            success=False,
            message="❌ This command must run in a task thread (or pass task_id).",
        )

    try:
        await registry.kill_task(task.task_id)
    except TaskNotFound:
        return CommandResult(success=False, message="❌ Task not found")

    return CommandResult(
        success=True,
        message=f"💥 Killed `{task.task_id[:8]}`",
    )


async def handle_list(registry: TaskRegistry) -> CommandResult:
    """List active tasks.

    Args:
        registry: TaskRegistry instance

    Returns:
        CommandResult with tasks list
    """
    tasks = await registry.list_tasks()
    if not tasks:
        return CommandResult(
            success=True,
            message="No active tasks.",
            tasks=[],
        )

    lines = ["**Active tasks:**"]
    for t in tasks:
        cwd_leaf = Path(t.cwd).name or "/"
        ago = _humanize_age(t.last_activity)
        lines.append(
            f"- `{t.task_id[:8]}` · {cwd_leaf} · {t.status} · {ago} · <#{t.thread_id}>"
        )

    return CommandResult(
        success=True,
        message="\n".join(lines),
        tasks=tasks,
    )


async def handle_restart(
    registry: TaskRegistry,
    thread_id: str | None = None,
    task_id: str | None = None,
) -> CommandResult:
    """Restart a task with --resume.

    Args:
        registry: TaskRegistry instance
        thread_id: Thread ID to resolve task from (if task_id not provided)
        task_id: Task ID to restart

    Returns:
        CommandResult indicating success or failure
    """
    from bridge.tasks import TaskNotFound, TaskRestartError

    task = _resolve_task(registry, thread_id, task_id)
    if task is None:
        return CommandResult(
            success=False,
            message="❌ This command must run in a task thread (or pass task_id).",
        )

    try:
        await registry.restart_task(task.task_id)
    except (TaskNotFound, TaskRestartError) as e:
        return CommandResult(success=False, message=f"❌ {e}")

    return CommandResult(
        success=True,
        message=f"🔄 Restarted `{task.task_id[:8]}`",
    )


async def handle_stats(
    registry: TaskRegistry,
    thread_id: str | None = None,
    task_id: str | None = None,
) -> CommandResult:
    """Show model / token / cost stats for a task.

    Args:
        registry: TaskRegistry instance
        thread_id: Thread ID to resolve task from (if task_id not provided)
        task_id: Task ID to get stats for

    Returns:
        CommandResult with stats data
    """
    task = _resolve_task(registry, thread_id, task_id)
    if task is None:
        return CommandResult(
            success=False,
            message="❌ This command must run in a task thread (or pass task_id).",
        )

    if not task.current_transcript_path:
        return CommandResult(
            success=False,
            message="❌ Task has no transcript yet — wait for the first turn.",
        )

    # Import here to avoid circular dependency
    from bridge import usage

    stats = usage.compute_stats(Path(task.current_transcript_path))
    if stats is None:
        return CommandResult(
            success=False,
            message="❌ No usage data in transcript yet.",
        )

    return CommandResult(
        success=True,
        message=usage.format_summary(stats),
        embed_data={"stats": stats},
    )


async def handle_rename(
    registry: TaskRegistry,
    thread_id: str | None = None,
    name: str | None = None,
) -> CommandResult:
    """Rename the task's thread.

    Args:
        registry: TaskRegistry instance
        thread_id: Thread ID to resolve task from
        name: New thread name (if None, auto-generation is needed)

    Returns:
        CommandResult indicating success or failure
    """
    task = _resolve_task(registry, thread_id, None)
    if task is None:
        return CommandResult(
            success=False,
            message="❌ This command must run in a task thread (or pass thread_id).",
        )

    if name is None:
        try:
            generated = await registry.generate_thread_name(task.task_id)
        except Exception as e:
            return CommandResult(success=False, message=f"❌ Generation failed: {e}")
        if not generated:
            return CommandResult(
                success=False,
                message="❌ Couldn't auto-generate (no transcript yet, or claude -p errored). Pass a name explicitly.",
            )
        name = generated

    # Discord thread names: 1–100 chars, no newlines.
    cleaned = " ".join(name.split())[:100]
    if not cleaned:
        return CommandResult(success=False, message="❌ Empty name.")

    return CommandResult(
        success=True,
        message=f"✏️ Renamed to `{cleaned}`",
        embed_data={"cleaned_name": cleaned, "task_id": task.task_id},
    )


async def handle_skill(
    registry: TaskRegistry,
    thread_id: str,
    skill_name: str,
    args: str | None = None,
) -> CommandResult:
    """Invoke a Claude Code skill in the task's session.

    Args:
        registry: TaskRegistry instance
        thread_id: Thread ID to resolve task from
        skill_name: Name of the skill to invoke
        args: Optional arguments to pass to the skill

    Returns:
        CommandResult indicating success or failure
    """
    from bridge.tasks import TaskNotFound, TaskSpawnError

    task = _resolve_task(registry, thread_id, None)
    if task is None:
        return CommandResult(
            success=False,
            message="❌ This command must run in a task thread (or pass thread_id).",
        )

    try:
        await registry.invoke_skill(task.task_id, skill_name, args)
    except (TaskNotFound, TaskSpawnError) as e:
        return CommandResult(success=False, message=f"❌ {e}")

    rendered = f"/{skill_name}" + (f" {args}" if args else "")
    return CommandResult(
        success=True,
        message=f"✅ Sent `{rendered}` to `{task.task_id[:8]}`",
    )


async def handle_tasks(
    registry: TaskRegistry,
    thread_id: str | None = None,
    task_id: str | None = None,
) -> CommandResult:
    """Show Claude's current session task list (mirrored by the bridge).

    Args:
        registry: TaskRegistry instance
        thread_id: Thread ID to resolve task from (if task_id not provided)
        task_id: Task ID to get task list for

    Returns:
        CommandResult with task list data
    """
    task = _resolve_task(registry, thread_id, task_id)
    if task is None:
        return CommandResult(
            success=False,
            message="❌ This command must run in a task thread (or pass task_id).",
        )

    if not task.task_list_state:
        return CommandResult(
            success=True,
            message="ℹ No tasks tracked yet — claude hasn't called TaskCreate "
            "in this session (or the daemon was restarted since the last call).",
        )

    # Store the task list state for the platform formatter to use
    return CommandResult(
        success=True,
        message="Task list follows",
        task=task,
        embed_data={"task_list_state": task.task_list_state},
    )


def _resolve_task(
    registry: TaskRegistry, thread_id: str | None, task_id: str | None
) -> Any | None:
    """Resolve a task from either thread_id or task_id.

    Args:
        registry: TaskRegistry instance
        thread_id: Thread ID to look up task by
        task_id: Task ID (directly resolves)

    Returns:
        Task object if found, None otherwise
    """
    if task_id:
        return registry.get_by_task_id(task_id)
    elif thread_id:
        return registry.get_by_thread_id(thread_id)
    else:
        return None

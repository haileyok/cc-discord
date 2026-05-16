"""Mattermost markdown formatting for task lists, subagent blocks, and todos."""

from __future__ import annotations

from typing import Any


def format_task_list(tasks: list[Any]) -> str:
    """Format task list as Mattermost markdown table.

    Args:
        tasks: List of task objects with task_id, status, cwd_leaf, age attributes.

    Returns:
        Markdown table string, or "No active tasks." if list is empty.
    """
    if not tasks:
        return "No active tasks."

    lines = ["| Status | Task | CWD | Age |", "|--------|------|-----|-----|"]
    for task in tasks:
        status_mark = {
            "running": "▶️",
            "spawning": "🔄",
            "stopped": "⏹",
            "crashed": "💥",
            "archived": "📦",
        }.get(task.status, "❓")
        lines.append(
            f"| {status_mark} | `{task.task_id[:8]}` | {task.cwd_leaf} | {task.age} |"
        )
    return "\n".join(lines)


def format_subagent_block(
    attribution: str,
    last_actions: list[str],
    total_actions: int,
    finished: bool,
    duration_str: str,
) -> str:
    """Format subagent block as Mattermost markdown.

    Args:
        attribution: Agent attribution string (e.g., "researcher").
        last_actions: List of action strings to display.
        total_actions: Total number of actions performed.
        finished: Whether the subagent has finished.
        duration_str: Duration string (e.g., "30s", "2m").

    Returns:
        Markdown string for the subagent block.
    """
    status = "finished" if finished else "running"
    status_emoji = "🟢" if finished else "🟡"

    actions_text = "\n".join(f"• {a}" for a in last_actions[-5:])
    if len(actions_text) > 3500:
        actions_text = actions_text[:3500] + "\n…(truncated)"

    return (
        f"**🤖 {attribution}**\n"
        f"{actions_text}\n"
        f"_{status_emoji} {status} · {total_actions} actions · {duration_str}_"
    )


def format_tool_diff(tool_name: str, diff_text: str) -> str:
    """Format a tool diff block for Mattermost (same markdown as Discord).

    Args:
        tool_name: Name of the tool (e.g., "Edit").
        diff_text: The diff text (already in markdown format).

    Returns:
        The diff text unchanged (already markdown-formatted).
    """
    # tool_summary.diff_block already produces markdown
    return diff_text


def format_agent_task_list(entries: list[dict], done: int, total: int) -> str:
    """Format an agent's TodoWrite task list as a Mattermost checklist.

    This renders the task_list_state from a Claude session (TodoWrite items),
    as opposed to format_task_list which renders bridge Task sessions.

    Args:
        entries: List of dicts with "id", "status", "subject" keys.
        done: Count of completed tasks.
        total: Total task count.

    Returns:
        Markdown checklist string with header and footer.
    """
    if not entries:
        return "**📋 Tasks**\n_(no tasks)_"

    lines = ["**📋 Tasks**"]
    for entry in entries:
        tid = entry.get("id", "?")
        status = entry.get("status", "pending")
        subject = entry.get("subject", "")
        mark = {
            "completed": "✅",
            "in_progress": "▶️",
            "deleted": "🗑",
            "pending": "⬜",
        }.get(status, "⬜")
        line = f"{mark} #{tid}"
        if subject:
            line += f" {subject}"
        lines.append(line)

    lines.append(f"_{done}/{total} done_")
    return "\n".join(lines)


def format_task_todos(todos: list[dict]) -> str:
    """Format TodoWrite as Mattermost checklist.

    Args:
        todos: List of todo dicts with 'status' and 'content' keys.

    Returns:
        Markdown checklist string.
    """
    lines = []
    for todo in todos:
        status = todo.get("status", "pending")
        mark = {
            "completed": "✅",
            "in_progress": "▶️",
            "pending": "⬜",
            "deleted": "🗑",
        }.get(status, "⬜")
        lines.append(f"{mark} {todo.get('content', '')}")
    return "\n".join(lines)

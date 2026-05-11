"""Discord slash commands (thin wrappers around platform-agnostic handlers).

Registered guild-scoped (instant sync). Bot must finish on_ready before sync runs.
"""

import asyncio
import logging
from pathlib import Path

import discord
from discord import app_commands

from bridge import skills
from bridge.backends.discord.bot import DiscordBot
from bridge.command_handlers import (
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
from bridge.tasks import TaskRegistry

logger = logging.getLogger(__name__)


class _NotInTaskThread(Exception):
    """Raised when a thread-context command is used outside a task thread."""

    pass


def build_tree(bot: DiscordBot, registry: TaskRegistry) -> app_commands.CommandTree:
    """Construct and return the CommandTree (not yet synced; caller decides when)."""
    tree = app_commands.CommandTree(bot.client)

    @tree.command(name="start", description="Start a new Claude task in a fresh thread")
    @app_commands.describe(
        cwd="Working directory the task should run in (must exist)",
        prompt="Optional first message to send after the task is bound",
    )
    async def start(
        interaction: discord.Interaction,
        cwd: str,
        prompt: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        result = await handle_start(registry, cwd=cwd, prompt=prompt)
        if result.task:
            thread_url = f"https://discord.com/channels/{interaction.guild_id}/{result.task.thread_id}"
            await interaction.followup.send(
                f"{result.message} → <#{result.task.thread_id}> ({thread_url})",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(result.message, ephemeral=True)

    @tree.command(name="list", description="List active tasks")
    async def list_cmd(interaction: discord.Interaction) -> None:
        result = await handle_list(registry)
        await interaction.response.send_message(result.message, ephemeral=True)

    @tree.command(name="stop", description="Gracefully stop a task")
    @app_commands.describe(thread="Thread to stop (defaults to invocation thread)")
    async def stop(
        interaction: discord.Interaction,
        thread: discord.Thread | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        thread_id = str(thread.id if thread else interaction.channel_id)
        result = await handle_stop(registry, thread_id=thread_id, task_id=None)
        await interaction.followup.send(result.message, ephemeral=True)

    @tree.command(name="kill", description="Immediately kill a task (close its pane)")
    @app_commands.describe(thread="Thread to kill (defaults to invocation thread)")
    async def kill(
        interaction: discord.Interaction,
        thread: discord.Thread | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        thread_id = str(thread.id if thread else interaction.channel_id)
        result = await handle_kill(registry, thread_id=thread_id, task_id=None)
        await interaction.followup.send(result.message, ephemeral=True)

    @tree.command(name="restart", description="Restart a task with --resume")
    @app_commands.describe(thread="Thread to restart (defaults to invocation thread)")
    async def restart(
        interaction: discord.Interaction,
        thread: discord.Thread | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        thread_id = str(thread.id if thread else interaction.channel_id)
        result = await handle_restart(registry, thread_id=thread_id, task_id=None)
        await interaction.followup.send(result.message, ephemeral=True)

    async def _skill_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        cur = current.lower()
        out: list[app_commands.Choice[str]] = []
        for s in skills.list_skills():
            if cur and cur not in s.name.lower() and not (
                s.description and cur in s.description.lower()
            ):
                continue
            label = s.name
            if s.description:
                label = f"{s.name} — {s.description}"
            # Discord limits both the displayed name and submitted value to 100 chars.
            out.append(
                app_commands.Choice(name=label[:100], value=s.name[:100])
            )
            if len(out) >= 25:
                break
        return out

    @tree.command(name="skill", description="Invoke a Claude Code skill in the task's session")
    @app_commands.describe(
        name="Skill name (autocomplete shows available skills + their descriptions)",
        args="Optional arguments to pass after the skill name",
    )
    @app_commands.autocomplete(name=_skill_autocomplete)
    async def skill_cmd(
        interaction: discord.Interaction,
        name: str,
        args: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        thread_id = str(interaction.channel_id)
        result = await handle_skill(registry, thread_id=thread_id, skill_name=name, args=args)
        await interaction.followup.send(result.message, ephemeral=True)

    @tree.command(
        name="rename",
        description="Rename the task's thread (omit name to auto-generate via claude -p)",
    )
    @app_commands.describe(name="New thread name; omit to auto-generate")
    async def rename_cmd(
        interaction: discord.Interaction,
        name: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        thread_id = str(interaction.channel_id)
        result = await handle_rename(registry, thread_id=thread_id, name=name)
        if not result.success:
            await interaction.followup.send(result.message, ephemeral=True)
            return
        # Get the task to access thread_id for rename call
        task = registry.get_by_thread_id(thread_id)
        if task and result.embed_data and "cleaned_name" in result.embed_data:
            try:
                await bot.rename_thread(task.thread_id, result.embed_data["cleaned_name"])
            except Exception as e:
                await interaction.followup.send(
                    f"❌ Rename failed: {e}", ephemeral=True
                )
                return
        await interaction.followup.send(result.message, ephemeral=True)

    @tree.command(name="stats", description="Show model / token / cost stats for a task")
    @app_commands.describe(thread="Thread to inspect (defaults to invocation thread)")
    async def stats_cmd(
        interaction: discord.Interaction,
        thread: discord.Thread | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        thread_id = str(thread.id if thread else interaction.channel_id)
        result = await handle_stats(registry, thread_id=thread_id, task_id=None)
        await interaction.followup.send(result.message, ephemeral=True)

    @tree.command(
        name="tasks",
        description="Show claude's current session task list (mirrored by the bridge)",
    )
    @app_commands.describe(thread="Thread to inspect (defaults to invocation thread)")
    async def tasks_cmd(
        interaction: discord.Interaction,
        thread: discord.Thread | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        thread_id = str(thread.id if thread else interaction.channel_id)
        result = await handle_tasks(registry, thread_id=thread_id, task_id=None)
        if not result.success or not result.task:
            await interaction.followup.send(result.message, ephemeral=True)
            return
        embed = registry._render_task_list_embed(result.task)
        await interaction.followup.send(embed=embed, ephemeral=True)

    return tree

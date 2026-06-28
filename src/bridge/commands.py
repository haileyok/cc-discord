"""discord.app_commands tree for task lifecycle control.

Registered guild-scoped (instant sync). Bot must finish on_ready before sync runs.
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands

from bridge import skills, usage
from bridge.bot import Bot, BotMissingPermission
from bridge.projects import Project
from bridge.tasks import (
    Task,
    TaskNotFound,
    TaskRegistry,
    TaskRestartError,
    TaskSpawnError,
)

logger = logging.getLogger(__name__)

# Reasoning-effort levels accepted by `/effort`. The daemon validates the level
# against the active model's capabilities and falls back gracefully.
_EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max", "none"]


class _NotInTaskThread(Exception):
    """Raised when a thread-context command is used outside a task thread."""


def build_tree(
    bot: Bot,
    registry: TaskRegistry,
    projects: list[Project] | None = None,
) -> app_commands.CommandTree:
    """Construct and return the CommandTree (not yet synced; caller decides when)."""
    tree = app_commands.CommandTree(bot.client)
    projects_list: list[Project] = list(projects or [])
    projects_by_key: dict[str, Project] = {
        f"{p.root_label}/{p.name}": p for p in projects_list
    }

    @tree.command(name="start", description="Start a new Polytoken task in a fresh thread")
    @app_commands.describe(
        cwd="Working directory the task should run in (must exist)",
        prompt="Optional first message to send to the new session",
    )
    async def start(
        interaction: discord.Interaction,
        cwd: str,
        prompt: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            task = await registry.spawn_task(cwd=cwd, prompt=None)
        except TaskSpawnError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        if prompt:
            await registry.write_initial_prompt(task.task_id, prompt)
        thread_url = f"https://discord.com/channels/{interaction.guild_id}/{task.thread_id}"
        await interaction.followup.send(
            f"✅ Started task `{task.task_id[:8]}` → <#{task.thread_id}> ({thread_url})",
            ephemeral=True,
        )

    async def _project_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        cur = current.lower()
        out: list[app_commands.Choice[str]] = []
        for key, proj in projects_by_key.items():
            if cur and cur not in proj.name.lower() and cur not in proj.root_label.lower():
                continue
            label = f"{proj.name} — {proj.root_label}"
            out.append(app_commands.Choice(name=label[:100], value=key[:100]))
            if len(out) >= 25:
                break
        return out

    @tree.command(
        name="spawn",
        description="Spawn a Polytoken task in a configured project folder (see BRIDGE_PROJECT_ROOTS)",
    )
    @app_commands.describe(
        project="Project folder (autocomplete shows immediate subfolders of BRIDGE_PROJECT_ROOTS)",
        prompt="Optional first message to send to the new session",
    )
    @app_commands.autocomplete(project=_project_autocomplete)
    async def spawn(
        interaction: discord.Interaction,
        project: str,
        prompt: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not projects_by_key:
            await interaction.followup.send(
                "❌ No project roots configured. Set `BRIDGE_PROJECT_ROOTS` "
                "(colon-separated parent paths) and restart the daemon.",
                ephemeral=True,
            )
            return
        proj = projects_by_key.get(project)
        if proj is None:
            await interaction.followup.send(
                f"❌ Unknown project `{project}`. Pick one from the autocomplete list.",
                ephemeral=True,
            )
            return
        try:
            task = await registry.spawn_task(cwd=str(proj.path), prompt=None)
        except TaskSpawnError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        if prompt:
            await registry.write_initial_prompt(task.task_id, prompt)
        thread_url = f"https://discord.com/channels/{interaction.guild_id}/{task.thread_id}"
        await interaction.followup.send(
            f"✅ Started task `{task.task_id[:8]}` in `{proj.name}` "
            f"({proj.root_label}) → <#{task.thread_id}> ({thread_url})",
            ephemeral=True,
        )

    @tree.command(name="list", description="List active tasks")
    async def list_cmd(interaction: discord.Interaction) -> None:
        tasks = await registry.list_tasks()
        if not tasks:
            await interaction.response.send_message("No active tasks.", ephemeral=True)
            return
        lines = ["**Active tasks:**"]
        for t in tasks:
            cwd_leaf = Path(t.cwd).name or "/"
            ago = _humanize_age(t.last_activity)
            lines.append(
                f"- `{t.task_id[:8]}` · {cwd_leaf} · {t.status} · {ago} · <#{t.thread_id}>"
            )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @tree.command(name="stop", description="Stop a task (cancels any turn and terminates the daemon)")
    @app_commands.describe(thread="Thread to stop (defaults to invocation thread)")
    async def stop(
        interaction: discord.Interaction,
        thread: discord.Thread | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            task = _resolve_task(registry, interaction, thread)
        except _NotInTaskThread as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        try:
            stopped = await registry.stop_task(task.task_id)
        except TaskNotFound:
            await interaction.followup.send("❌ Task not found", ephemeral=True)
            return
        if stopped:
            await interaction.followup.send(f"✅ Stopped `{task.task_id[:8]}`", ephemeral=True)
        else:
            await interaction.followup.send(
                f"⚠️ Couldn't terminate `{task.task_id[:8]}` — the daemon rejected it and is "
                "still running. The task stays active; see the thread for details.",
                ephemeral=True,
            )

    @tree.command(name="kill", description="Immediately terminate a task's daemon")
    @app_commands.describe(thread="Thread to kill (defaults to invocation thread)")
    async def kill(
        interaction: discord.Interaction,
        thread: discord.Thread | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            task = _resolve_task(registry, interaction, thread)
        except _NotInTaskThread as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        try:
            killed = await registry.kill_task(task.task_id)
        except TaskNotFound:
            await interaction.followup.send("❌ Task not found", ephemeral=True)
            return
        if killed:
            await interaction.followup.send(f"💥 Killed `{task.task_id[:8]}`", ephemeral=True)
        else:
            await interaction.followup.send(
                f"⚠️ Couldn't terminate `{task.task_id[:8]}` — the daemon rejected it and is "
                "still running. Check `polytoken sessions` and terminate it manually.",
                ephemeral=True,
            )

    @tree.command(name="restart", description="(Unsupported with the daemon backend)")
    @app_commands.describe(thread="Thread to restart (defaults to invocation thread)")
    async def restart(
        interaction: discord.Interaction,
        thread: discord.Thread | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            task = _resolve_task(registry, interaction, thread)
        except _NotInTaskThread as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        try:
            await registry.restart_task(task.task_id)
        except (TaskNotFound, TaskRestartError) as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        await interaction.followup.send(f"🔄 Restarted `{task.task_id[:8]}`", ephemeral=True)

    async def _skill_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        cur = current.lower()
        names: list[str] = []
        # Prefer the live session's available skills when inside a task thread.
        task = registry.get_by_thread_id(interaction.channel_id or 0)
        if task is not None:
            state = await registry.get_state(task.task_id)
            if state:
                names = [s for s in (state.get("available_skills") or []) if isinstance(s, str)]
        if not names:
            names = [s.name for s in skills.list_skills()]
        out: list[app_commands.Choice[str]] = []
        for name in names:
            if cur and cur not in name.lower():
                continue
            out.append(app_commands.Choice(name=name[:100], value=name[:100]))
            if len(out) >= 25:
                break
        return out

    @tree.command(name="skill", description="Invoke a skill in the task's session")
    @app_commands.describe(
        name="Skill name (autocomplete shows the session's available skills)",
        args="Optional arguments to pass after the skill name",
    )
    @app_commands.autocomplete(name=_skill_autocomplete)
    async def skill_cmd(
        interaction: discord.Interaction,
        name: str,
        args: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            task = _resolve_task(registry, interaction, None)
        except _NotInTaskThread as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        try:
            await registry.invoke_skill(task.task_id, name, args)
        except (TaskNotFound, TaskSpawnError) as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        rendered = f"@{name}" + (f" {args}" if args else "")
        await interaction.followup.send(
            f"✅ Sent `{rendered}` to `{task.task_id[:8]}`", ephemeral=True
        )

    @tree.command(name="effort", description="Change the session's reasoning effort level")
    @app_commands.describe(level="Reasoning effort (the daemon falls back if the model lacks the level)")
    @app_commands.choices(
        level=[app_commands.Choice(name=lvl, value=lvl) for lvl in _EFFORT_LEVELS]
    )
    async def effort_cmd(
        interaction: discord.Interaction,
        level: app_commands.Choice[str],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            task = _resolve_task(registry, interaction, None)
        except _NotInTaskThread as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        try:
            await registry.set_effort(task.task_id, level.value)
        except (TaskNotFound, TaskSpawnError) as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        await interaction.followup.send(
            f"⚙️ Set effort to `{level.value}` for `{task.task_id[:8]}`", ephemeral=True
        )

    _models_cache: list[str] = []

    async def _model_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if not _models_cache:
            _models_cache.extend(await registry.list_models())
        cur = current.lower()
        out: list[app_commands.Choice[str]] = []
        for name in _models_cache:
            if cur and cur not in name.lower():
                continue
            out.append(app_commands.Choice(name=name[:100], value=name[:100]))
            if len(out) >= 25:
                break
        return out

    @tree.command(name="model", description="Switch the session's active model")
    @app_commands.describe(
        name="Model registry key (autocomplete shows configured models)",
        effort="Optional reasoning effort to apply with the switch",
    )
    @app_commands.autocomplete(name=_model_autocomplete)
    @app_commands.choices(
        effort=[app_commands.Choice(name=lvl, value=lvl) for lvl in _EFFORT_LEVELS]
    )
    async def model_cmd(
        interaction: discord.Interaction,
        name: str,
        effort: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            task = _resolve_task(registry, interaction, None)
        except _NotInTaskThread as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        eff = effort.value if effort is not None else None
        try:
            await registry.set_model(task.task_id, name, reasoning_effort=eff)
        except (TaskNotFound, TaskSpawnError) as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        suffix = f" (effort `{eff}`)" if eff else ""
        await interaction.followup.send(
            f"🔧 Switched `{task.task_id[:8]}` to `{name}`{suffix}", ephemeral=True
        )

    @tree.command(name="facet", description="Switch the session's active facet")
    @app_commands.describe(facet="Facet name (the daemon validates it and rejects unknowns)")
    async def facet_cmd(interaction: discord.Interaction, facet: str) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            task = _resolve_task(registry, interaction, None)
        except _NotInTaskThread as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        try:
            await registry.set_facet(task.task_id, facet)
        except (TaskNotFound, TaskSpawnError) as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        await interaction.followup.send(
            f"🎭 Switched `{task.task_id[:8]}` to facet `{facet}`", ephemeral=True
        )

    @tree.command(
        name="rename",
        description="Rename the task's thread (omit name to use the daemon's session title)",
    )
    @app_commands.describe(name="New thread name; omit to use the daemon's auto-generated title")
    async def rename_cmd(
        interaction: discord.Interaction,
        name: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            task = _resolve_task(registry, interaction, None)
        except _NotInTaskThread as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return

        if name is None:
            generated = await registry.generate_thread_name(task.task_id)
            if not generated:
                await interaction.followup.send(
                    "❌ The daemon hasn't titled this session yet. Pass a name explicitly.",
                    ephemeral=True,
                )
                return
            name = generated

        cleaned = " ".join(name.split())[:100]
        if not cleaned:
            await interaction.followup.send("❌ Empty name.", ephemeral=True)
            return
        try:
            await bot.rename_thread(task.thread_id, cleaned)
        except Exception as e:
            await interaction.followup.send(f"❌ Rename failed: {e}", ephemeral=True)
            return
        await interaction.followup.send(f"✏️ Renamed to `{cleaned}`", ephemeral=True)

    @tree.command(name="stats", description="Show model / context-usage stats for a task")
    @app_commands.describe(thread="Thread to inspect (defaults to invocation thread)")
    async def stats_cmd(
        interaction: discord.Interaction,
        thread: discord.Thread | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            task = _resolve_task(registry, interaction, thread)
        except _NotInTaskThread as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        state = await registry.get_state(task.task_id)
        if state is None:
            await interaction.followup.send(
                "❌ Couldn't reach the task's daemon for stats.", ephemeral=True
            )
            return
        await interaction.followup.send(usage.format_state_summary(state), ephemeral=True)

    @tree.command(name="tasks", description="Show the session's todo list")
    @app_commands.describe(thread="Thread to inspect (defaults to invocation thread)")
    async def tasks_cmd(
        interaction: discord.Interaction,
        thread: discord.Thread | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            task = _resolve_task(registry, interaction, thread)
        except _NotInTaskThread as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        state = await registry.get_state(task.task_id)
        todos = (state or {}).get("todos") or []
        if not todos:
            await interaction.followup.send("ℹ No todos tracked in this session yet.", ephemeral=True)
            return
        await interaction.followup.send(_format_todos(todos), ephemeral=True)

    @tree.command(
        name="pin",
        description="Create a Discord channel bound to a project; messages auto-spawn a session",
    )
    @app_commands.describe(
        name="Optional channel name (default: cwd basename, normalized)",
        project=(
            "Project to bind (autocomplete) — required when /pin runs outside "
            "a task thread; ignored if inside one (the thread's cwd is inherited)"
        ),
    )
    @app_commands.autocomplete(project=_project_autocomplete)
    async def pin_cmd(
        interaction: discord.Interaction,
        name: str | None = None,
        project: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        existing_task = registry.get_by_thread_id(interaction.channel_id or 0)
        if existing_task is not None:
            cwd = existing_task.cwd
            source = f"inherited from thread <#{existing_task.thread_id}>"
        else:
            if not projects_by_key:
                await interaction.followup.send(
                    "❌ Not inside a task thread, and no project roots configured. "
                    "Set `BRIDGE_PROJECT_ROOTS` and restart, or run `/pin` inside an "
                    "existing task thread.",
                    ephemeral=True,
                )
                return
            if not project:
                await interaction.followup.send(
                    "❌ Outside a task thread — pass `project:` (autocomplete shows "
                    "subfolders of BRIDGE_PROJECT_ROOTS).",
                    ephemeral=True,
                )
                return
            proj = projects_by_key.get(project)
            if proj is None:
                await interaction.followup.send(
                    f"❌ Unknown project `{project}`. Pick one from the autocomplete list.",
                    ephemeral=True,
                )
                return
            cwd = str(proj.path)
            source = f"project `{proj.name}` ({proj.root_label})"

        channel_name = _sanitize_channel_name(name or Path(cwd).name)
        try:
            channel_id = await bot.create_channel(channel_name)
        except BotMissingPermission as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"❌ Channel creation failed: {e}", ephemeral=True)
            return

        try:
            await registry.pin_channel(channel_id, cwd)
        except ValueError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return

        await interaction.followup.send(
            f"📌 Pinned <#{channel_id}> → `{cwd}` ({source}). "
            "Send a message in that channel to wake a session.",
            ephemeral=True,
        )

    @tree.command(
        name="unpin",
        description="Remove the pin binding from the current channel (channel itself is not deleted)",
    )
    async def unpin_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        channel_id = interaction.channel_id or 0
        removed = await registry.unpin_channel(channel_id)
        if removed:
            await interaction.followup.send(
                f"📍 Unpinned <#{channel_id}>. Future messages here won't auto-spawn.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send("ℹ This channel isn't pinned.", ephemeral=True)

    return tree


def _resolve_task(
    registry: TaskRegistry, interaction: discord.Interaction, override: discord.Thread | None
) -> Task:
    """Resolve the task context from interaction or override thread.

    Raises _NotInTaskThread if no task is bound to the thread.
    """
    target_id = override.id if override else interaction.channel_id
    task = registry.get_by_thread_id(target_id)
    if task is None:
        raise _NotInTaskThread(
            "This command must run in a task thread (or pass `thread:` arg)."
        )
    return task


def _format_todos(todos: list) -> str:
    lines = ["**Session todos:**"]
    for t in todos:
        if not isinstance(t, dict):
            continue
        status = t.get("status") or ""
        content = t.get("content") or t.get("activeForm") or ""
        mark = {"completed": "✅", "in_progress": "▶️"}.get(status, "⬜")
        lines.append(f"{mark} {content}")
    return "\n".join(lines)[:1900]


_CHANNEL_NAME_INVALID = re.compile(r"[^a-z0-9_-]+")
_CHANNEL_NAME_COLLAPSE = re.compile(r"-+")


def _sanitize_channel_name(name: str) -> str:
    """Coerce a string into a Discord text-channel-name-safe form."""
    cleaned = _CHANNEL_NAME_INVALID.sub("-", name.lower())
    cleaned = _CHANNEL_NAME_COLLAPSE.sub("-", cleaned).strip("-")
    return cleaned[:100] or "cc-pin"


def _humanize_age(epoch: int) -> str:
    """Format an epoch timestamp as a human-readable age string."""
    delta = datetime.now(timezone.utc).timestamp() - epoch
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"

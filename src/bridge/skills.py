"""Skill enumeration for the `/skill` slash command.

Walks the user-level skills directory plus enabled-plugin skill directories
listed in `~/.claude/plugins/installed_plugins.json`, scoped by the enabled
flag in `~/.claude/settings.json`. Reads each `SKILL.md` for the description.

Resolution is best-effort and forgiving: missing files / unparseable
frontmatter just produce a skill with `description=None`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Skill:
    name: str  # `<skill>` for user, `<plugin>:<skill>` for plugin skills
    description: str | None
    source: str  # "user" or "plugin:<plugin-id>"


def list_skills() -> list[Skill]:
    """Enumerate user-level skills + skills from enabled plugins. Sorted by name."""
    out: list[Skill] = []
    home = Path.home()

    # User-level skills.
    user_skills_dir = home / ".claude" / "skills"
    if user_skills_dir.is_dir():
        for d in sorted(user_skills_dir.iterdir()):
            if not d.is_dir():
                continue
            desc = _read_description(d / "SKILL.md")
            out.append(Skill(name=d.name, description=desc, source="user"))

    # Plugin skills.
    for plugin_id, install_path in _enabled_plugin_paths().items():
        skills_root = install_path / "skills"
        if not skills_root.is_dir():
            continue
        # plugin_id is "<plugin>@<marketplace>"; the displayed prefix is just
        # the plugin part (matches Claude Code's own naming).
        prefix = plugin_id.split("@", 1)[0]
        for d in sorted(skills_root.iterdir()):
            if not d.is_dir():
                continue
            desc = _read_description(d / "SKILL.md")
            out.append(
                Skill(
                    name=f"{prefix}:{d.name}",
                    description=desc,
                    source=f"plugin:{plugin_id}",
                )
            )

    out.sort(key=lambda s: s.name)
    return out


def _enabled_plugin_paths() -> dict[str, Path]:
    """Return {plugin_id: install_path} for every enabled plugin.

    Reads `enabledPlugins` from `~/.claude/settings.json` and joins against
    `~/.claude/plugins/installed_plugins.json` for the paths.
    """
    home = Path.home()
    settings_path = home / ".claude" / "settings.json"
    installed_path = home / ".claude" / "plugins" / "installed_plugins.json"

    enabled: dict[str, bool] = {}
    try:
        settings = json.loads(settings_path.read_text())
        if isinstance(settings, dict):
            ep = settings.get("enabledPlugins")
            if isinstance(ep, dict):
                enabled = {k: bool(v) for k, v in ep.items()}
    except (OSError, ValueError):
        pass

    installed: dict[str, list[dict]] = {}
    try:
        data = json.loads(installed_path.read_text())
        if isinstance(data, dict) and isinstance(data.get("plugins"), dict):
            installed = data["plugins"]
    except (OSError, ValueError):
        pass

    paths: dict[str, Path] = {}
    for plugin_id, is_enabled in enabled.items():
        if not is_enabled:
            continue
        entries = installed.get(plugin_id) or []
        if not entries:
            continue
        # Take the first install record's path (each plugin_id has 1 install).
        install_path = entries[0].get("installPath")
        if isinstance(install_path, str):
            paths[plugin_id] = Path(install_path)
    return paths


def _read_description(skill_md: Path) -> str | None:
    """Pull the `description:` value out of a SKILL.md YAML frontmatter."""
    try:
        text = skill_md.read_text()
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    for line in text[3:end].splitlines():
        s = line.strip()
        if s.startswith("description:"):
            value = s[len("description:") :].strip()
            # Strip surrounding quotes if present.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value or None
    return None

"""Tests for skills enumeration."""

import json
from pathlib import Path

import pytest

from bridge.skills import Skill, _enabled_plugin_paths, _read_description, list_skills


class TestReadDescription:
    """Tests for _read_description function."""

    def test_reads_description_from_valid_frontmatter(self, tmp_path):
        """Extract description from YAML frontmatter."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(
            """---
name: test-skill
description: This is a test skill
version: 1.0
---

# Test Skill
"""
        )
        assert _read_description(skill_md) == "This is a test skill"

    def test_strips_double_quotes_from_description(self, tmp_path):
        """Remove surrounding double quotes if present."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(
            """---
description: "A quoted description"
---
"""
        )
        assert _read_description(skill_md) == "A quoted description"

    def test_strips_single_quotes_from_description(self, tmp_path):
        """Remove surrounding single quotes if present."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(
            """---
description: 'Another quoted description'
---
"""
        )
        assert _read_description(skill_md) == "Another quoted description"

    def test_returns_none_for_empty_description(self, tmp_path):
        """Return None when description is empty after stripping quotes."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(
            """---
description: ""
---
"""
        )
        assert _read_description(skill_md) is None

    def test_returns_none_for_missing_frontmatter(self, tmp_path):
        """Return None when file doesn't start with ---."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("# No frontmatter\n")
        assert _read_description(skill_md) is None

    def test_returns_none_for_unclosed_frontmatter(self, tmp_path):
        """Return None when closing --- is missing."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("---\ndescription: test\n# No closing\n")
        assert _read_description(skill_md) is None

    def test_returns_none_for_missing_description_line(self, tmp_path):
        """Return None when description key is not in frontmatter."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(
            """---
name: test
version: 1.0
---
"""
        )
        assert _read_description(skill_md) is None

    def test_returns_none_for_nonexistent_file(self, tmp_path):
        """Return None when file doesn't exist."""
        skill_md = tmp_path / "nonexistent.md"
        assert _read_description(skill_md) is None

    def test_handles_description_with_spaces(self, tmp_path):
        """Preserve spaces in description value."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(
            """---
description:   "A description with   spaces"
---
"""
        )
        assert _read_description(skill_md) == "A description with   spaces"

    def test_first_description_line_wins(self, tmp_path):
        """Use first description line if multiple exist."""
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text(
            """---
description: First description
another_description: Second description
---
"""
        )
        assert _read_description(skill_md) == "First description"


class TestEnabledPluginPaths:
    """Tests for _enabled_plugin_paths function."""

    def test_returns_empty_dict_when_no_settings(self, tmp_path, monkeypatch):
        """Return empty dict when settings.json doesn't exist."""
        home = tmp_path
        monkeypatch.setattr(Path, "home", lambda: home)
        (home / ".claude" / "plugins").mkdir(parents=True)
        result = _enabled_plugin_paths()
        assert result == {}

    def test_returns_empty_dict_when_no_installed_plugins(self, tmp_path, monkeypatch):
        """Return empty dict when installed_plugins.json doesn't exist."""
        home = tmp_path
        monkeypatch.setattr(Path, "home", lambda: home)
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"enabledPlugins": {"plugin1@market": True}})
        )
        result = _enabled_plugin_paths()
        assert result == {}

    def test_returns_enabled_plugin_paths(self, tmp_path, monkeypatch):
        """Return dict with enabled plugin paths."""
        home = tmp_path
        monkeypatch.setattr(Path, "home", lambda: home)
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "plugins").mkdir()

        plugin_path = home / "plugins" / "my-plugin"
        plugin_path.mkdir(parents=True)

        (home / ".claude" / "settings.json").write_text(
            json.dumps({"enabledPlugins": {"my-plugin@market": True}})
        )
        (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
            json.dumps(
                {
                    "plugins": {
                        "my-plugin@market": [{"installPath": str(plugin_path)}]
                    }
                }
            )
        )

        result = _enabled_plugin_paths()
        assert result == {"my-plugin@market": plugin_path}

    def test_skips_disabled_plugins(self, tmp_path, monkeypatch):
        """Skip plugins that are disabled."""
        home = tmp_path
        monkeypatch.setattr(Path, "home", lambda: home)
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "plugins").mkdir()

        plugin_path = home / "plugins" / "my-plugin"
        plugin_path.mkdir(parents=True)

        (home / ".claude" / "settings.json").write_text(
            json.dumps(
                {
                    "enabledPlugins": {
                        "enabled-plugin@market": True,
                        "disabled-plugin@market": False,
                    }
                }
            )
        )
        (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
            json.dumps(
                {
                    "plugins": {
                        "enabled-plugin@market": [
                            {"installPath": str(plugin_path / "enabled")}
                        ],
                        "disabled-plugin@market": [
                            {"installPath": str(plugin_path / "disabled")}
                        ],
                    }
                }
            )
        )

        result = _enabled_plugin_paths()
        assert "enabled-plugin@market" in result
        assert "disabled-plugin@market" not in result

    def test_handles_malformed_settings_json(self, tmp_path, monkeypatch):
        """Gracefully handle malformed settings.json."""
        home = tmp_path
        monkeypatch.setattr(Path, "home", lambda: home)
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "plugins").mkdir()

        (home / ".claude" / "settings.json").write_text("{invalid json")
        result = _enabled_plugin_paths()
        assert result == {}

    def test_handles_malformed_installed_plugins_json(self, tmp_path, monkeypatch):
        """Gracefully handle malformed installed_plugins.json."""
        home = tmp_path
        monkeypatch.setattr(Path, "home", lambda: home)
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "plugins").mkdir()

        (home / ".claude" / "settings.json").write_text(
            json.dumps({"enabledPlugins": {"plugin@market": True}})
        )
        (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
            "{invalid json"
        )
        result = _enabled_plugin_paths()
        assert result == {}

    def test_handles_missing_install_path(self, tmp_path, monkeypatch):
        """Skip plugin entries without installPath."""
        home = tmp_path
        monkeypatch.setattr(Path, "home", lambda: home)
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "plugins").mkdir()

        (home / ".claude" / "settings.json").write_text(
            json.dumps({"enabledPlugins": {"plugin@market": True}})
        )
        (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
            json.dumps({"plugins": {"plugin@market": [{}]}})
        )
        result = _enabled_plugin_paths()
        assert result == {}

    def test_takes_first_install_record(self, tmp_path, monkeypatch):
        """Use first install record when multiple exist for one plugin."""
        home = tmp_path
        monkeypatch.setattr(Path, "home", lambda: home)
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "plugins").mkdir()

        plugin_path1 = home / "plugins" / "plugin1"
        plugin_path2 = home / "plugins" / "plugin2"
        plugin_path1.mkdir(parents=True)
        plugin_path2.mkdir(parents=True)

        (home / ".claude" / "settings.json").write_text(
            json.dumps({"enabledPlugins": {"plugin@market": True}})
        )
        (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
            json.dumps(
                {
                    "plugins": {
                        "plugin@market": [
                            {"installPath": str(plugin_path1)},
                            {"installPath": str(plugin_path2)},
                        ]
                    }
                }
            )
        )
        result = _enabled_plugin_paths()
        assert result["plugin@market"] == plugin_path1


class TestListSkills:
    """Tests for list_skills function."""

    def test_lists_user_skills_from_directory(self, tmp_path, monkeypatch):
        """Enumerate user-level skills from ~/.claude/skills/."""
        home = tmp_path
        monkeypatch.setattr(Path, "home", lambda: home)

        skills_dir = home / ".claude" / "skills"
        (skills_dir / "skill1").mkdir(parents=True)
        (skills_dir / "skill2").mkdir(parents=True)
        (skills_dir / "skill1" / "SKILL.md").write_text(
            """---
description: "Skill One"
---
"""
        )
        (skills_dir / "skill2" / "SKILL.md").write_text(
            """---
description: "Skill Two"
---
"""
        )

        skills = list_skills()
        names = {s.name for s in skills}
        assert "skill1" in names
        assert "skill2" in names

    def test_user_skills_have_user_source(self, tmp_path, monkeypatch):
        """User-level skills have source='user'."""
        home = tmp_path
        monkeypatch.setattr(Path, "home", lambda: home)

        skills_dir = home / ".claude" / "skills"
        (skills_dir / "my-skill").mkdir(parents=True)
        (skills_dir / "my-skill" / "SKILL.md").write_text(
            """---
description: "My Skill"
---
"""
        )

        skills = list_skills()
        skill = next(s for s in skills if s.name == "my-skill")
        assert skill.source == "user"

    def test_lists_plugin_skills_with_plugin_prefix(self, tmp_path, monkeypatch):
        """Enumerate plugin skills with <plugin>:<skill> naming."""
        home = tmp_path
        monkeypatch.setattr(Path, "home", lambda: home)

        plugin_path = home / "plugins" / "my-plugin"
        skills_dir = plugin_path / "skills"
        (skills_dir / "skill1").mkdir(parents=True)
        (skills_dir / "skill1" / "SKILL.md").write_text(
            """---
description: "Plugin Skill One"
---
"""
        )

        (home / ".claude" / "plugins").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"enabledPlugins": {"my-plugin@market": True}})
        )
        (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
            json.dumps(
                {
                    "plugins": {
                        "my-plugin@market": [{"installPath": str(plugin_path)}]
                    }
                }
            )
        )

        skills = list_skills()
        names = {s.name for s in skills}
        assert "my-plugin:skill1" in names

    def test_plugin_skills_have_plugin_source(self, tmp_path, monkeypatch):
        """Plugin skills have source='plugin:<plugin-id>'."""
        home = tmp_path
        monkeypatch.setattr(Path, "home", lambda: home)

        plugin_path = home / "plugins" / "test-plugin"
        skills_dir = plugin_path / "skills"
        (skills_dir / "test-skill").mkdir(parents=True)
        (skills_dir / "test-skill" / "SKILL.md").write_text(
            """---
description: "Test"
---
"""
        )

        (home / ".claude" / "plugins").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"enabledPlugins": {"test-plugin@market": True}})
        )
        (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
            json.dumps(
                {
                    "plugins": {
                        "test-plugin@market": [{"installPath": str(plugin_path)}]
                    }
                }
            )
        )

        skills = list_skills()
        skill = next(
            (s for s in skills if s.name == "test-plugin:test-skill"), None
        )
        assert skill is not None
        assert skill.source == "plugin:test-plugin@market"

    def test_returns_sorted_list(self, tmp_path, monkeypatch):
        """Return skills sorted by name."""
        home = tmp_path
        monkeypatch.setattr(Path, "home", lambda: home)

        skills_dir = home / ".claude" / "skills"
        for name in ["zebra", "alpha", "beta"]:
            (skills_dir / name).mkdir(parents=True)
            (skills_dir / name / "SKILL.md").write_text(
                """---
description: "Test"
---
"""
            )

        skills = list_skills()
        names = [s.name for s in skills]
        assert names == sorted(names)

    def test_ignores_non_directory_entries_in_skills(self, tmp_path, monkeypatch):
        """Skip non-directory entries in skills folder."""
        home = tmp_path
        monkeypatch.setattr(Path, "home", lambda: home)

        skills_dir = home / ".claude" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "real-skill").mkdir()
        (skills_dir / "real-skill" / "SKILL.md").write_text(
            """---
description: "Real"
---
"""
        )
        (skills_dir / "file.txt").write_text("not a skill")

        skills = list_skills()
        names = {s.name for s in skills}
        assert "real-skill" in names
        assert "file.txt" not in names

    def test_handles_missing_skill_md(self, tmp_path, monkeypatch):
        """Use description=None when SKILL.md missing."""
        home = tmp_path
        monkeypatch.setattr(Path, "home", lambda: home)

        skills_dir = home / ".claude" / "skills"
        (skills_dir / "no-desc").mkdir(parents=True)

        skills = list_skills()
        skill = next((s for s in skills if s.name == "no-desc"), None)
        assert skill is not None
        assert skill.description is None

    def test_handles_empty_skills_directory(self, tmp_path, monkeypatch):
        """Return empty list when skills directory is empty."""
        home = tmp_path
        monkeypatch.setattr(Path, "home", lambda: home)
        (home / ".claude" / "skills").mkdir(parents=True)

        skills = list_skills()
        assert skills == []

    def test_handles_missing_skills_directory(self, tmp_path, monkeypatch):
        """Return empty list when skills directory doesn't exist."""
        home = tmp_path
        monkeypatch.setattr(Path, "home", lambda: home)

        skills = list_skills()
        assert skills == []

    def test_handles_plugin_missing_skills_directory(self, tmp_path, monkeypatch):
        """Skip plugins that don't have a skills directory."""
        home = tmp_path
        monkeypatch.setattr(Path, "home", lambda: home)

        plugin_path = home / "plugins" / "my-plugin"
        plugin_path.mkdir(parents=True)

        (home / ".claude" / "plugins").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"enabledPlugins": {"my-plugin@market": True}})
        )
        (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
            json.dumps(
                {
                    "plugins": {
                        "my-plugin@market": [{"installPath": str(plugin_path)}]
                    }
                }
            )
        )

        skills = list_skills()
        assert all(not s.name.startswith("my-plugin:") for s in skills)

    def test_handles_plugin_with_empty_skills_directory(self, tmp_path, monkeypatch):
        """Return empty list from plugin with empty skills directory."""
        home = tmp_path
        monkeypatch.setattr(Path, "home", lambda: home)

        plugin_path = home / "plugins" / "my-plugin"
        (plugin_path / "skills").mkdir(parents=True)

        (home / ".claude" / "plugins").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"enabledPlugins": {"my-plugin@market": True}})
        )
        (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
            json.dumps(
                {
                    "plugins": {
                        "my-plugin@market": [{"installPath": str(plugin_path)}]
                    }
                }
            )
        )

        skills = list_skills()
        plugin_skills = [s for s in skills if s.name.startswith("my-plugin:")]
        assert plugin_skills == []

    def test_extracts_plugin_id_prefix_before_at_sign(self, tmp_path, monkeypatch):
        """Use plugin ID prefix (before @) for skill naming."""
        home = tmp_path
        monkeypatch.setattr(Path, "home", lambda: home)

        plugin_path = home / "plugins" / "my-plugin"
        skills_dir = plugin_path / "skills"
        (skills_dir / "feature").mkdir(parents=True)
        (skills_dir / "feature" / "SKILL.md").write_text(
            """---
description: "Feature"
---
"""
        )

        (home / ".claude" / "plugins").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"enabledPlugins": {"my-plugin@custom-market": True}})
        )
        (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
            json.dumps(
                {
                    "plugins": {
                        "my-plugin@custom-market": [{"installPath": str(plugin_path)}]
                    }
                }
            )
        )

        skills = list_skills()
        names = {s.name for s in skills}
        assert "my-plugin:feature" in names
        assert "my-plugin@custom-market:feature" not in names

    def test_combines_user_and_plugin_skills_sorted(self, tmp_path, monkeypatch):
        """Combine user and plugin skills in sorted order."""
        home = tmp_path
        monkeypatch.setattr(Path, "home", lambda: home)

        # Create user skills
        user_skills = home / ".claude" / "skills"
        (user_skills / "user-skill").mkdir(parents=True)
        (user_skills / "user-skill" / "SKILL.md").write_text(
            """---
description: "User"
---
"""
        )

        # Create plugin skills
        plugin_path = home / "plugins" / "my-plugin"
        plugin_skills = plugin_path / "skills"
        (plugin_skills / "plugin-skill").mkdir(parents=True)
        (plugin_skills / "plugin-skill" / "SKILL.md").write_text(
            """---
description: "Plugin"
---
"""
        )

        (home / ".claude" / "plugins").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"enabledPlugins": {"my-plugin@market": True}})
        )
        (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
            json.dumps(
                {
                    "plugins": {
                        "my-plugin@market": [{"installPath": str(plugin_path)}]
                    }
                }
            )
        )

        skills = list_skills()
        names = [s.name for s in skills]
        assert "user-skill" in names
        assert "my-plugin:plugin-skill" in names
        assert names == sorted(names)

    def test_skill_dataclass_fields(self, tmp_path, monkeypatch):
        """Verify Skill dataclass has correct fields."""
        home = tmp_path
        monkeypatch.setattr(Path, "home", lambda: home)

        skills_dir = home / ".claude" / "skills"
        (skills_dir / "test").mkdir(parents=True)
        (skills_dir / "test" / "SKILL.md").write_text(
            """---
description: "Test Skill"
---
"""
        )

        skills = list_skills()
        skill = skills[0]

        assert isinstance(skill, Skill)
        assert skill.name == "test"
        assert skill.description == "Test Skill"
        assert skill.source == "user"

    def test_skill_dataclass_is_frozen(self):
        """Verify Skill dataclass is frozen (immutable)."""
        skill = Skill(name="test", description="Test", source="user")
        with pytest.raises(Exception):  # FrozenInstanceError
            skill.name = "modified"

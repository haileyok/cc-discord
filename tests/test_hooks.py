"""Tests for the global Polytoken notification hook install/register logic."""

import json
import os
import stat

from bridge import hooks


def test_install_writes_script_and_registers_both_events(tmp_path) -> None:
    cdir = str(tmp_path)
    path = hooks.install_hook(cdir)
    # Script exists, is executable.
    assert os.path.isfile(path)
    assert os.access(path, os.X_OK)
    # hooks.json has both entries pointing at the script, with the right events.
    entries = json.loads((tmp_path / "hooks.json").read_text())
    by_name = {e["name"]: e for e in entries}
    assert by_name[hooks.STOP_HOOK_NAME]["event"] == "stop"
    assert by_name[hooks.NOTIFICATION_HOOK_NAME]["event"] == "notification"
    assert by_name[hooks.STOP_HOOK_NAME]["handler"]["bash"] == str(path)
    # Preserves any pre-existing unrelated entries.
    assert len([e for e in entries if e["name"] not in hooks._HOOK_NAMES]) == 0


def test_install_is_idempotent_and_preserves_other_hooks(tmp_path) -> None:
    cdir = str(tmp_path)
    (tmp_path / "hooks.json").write_text(json.dumps(
        [{"name": "other-hook", "event": "session_start", "handler": {"bash": "true"}}]
    ))
    hooks.install_hook(cdir)
    hooks.install_hook(cdir)  # twice
    entries = json.loads((tmp_path / "hooks.json").read_text())
    names = [e["name"] for e in entries]
    assert names.count(hooks.STOP_HOOK_NAME) == 1  # no duplicates
    assert names.count(hooks.NOTIFICATION_HOOK_NAME) == 1
    assert "other-hook" in names  # preserved


def test_uninstall_removes_entries(tmp_path) -> None:
    cdir = str(tmp_path)
    hooks.install_hook(cdir)
    assert hooks.uninstall_hook(cdir) is True
    entries = json.loads((tmp_path / "hooks.json").read_text())
    assert all(e["name"] not in hooks._HOOK_NAMES for e in entries)
    assert hooks.uninstall_hook(cdir) is False  # nothing to remove


def test_status_reports_installed(tmp_path) -> None:
    cdir = str(tmp_path)
    assert hooks.hook_status(cdir)["installed"] is False
    hooks.install_hook(cdir)
    st = hooks.hook_status(cdir)
    assert st["installed"] is True
    assert set(st["registered_events"]) == {"stop", "notification"}
    assert os.path.basename(st["script_path"]) == "notify-discord.sh"


def test_status_not_installed_if_script_not_executable(tmp_path) -> None:
    cdir = str(tmp_path)
    hooks.install_hook(cdir)
    # Strip the executable bit.
    os.chmod(hooks._hook_script_path(tmp_path), 0o644)
    assert hooks.hook_status(cdir)["installed"] is False

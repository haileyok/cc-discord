"""Install/register the global Polytoken notification hook.

The hook (``hooks/notify-discord.sh``) fires for EVERY Polytoken session and
forwards ``stop`` (session waiting for input) and ``notification`` events to the
bridge's ``POST /v1/notify``, which posts to Discord with an @mention. This is
the engine-level analog of the old claude-code ``~/.claude/settings.json``
notification hooks — it works for sessions the Discord bridge does *not* drive
(TUI, ``exec``, background daemons), not just its own.

The hook script is embedded (not located by path) so registration is robust to
how the bridge was installed (``uv run`` vs ``uv tool install``).
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

log = logging.getLogger(__name__)

# Default Polytoken global config dir (where global hooks.json lives).
DEFAULT_POLYTOKEN_CONFIG_DIR = str(Path.home() / ".config" / "polytoken")
HOOK_DIR_NAME = "hooks"
HOOK_FILE_NAME = "notify-discord.sh"
HOOKS_JSON_NAME = "hooks.json"

# Distinctive names so install/uninstall/status are idempotent and a project can
# turn a hook off with ``!cc-discord-notify-stop``.
STOP_HOOK_NAME = "cc-discord-notify-stop"
NOTIFICATION_HOOK_NAME = "cc-discord-notify-notification"
_HOOK_NAMES = (STOP_HOOK_NAME, NOTIFICATION_HOOK_NAME)

# The hook handler, written verbatim to <config>/hooks/notify-discord.sh.
# Side-effect only: forwards the event to the bridge and exits 0 (proceed
# normally for any event). Best-effort, fails fast if the bridge is down.
_HOOK_SCRIPT = """#!/usr/bin/env bash
# Global Polytoken notification hook for the claude-discord-bridge.
# Fires for every session on `stop` (waiting for input) and `notification`.
# Forwards the event to the bridge's POST /v1/notify (Discord @mention).
# Side-effect only — always exits 0 (proceed normally), never changes Polytoken.
set -u
BRIDGE_URL="${BRIDGE_URL:-http://127.0.0.1:8787}"
curl -fsS --connect-timeout 1 --max-time 2 \\
  -H "Content-Type: application/json" \\
  -H "X-Polytoken-Event: ${POLYTOKEN_HOOK_EVENT:-}" \\
  -H "X-Polytoken-Session: ${POLYTOKEN_SESSION_ID:-}" \\
  -H "X-Polytoken-Project: ${POLYTOKEN_PROJECT_DIR:-}" \\
  -H "X-Polytoken-Non-Interactive: ${POLYTOKEN_NON_INTERACTIVE:-}" \\
  --data-binary @- \\
  "${BRIDGE_URL}/v1/notify" >/dev/null 2>&1 || true
exit 0
"""


def _config_dir(explicit: str | None = None) -> Path:
    d = Path(explicit or os.environ.get("POLYTOKEN_CONFIG_DIR", DEFAULT_POLYTOKEN_CONFIG_DIR))
    return d


def _hook_script_path(config_dir: Path) -> Path:
    return config_dir / HOOK_DIR_NAME / HOOK_FILE_NAME


def _load_hooks_json(config_dir: Path) -> list:
    p = config_dir / HOOKS_JSON_NAME
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("could not parse %s; treating as empty", p)
        return []
    return data if isinstance(data, list) else []


def _save_hooks_json(config_dir: Path, entries: list) -> None:
    p = config_dir / HOOKS_JSON_NAME
    config_dir.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entries, indent=2) + "\n")


def _bridge_url_default() -> str:
    return os.environ.get("BRIDGE_URL", "http://127.0.0.1:8787")


def install_hook(config_dir: str | None = None) -> Path:
    """Install the hook script + register both events in the global hooks.json.

    Idempotent: re-writes the script and replaces any existing same-named
    entries. Returns the path to the installed hook script.
    """
    cdir = _config_dir(config_dir)
    # 1. Write the hook script to <config>/hooks/notify-discord.sh (executable).
    script_path = _hook_script_path(cdir)
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(_HOOK_SCRIPT)
    os.chmod(script_path, 0o755)

    # 2. Register the two entries in <config>/hooks.json, replacing same-named.
    entries = [e for e in _load_hooks_json(cdir) if e.get("name") not in _HOOK_NAMES]
    handler = {"bash": str(script_path)}
    entries.append({"name": STOP_HOOK_NAME, "event": "stop", "handler": handler})
    entries.append({"name": NOTIFICATION_HOOK_NAME, "event": "notification", "handler": handler})
    _save_hooks_json(cdir, entries)
    log.info("installed polytoken notification hook at %s (events: stop, notification)", script_path)
    return script_path


def uninstall_hook(config_dir: str | None = None) -> bool:
    """Remove the hook entries from hooks.json (leaves the script file). Returns
    True if any entry was removed."""
    cdir = _config_dir(config_dir)
    before = _load_hooks_json(cdir)
    after = [e for e in before if e.get("name") not in _HOOK_NAMES]
    if len(after) == len(before):
        return False
    _save_hooks_json(cdir, after)
    log.info("removed polytoken notification hook entries from %s", cdir / HOOKS_JSON_NAME)
    return True


def hook_status(config_dir: str | None = None) -> dict:
    """Return ``{installed, script_path, registered_events, hooks_json_path}``.

    ``installed`` is True only when the script exists, is executable, and both
    events are registered in hooks.json. Used by ``doctor``.
    """
    cdir = _config_dir(config_dir)
    script_path = _hook_script_path(cdir)
    script_ok = script_path.exists() and bool(script_path.stat().st_mode & stat.S_IXUSR)
    entries = _load_hooks_json(cdir)
    by_name = {e.get("name"): e for e in entries if isinstance(e, dict)}
    events = []
    for name, evt in ((STOP_HOOK_NAME, "stop"), (NOTIFICATION_HOOK_NAME, "notification")):
        e = by_name.get(name)
        if e and e.get("event") == evt:
            events.append(evt)
    return {
        "installed": script_ok and set(events) == {"stop", "notification"},
        "script_path": str(script_path),
        "registered_events": events,
        "hooks_json_path": str(cdir / HOOKS_JSON_NAME),
    }

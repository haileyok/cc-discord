# cc-bridge Multiplatform — Phase 1: Project Rename

**Goal:** Rename the project from `claude-discord-bridge` to `cc-bridge` across package name, CLI entrypoint, config paths, env vars, and deployment units.

**Architecture:** Pure infrastructure rename — update string constants, paths, and documentation. Add backward-compatible fallback for secrets path and hook env vars with deprecation warnings. No functional changes to business logic.

**Tech Stack:** Python 3.12, uv, pyproject.toml, systemd, launchd

**Scope:** 8 phases from original design (phase 1 of 8)

**Codebase verified:** 2026-05-10

---

## Acceptance Criteria Coverage

This phase implements and tests:

### cc-bridge-multiplatform.AC4: Project Rename
- **cc-bridge-multiplatform.AC4.1 Success:** Package installs as `cc-bridge` CLI command
- **cc-bridge-multiplatform.AC4.2 Success:** Secrets load from `~/.config/cc-bridge/secrets.json` (with fallback to old path + deprecation warning)
- **cc-bridge-multiplatform.AC4.3 Success:** State stored in `~/.local/state/cc-bridge/`
- **cc-bridge-multiplatform.AC4.4 Success:** Hook scripts use `CC_BRIDGE_TASK_ID` env var (accept `CC_DISCORD_TASK_ID` as fallback)

### cc-bridge-multiplatform.F3: Invalid Platform Error (partial)
- **cc-bridge-multiplatform.F3:** If `BRIDGE_PLATFORM` is set to an unknown value, daemon exits with a clear error (not a stack trace) — *deferred to Phase 7; this phase only renames, does not add platform selection*

---

<!-- START_SUBCOMPONENT_A (tasks 1-2) -->
<!-- START_TASK_1 -->
### Task 1: Rename package metadata and CLI entrypoint

**Files:**
- Modify: `pyproject.toml:2,14`

**Implementation:**

Update the package name and CLI script entrypoint:

```toml
# line 2: change name
name = "claude-code-bridge"

# line 14: change CLI entrypoint
[project.scripts]
cc-bridge = "bridge.cli:main"
```

**Verification:**

```bash
uv run cc-bridge --help
```

Expected: CLI help output renders without errors.

**Commit:** `chore: rename package to claude-code-bridge with cc-bridge CLI`

<!-- END_TASK_1 -->

<!-- START_TASK_2 -->
### Task 2: Reinstall package and verify entrypoint resolves

**Files:**
- None (operational verification only)

**Verification:**

```bash
uv sync
uv run cc-bridge --help
```

Expected: `uv sync` completes without errors. `cc-bridge --help` shows the CLI commands (init, serve, doctor).

If using `uv tool install .`, also verify:
```bash
uv tool install . --force
cc-bridge --help
```

<!-- END_TASK_2 -->
<!-- END_SUBCOMPONENT_A -->

<!-- START_SUBCOMPONENT_B (tasks 3-5) -->
<!-- START_TASK_3 -->
### Task 3: Update path constants with backward-compatible fallback

**Verifies:** cc-bridge-multiplatform.AC4.2, cc-bridge-multiplatform.AC4.3

**Files:**
- Modify: `src/bridge/secrets.py:10-11`
- Modify: `src/bridge/state.py:7`
- Modify: `src/bridge/tasks.py:34,36`

**Implementation:**

In `src/bridge/secrets.py`, update the path constants and add fallback logic:

```python
import warnings

SECRETS_DIR = Path.home() / ".config" / "cc-bridge"
SECRETS_FILE = SECRETS_DIR / "secrets.json"

_OLD_SECRETS_DIR = Path.home() / ".config" / "claude-discord-bridge"
_OLD_SECRETS_FILE = _OLD_SECRETS_DIR / "secrets.json"
```

Update the `load()` function (or wherever secrets are read) to check the new path first, fall back to old path with a deprecation warning:

```python
def _resolve_secrets_path() -> Path:
    """Return the secrets file path, falling back to the old location with a warning."""
    override = os.environ.get("BRIDGE_SECRETS_PATH")
    if override:
        return Path(override).expanduser()
    if SECRETS_FILE.exists():
        return SECRETS_FILE
    if _OLD_SECRETS_FILE.exists():
        warnings.warn(
            f"Loading secrets from deprecated path {_OLD_SECRETS_FILE}. "
            f"Move to {SECRETS_FILE} to silence this warning.",
            DeprecationWarning,
            stacklevel=2,
        )
        return _OLD_SECRETS_FILE
    return SECRETS_FILE  # default (will error on read if missing)
```

Update all references to `SECRETS_FILE` in `load()` and validation functions to use `_resolve_secrets_path()`.

Also update all error messages referencing `'claude-discord-bridge init'` to `'cc-bridge init'` (lines 35, 40, 48, 53, 61, 68, 76).

In `src/bridge/state.py`, update the DB path:

```python
DEFAULT_DB_PATH = Path.home() / ".local" / "state" / "cc-bridge" / "state.db"
```

In `src/bridge/tasks.py`, update the state directories:

```python
TASK_SETTINGS_DIR = Path.home() / ".local" / "state" / "cc-bridge" / "task-settings"
ATTACHMENTS_DIR = Path.home() / ".local" / "state" / "cc-bridge" / "attachments"
```

**Testing:**

Tests must verify:
- cc-bridge-multiplatform.AC4.2: Secrets load from new path; if only old path exists, secrets load with deprecation warning
- cc-bridge-multiplatform.AC4.3: State path constants point to `~/.local/state/cc-bridge/`

Test file: `tests/test_secrets.py` (create or extend existing)

Follow project testing patterns: use `tmp_path` fixture for filesystem tests, `monkeypatch` for env vars. Use `warnings.catch_warnings()` to assert deprecation warning is emitted.

**Verification:**

```bash
uv run pytest tests/test_secrets.py -v
```

**Commit:** `feat: update config/state paths to cc-bridge with backward-compat fallback`

<!-- END_TASK_3 -->

<!-- START_TASK_4 -->
### Task 4: Update hook env vars with backward-compatible fallback

**Verifies:** cc-bridge-multiplatform.AC4.4

**Files:**
- Modify: `hooks/event.py:53`
- Modify: `hooks/pretooluse-approve.py:38`
- Modify: `src/bridge/tasks.py:642,1049,1057`

**Implementation:**

In `src/bridge/tasks.py`, update the env var name used when spawning tasks. The spawned pane should receive `CC_BRIDGE_TASK_ID`. For backward compat, also set the old name:

```python
# line 642 area — in the env dict passed to the layout/spawn
"CC_BRIDGE_TASK_ID": task_id,
"CC_DISCORD_TASK_ID": task_id,  # backward compat, remove in future release
```

Update lines 1049, 1057 (env_passthrough references) to include both:
```python
("CC_BRIDGE_TASK_ID", "CC_DISCORD_TASK_ID", "CLAUDE_PROJECT_DIR")
```

In `hooks/event.py`, update the env var lookup (line 53) to prefer new, fall back to old:

```python
for key in ("CC_BRIDGE_TASK_ID", "CC_DISCORD_TASK_ID", "CLAUDE_PROJECT_DIR"):
```

In `hooks/pretooluse-approve.py`, update the env var lookup (line 38):

```python
task_id = os.environ.get("CC_BRIDGE_TASK_ID") or os.environ.get("CC_DISCORD_TASK_ID")
```

**Testing:**

Tests must verify:
- cc-bridge-multiplatform.AC4.4: Hook scripts read `CC_BRIDGE_TASK_ID`; if absent, fall back to `CC_DISCORD_TASK_ID`

Test file: `tests/test_tasks.py` (extend existing task spawn tests)

Verify that the env dict produced by task spawning includes both `CC_BRIDGE_TASK_ID` and `CC_DISCORD_TASK_ID`.

**Verification:**

```bash
uv run pytest tests/test_tasks.py -v
```

**Commit:** `feat: rename hook env var to CC_BRIDGE_TASK_ID with backward compat`

<!-- END_TASK_4 -->

<!-- START_TASK_5 -->
### Task 5: Run full test suite to verify no regressions

**Files:**
- None (verification only)

**Verification:**

```bash
uv run pytest -v
```

Expected: All existing tests pass. Fix any failures caused by the path/env var renames before proceeding.

<!-- END_TASK_5 -->
<!-- END_SUBCOMPONENT_B -->

<!-- START_SUBCOMPONENT_C (tasks 6-8) -->
<!-- START_TASK_6 -->
### Task 6: Update CLI text references

**Files:**
- Modify: `src/bridge/cli.py:44,68,166,171,189,379`
- Modify: `src/bridge/zellij.py:23`

**Implementation:**

In `src/bridge/cli.py`, replace all `claude-discord-bridge` text references:

- Line 44: `"✅ cc-bridge init succeeded — you'll see future notifications here."`
- Line 68: Help text → `Writes to ~/.config/cc-bridge/secrets.json (mode 0600)`
- Line 166: `click.echo("  cc-bridge serve")`
- Line 171: `click.echo("  packaging/cc-bridge.service")`
- Line 189: Help text → `Loads secrets from ~/.config/cc-bridge/secrets.json`
- Line 379: Update path to `Path.home() / ".local" / "state" / "cc-bridge" / "task-settings"`

In `src/bridge/zellij.py`, update the default session name:

```python
SESSION_NAME = os.environ.get("BRIDGE_ZELLIJ_SESSION", "cc-bridge-worker")
```

**Verification:**

```bash
uv run pytest -v
```

**Commit:** `chore: update CLI text and zellij default session name to cc-bridge`

<!-- END_TASK_6 -->

<!-- START_TASK_7 -->
### Task 7: Rename packaging and deployment files

**Files:**
- Rename: `packaging/claude-discord-bridge.service` → `packaging/cc-bridge.service`
- Modify: `packaging/cc-bridge.service:8` (update ExecStart binary name)
- Modify: `scripts/install-systemd-user.sh:4-5,8`

**Implementation:**

Rename the service file:
```bash
git mv packaging/claude-discord-bridge.service packaging/cc-bridge.service
```

Update `packaging/cc-bridge.service` line 8:
```ini
ExecStart=%h/.local/bin/cc-bridge serve
```

Update `scripts/install-systemd-user.sh` to reference the new service name:
- Lines 4-5: `cc-bridge.service`
- Line 8: `systemctl --user enable --now cc-bridge`

Create a LaunchAgent plist template if one doesn't already exist, or update README to reference `local.cc-bridge.plist` (the README already documents the plist pattern).

**Verification:**

Verify file renamed:
```bash
ls packaging/cc-bridge.service
grep cc-bridge packaging/cc-bridge.service
grep cc-bridge scripts/install-systemd-user.sh
```

**Commit:** `chore: rename systemd service to cc-bridge`

<!-- END_TASK_7 -->

<!-- START_TASK_8 -->
### Task 8: Update documentation and skills

**Files:**
- Modify: `README.md` (extensive — ~30+ references)
- Modify: `CLAUDE.md` (project title, repo location, path references)
- Modify: `skills/SKILL.md` (daemon name, paths, error messages)

**Implementation:**

Global find-and-replace across each file:

In `README.md`:
- `claude-discord-bridge` → `cc-bridge` (for CLI/service/command references)
- Keep the git clone URL pointing to the actual repo name (it's `cc-discord`)
- Update the repo location path references if they mention `claude-discord-bridge`
- Update systemd/launchd unit names
- Update `CC_DISCORD_TASK_ID` references → `CC_BRIDGE_TASK_ID`

In `CLAUDE.md`:
- Line 1: Project title
- Path references: `~/.local/state/claude-discord-bridge/` → `~/.local/state/cc-bridge/`
- Path references: `~/.config/claude-discord-bridge/` → `~/.config/cc-bridge/`
- `CC_DISCORD_TASK_ID` → `CC_BRIDGE_TASK_ID` (mention backward compat)
- Service name references

In `skills/SKILL.md`:
- Line 8: daemon name → `cc-bridge`
- Line 28: shell command path
- Line 40: error message → `is 'cc-bridge serve' running?`

**Verification:**

```bash
grep -rn "claude-discord-bridge" README.md CLAUDE.md skills/SKILL.md
```

Expected: No matches remaining (except possibly in migration/backward-compat documentation noting the old name).

**Commit:** `docs: update all documentation references to cc-bridge`

<!-- END_TASK_8 -->
<!-- END_SUBCOMPONENT_C -->

<!-- START_TASK_9 -->
### Task 9: Final verification and integration commit

**Files:**
- None (verification only)

**Verification:**

Full codebase grep to catch any stragglers:

```bash
grep -rn "claude-discord-bridge" src/ hooks/ tests/ packaging/ scripts/
grep -rn "CC_DISCORD_TASK_ID" src/ hooks/
```

The first grep should return zero matches in source code (documentation may mention the old name in migration context).

The second grep should only return matches in backward-compat fallback code.

Run full test suite:

```bash
uv run pytest -v
```

Expected: All tests pass.

<!-- END_TASK_9 -->

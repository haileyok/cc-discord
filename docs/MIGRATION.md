# Migration Guide: claude-discord-bridge → cc-bridge

This guide covers migrating existing `claude-discord-bridge` installations to the new multiplatform `cc-bridge`.

## Overview

The package has been renamed from `claude-discord-bridge` to `cc-bridge` and refactored to support both Discord and Mattermost. Most configuration paths remain the same, but a few have moved or been renamed for consistency.

## Quick Start

```bash
# Update the package
pip install --upgrade cc-bridge
# or if using uv:
uv tool install --upgrade cc-bridge

# Migrate config directory (optional, backward compatible)
mv ~/.config/claude-discord-bridge ~/.config/cc-bridge

# Migrate state directory
mv ~/.local/state/claude-discord-bridge ~/.local/state/cc-bridge

# Update systemd/launchd service (if installed)
# See the service migration section below
```

## Step-by-step migration

### 1. Package installation and CLI command

**Old:**
```bash
pip install claude-discord-bridge
claude-discord-bridge init
claude-discord-bridge serve
```

**New:**
```bash
pip install cc-bridge
cc-bridge init
cc-bridge serve
```

Or with `uv`:
```bash
uv tool install cc-bridge
uv run cc-bridge init
uv run cc-bridge serve
```

### 2. Configuration directory

The bridge looks for secrets at `~/.config/cc-bridge/secrets.json` by default, but still accepts the old path at `~/.config/claude-discord-bridge/secrets.json` as a fallback for backward compatibility.

**Optional manual migration:**
```bash
mkdir -p ~/.config/cc-bridge
mv ~/.config/claude-discord-bridge/secrets.json ~/.config/cc-bridge/
rmdir ~/.config/claude-discord-bridge   # if empty
```

If you skip this, the old path will still work and the `init` command will update it the next time you run it.

### 3. State directory

The bridge expects task settings and attachments at `~/.local/state/cc-bridge/`. Migrate manually:

```bash
mv ~/.local/state/claude-discord-bridge ~/.local/state/cc-bridge
```

This is **required** — there is no backward-compatibility fallback for the state directory.

### 4. Environment variables

**Old env vars** (still supported during transition):

| Old | New | Status |
|---|---|---|
| `CC_DISCORD_TASK_ID` | `CC_BRIDGE_TASK_ID` | Both work; new var takes precedence |

**New required env var:**

| Variable | Value | Purpose |
|---|---|---|
| `BRIDGE_PLATFORM` | `discord` or `mattermost` | Selects which backend to use (default: `discord`) |

If you don't set `BRIDGE_PLATFORM`, it defaults to Discord, so existing workflows don't break.

### 5. Systemd user service

**Old:**
```bash
uv tool install claude-discord-bridge
bash scripts/install-systemd-user.sh
# Installed to: ~/.config/systemd/user/claude-discord-bridge.service
```

**New:**
```bash
uv tool install cc-bridge
bash scripts/install-systemd-user.sh
# Installs to: ~/.config/systemd/user/cc-bridge.service
```

To migrate:
```bash
# Unload the old service
systemctl --user stop claude-discord-bridge
systemctl --user disable claude-discord-bridge
rm ~/.config/systemd/user/claude-discord-bridge.service

# Install and load the new one
bash scripts/install-systemd-user.sh
systemctl --user daemon-reload
systemctl --user enable --now cc-bridge
```

### 6. macOS launchd agent

**Old:**
```bash
# Write ~/Library/LaunchAgents/local.claude-discord-bridge.plist
launchctl load ~/Library/LaunchAgents/local.claude-discord-bridge.plist
```

**New:**
```bash
# Write ~/Library/LaunchAgents/local.cc-bridge.plist
launchctl load ~/Library/LaunchAgents/local.cc-bridge.plist
```

To migrate:
```bash
# Unload the old agent
launchctl unload ~/Library/LaunchAgents/local.claude-discord-bridge.plist
rm ~/Library/LaunchAgents/local.claude-discord-bridge.plist

# Create the new plist (copy from packaging/local.cc-bridge.plist)
# and customize USERNAME
launchctl load ~/Library/LaunchAgents/local.cc-bridge.plist
```

See the README for the plist template and detailed instructions.

### 7. Zellij session name

The default zellij session name has changed from `meow` to `cc-bridge-worker`.

**If you explicitly set `BRIDGE_ZELLIJ_SESSION=meow`:** No action needed — your setting is respected.

**If you rely on the default:** The bridge will create a new session with the new name on first run. You can safely delete the old one:
```bash
zellij kill-session meow
```

(Only do this after the bridge has started at least once with the new default.)

### 8. Hook scripts

Hook script paths remain the same (`hooks/notify-stop.py`, `hooks/notify-notification.py`). They accept both old and new environment variable names during the transition, so no changes are needed.

```bash
# These are already in your ~/.claude/settings.json if you set them up before
# No changes needed — they'll work with both old and new env vars
```

### 9. Skills

The `/ask-discord` skill is symlinked to both `~/.claude/skills/ask-discord/` and `~/.claude/skills/ask-bridge/` for backward compatibility.

**Old:**
```bash
# Already symlinked during original setup
ls -l ~/.claude/skills/ask-discord/SKILL.md
```

**New:**
```bash
# Same location, plus an alias
ln -s /path/to/repo/skills/SKILL.md ~/.claude/skills/ask-bridge/SKILL.md
```

The `init` or `doctor` command will set these up automatically. You can use `/ask-discord` or `/ask-bridge` interchangeably.

## Choosing your backend

After migration, decide which backend(s) you want to use:

### Discord (default)

```bash
export BRIDGE_PLATFORM=discord
uv run cc-bridge serve
```

Behavior is identical to `claude-discord-bridge` — all existing Discord workflows continue unchanged.

### Mattermost

```bash
uv run cc-bridge init   # Answer "Mattermost" to the platform prompt
export BRIDGE_PLATFORM=mattermost
uv run cc-bridge serve
```

Mattermost supports text commands (`!start`, `!stop`, etc.) as an alternative to slash commands.

### Switching platforms

To switch between Discord and Mattermost without reinstalling:

1. Run `init` again and choose the new platform
2. Set `BRIDGE_PLATFORM=<platform>` before starting the daemon
3. Restart the daemon

The SQLite database and task history are shared across platforms.

## Troubleshooting

### "Unknown command: `claude-discord-bridge`"

The CLI command has changed. Use `cc-bridge` instead:
```bash
cc-bridge init
cc-bridge serve
cc-bridge doctor
```

### "~/.config/claude-discord-bridge/secrets.json: Permission denied"

The old path still works as a fallback, but it's better to migrate:
```bash
mkdir -p ~/.config/cc-bridge
mv ~/.config/claude-discord-bridge/secrets.json ~/.config/cc-bridge/
chmod 600 ~/.config/cc-bridge/secrets.json
```

### "BRIDGE_TASK_ID is not set"

If hook scripts are failing, check that they're using the new env var. The transition accepts both old and new names, but it's good practice to update:

Old: `$CC_DISCORD_TASK_ID`
New: `$CC_BRIDGE_TASK_ID`

### systemctl fails after migration

If `systemctl --user enable --now cc-bridge` fails:

```bash
# Reload systemd
systemctl --user daemon-reload

# Verify the service file
cat ~/.config/systemd/user/cc-bridge.service

# Check for syntax errors
systemd-analyze verify cc-bridge.service

# Try enabling separately
systemctl --user enable cc-bridge
systemctl --user start cc-bridge

# Check status
systemctl --user status cc-bridge
```

If you get "Operation not permitted," enable lingering:
```bash
sudo loginctl enable-linger $USER
```

## Rollback

If you need to roll back to `claude-discord-bridge`:

```bash
# Uninstall cc-bridge
pip uninstall cc-bridge
# or: uv tool uninstall cc-bridge

# Reinstall the old package (if still available)
pip install claude-discord-bridge

# Revert config (if you migrated)
mv ~/.config/cc-bridge ~/.config/claude-discord-bridge
mv ~/.local/state/cc-bridge ~/.local/state/claude-discord-bridge

# Revert systemd/launchd
# (Undo the service migration steps above)
```

The old data is fully compatible with `claude-discord-bridge`, so rolling back is safe.

## What's new in cc-bridge

- **Multiplatform support:** Discord and Mattermost backends share the same codebase and database.
- **ChatPlatform protocol:** New abstraction layer makes adding backends easier.
- **Mattermost text commands:** `!start`, `!stop`, `!skill`, etc. work without slash commands.
- **Emoji mapping:** Mattermost reactions automatically map between names (`:+1:`) and Unicode (👍).
- **Consistent configuration:** Same paths and env vars across platforms.

## Questions?

See the main README for setup and usage. Check CLAUDE.md for architecture and gotchas. Run `cc-bridge doctor` to verify your setup.

#!/usr/bin/env bash
set -euo pipefail

# Re-create skill symlinks (bind-mount volumes override image contents)
mkdir -p "$HOME/.claude/skills/ask-discord" "$HOME/.claude/skills/ask-bridge"
ln -sf /app/skills/SKILL.md "$HOME/.claude/skills/ask-discord/SKILL.md"
ln -sf /app/skills/SKILL.md "$HOME/.claude/skills/ask-bridge/SKILL.md"

# Re-seed onboarding config if missing
if [ ! -f "$HOME/.claude.json" ]; then
  echo '{"hasCompletedOnboarding":true,"lastOnboardingVersion":"0.0.0","autoUpdates":false}' \
    > "$HOME/.claude.json"
fi

exec "$@"

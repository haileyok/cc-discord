#!/usr/bin/env bash
# Foreground runner. Use under tmux/nohup if you want it to survive your shell.
set -euo pipefail
cd "$(dirname "$0")/.."

export BRIDGE_CLAUDE_COMMAND="${BRIDGE_CLAUDE_COMMAND:-claude-mode extend --modifier ~/.claude/output-styles/qdots-coding-partner.md --modifier bold --}"

exec uv run cc-bridge serve "$@"

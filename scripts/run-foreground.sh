#!/usr/bin/env bash
# Foreground runner. Use under tmux/nohup if you want it to survive your shell.
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run claude-slack-bridge serve "$@"

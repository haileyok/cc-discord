#!/usr/bin/env bash
# Stop the cc-bridge daemon and tear down the tmux-held zellij client.
set -euo pipefail

STATE_DIR="${HOME}/.local/state/cc-bridge"
PID_FILE="${STATE_DIR}/daemon.pid"
TMUX_SESSION="cc-bridge-worker"

if [[ -f "$PID_FILE" ]]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        echo "Daemon stopped (pid $PID)."
    else
        echo "Daemon not running (stale pid $PID)."
    fi
    rm -f "$PID_FILE"
else
    echo "No pid file found."
fi

if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    tmux kill-session -t "$TMUX_SESSION"
    echo "tmux session '$TMUX_SESSION' killed."
fi

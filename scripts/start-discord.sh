#!/usr/bin/env bash
# Launch cc-bridge for Discord:
#   1. Starts the bridge daemon in the background
#   2. Waits for the worker zellij session to appear
#   3. Attaches a tmux-held zellij client (detached — won't block your terminal)
#
# The daemon logs to ~/.local/state/cc-bridge/daemon.log.
# Stop with: scripts/stop-bridge.sh (or kill $(cat ~/.local/state/cc-bridge/daemon.pid))
# Inspect worker: tmux attach -t cc-bridge-worker
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="${HOME}/.local/state/cc-bridge"
PID_FILE="${STATE_DIR}/daemon.pid"
LOG_FILE="${STATE_DIR}/daemon.log"
WORKER_SESSION="${BRIDGE_ZELLIJ_SESSION:-cc-bridge-worker}"
TMUX_SESSION="cc-bridge-worker"

export BRIDGE_PLATFORM=discord

mkdir -p "$STATE_DIR"

# Check if daemon is already running
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Daemon already running (pid $(cat "$PID_FILE"))."
else
    echo "Starting cc-bridge daemon (discord)..."
    cd "$REPO_DIR"
    nohup uv run cc-bridge serve "$@" >> "$LOG_FILE" 2>&1 &
    DAEMON_PID=$!
    echo "$DAEMON_PID" > "$PID_FILE"
    echo "Daemon started (pid $DAEMON_PID), logging to $LOG_FILE"
fi

# Wait for the worker session to appear
echo "Waiting for worker session '$WORKER_SESSION'..."
for i in $(seq 1 30); do
    if zellij list-sessions 2>/dev/null | grep -q "$WORKER_SESSION"; then
        break
    fi
    if [[ $i -eq 30 ]]; then
        echo "Worker session did not appear within 30s. Check $LOG_FILE"
        exit 1
    fi
    sleep 1
done

# Attach zellij inside a detached tmux session so write-chars works
# without tying up the user's terminal
if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    echo "tmux session '$TMUX_SESSION' already holding the zellij client."
else
    tmux new-session -d -s "$TMUX_SESSION" "zellij attach $WORKER_SESSION"
    echo "Zellij client attached via tmux (session: $TMUX_SESSION)."
fi

echo "Ready. Inspect worker panes: tmux attach -t $TMUX_SESSION"

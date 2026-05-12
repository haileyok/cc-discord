#!/usr/bin/env bash
# Launch cc-bridge for Mattermost:
#   1. Starts the bridge daemon in the background
#   2. Waits for the daemon to create the worker zellij session
#   3. Attaches to the worker session (required for chat-driven mode)
#
# The daemon logs to ~/.local/state/cc-bridge/daemon.log.
# Stop with: kill $(cat ~/.local/state/cc-bridge/daemon.pid)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="${HOME}/.local/state/cc-bridge"
PID_FILE="${STATE_DIR}/daemon.pid"
LOG_FILE="${STATE_DIR}/daemon.log"
WORKER_SESSION="${BRIDGE_ZELLIJ_SESSION:-cc-bridge-worker}"

export BRIDGE_PLATFORM=mattermost

mkdir -p "$STATE_DIR"

# Check if daemon is already running
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Daemon already running (pid $(cat "$PID_FILE")). Attaching to worker session."
    exec zellij attach "$WORKER_SESSION"
fi

# Start daemon in background
echo "Starting cc-bridge daemon (mattermost)..."
cd "$REPO_DIR"
nohup uv run cc-bridge serve "$@" >> "$LOG_FILE" 2>&1 &
DAEMON_PID=$!
echo "$DAEMON_PID" > "$PID_FILE"
echo "Daemon started (pid $DAEMON_PID), logging to $LOG_FILE"

# Wait for the worker session to appear
echo "Waiting for worker session '$WORKER_SESSION'..."
for i in $(seq 1 30); do
    if zellij list-sessions 2>/dev/null | grep -q "$WORKER_SESSION"; then
        echo "Worker session ready. Attaching..."
        exec zellij attach "$WORKER_SESSION"
    fi
    sleep 1
done

echo "Worker session did not appear within 30s. Check $LOG_FILE for errors."
exit 1

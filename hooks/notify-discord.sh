#!/usr/bin/env bash
#
# Global Polytoken notification hook for the claude-discord-bridge.
#
# Registered in ~/.config/polytoken/hooks.json so it fires for EVERY Polytoken
# session (TUI, exec, headless daemon — not just the ones the Discord bridge
# drives). It covers two events:
#
#   - `stop`         the session finished a turn and is waiting for your input
#   - `notification` Polytoken raised a notification (e.g. a job completed)
#
# The hook forwards the event (JSON on stdin + the POLYTOKEN_* env vars) to the
# bridge's POST /v1/notify, which posts to Discord with an @mention. The bridge
# suppresses `stop` pings for the sessions it already renders inline, so you are
# not double-notified.
#
# This hook is side-effect only: it always exits 0, which is the "proceed
# normally" outcome for any event, so it never changes what Polytoken does next.
# The post is best-effort and fails fast if the bridge is down.
set -u

BRIDGE_URL="${BRIDGE_URL:-http://127.0.0.1:8787}"

# Forward the event JSON (stdin) as the body; carry the env context as headers
# so the bridge knows which session/project/event fired.
curl -fsS --connect-timeout 1 --max-time 2 \
  -H "Content-Type: application/json" \
  -H "X-Polytoken-Event: ${POLYTOKEN_HOOK_EVENT:-}" \
  -H "X-Polytoken-Session: ${POLYTOKEN_SESSION_ID:-}" \
  -H "X-Polytoken-Project: ${POLYTOKEN_PROJECT_DIR:-}" \
  -H "X-Polytoken-Non-Interactive: ${POLYTOKEN_NON_INTERACTIVE:-}" \
  --data-binary @- \
  "${BRIDGE_URL}/v1/notify" >/dev/null 2>&1 || true

exit 0

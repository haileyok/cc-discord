#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
install -m 0644 packaging/claude-slack-bridge.service \
    "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/claude-slack-bridge.service"
echo "Installed claude-slack-bridge.service. Try:"
echo "  systemctl --user daemon-reload"
echo "  systemctl --user enable --now claude-slack-bridge"
echo "If 'systemctl --user' fails with 'Operation not permitted', run 'loginctl enable-linger \$USER'"
echo "or use scripts/run-foreground.sh as a fallback."

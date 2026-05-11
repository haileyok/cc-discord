#!/usr/bin/env bash
set -euo pipefail
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
install -m 0644 packaging/cc-bridge.service \
    "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/cc-bridge.service"
echo "Installed. Try:"
echo "  systemctl --user daemon-reload"
echo "  systemctl --user enable --now cc-bridge"
echo "If 'systemctl --user' fails with 'Operation not permitted', run 'loginctl enable-linger \$USER'"
echo "or use scripts/run-foreground.sh as a fallback."

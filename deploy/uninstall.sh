#!/usr/bin/env bash
# Disable and remove the homehunt systemd user units.
set -euo pipefail

dest="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

systemctl --user disable --now homehunt-poll.timer homehunt-summary.timer 2>/dev/null || true
for f in homehunt-poll.service homehunt-poll.timer homehunt-summary.service homehunt-summary.timer; do
    rm -f "$dest/$f"
done
systemctl --user daemon-reload
echo "homehunt timers removed"

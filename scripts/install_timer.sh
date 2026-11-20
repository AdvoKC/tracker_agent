#!/usr/bin/env bash
# Install (or remove) the promise-monitor systemd USER units. Idempotent.
#   scripts/install_timer.sh                    daily run timer (+ crash alert)
#   scripts/install_timer.sh --with-dashboard   ...and the admin dashboard (127.0.0.1:8800)
#   scripts/install_timer.sh --remove           disable and delete all units
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$HOME/.config/systemd/user"
UNITS=(promise-monitor.service promise-monitor-alert.service promise-monitor.timer)
DASH=promise-monitor-dashboard.service

if [[ "${1:-}" == "--remove" ]]; then
  systemctl --user disable --now promise-monitor.timer "$DASH" 2>/dev/null || true
  for u in "${UNITS[@]}" "$DASH"; do rm -f "$DEST/$u"; done
  systemctl --user daemon-reload
  echo "removed promise-monitor timer and services"
  exit 0
fi

mkdir -p "$DEST"
for u in "${UNITS[@]}"; do
  sed "s#@REPO@#${REPO}#g" "$REPO/scripts/systemd/$u" > "$DEST/$u"
done
systemctl --user daemon-reload
systemd-analyze --user verify "$DEST/promise-monitor.service" "$DEST/promise-monitor.timer"
systemctl --user enable --now promise-monitor.timer
systemctl --user list-timers promise-monitor.timer --no-pager

if [[ "${1:-}" == "--with-dashboard" ]]; then
  sed "s#@REPO@#${REPO}#g" "$REPO/scripts/systemd/$DASH" > "$DEST/$DASH"
  systemctl --user daemon-reload
  systemctl --user enable "$DASH"
  systemctl --user restart "$DASH"
  sleep 3
  if systemctl --user is-active --quiet "$DASH"; then
    echo "dashboard running: http://localhost:8800"
  else
    echo "dashboard failed to start:"; journalctl --user -u "$DASH" -n 20 --no-pager
    exit 1
  fi
fi

if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]]; then
  echo
  echo "NOTE: the timer only fires while you are logged in. To run it without a login session:"
  echo "    sudo loginctl enable-linger $USER"
fi

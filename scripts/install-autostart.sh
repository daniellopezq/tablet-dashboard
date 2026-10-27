#!/usr/bin/env bash
# Start the dashboard (and the local AI models) at boot as systemd user services, restarting the
# dashboard after a crash, plus a daily log rotation timer. Usage: scripts/install-autostart.sh [--remove] [--no-local-ai]
set -euo pipefail
cd "$(dirname "$0")/.."
DIR=$(pwd)
UNITS="$HOME/.config/systemd/user"

if [ "${1:-}" = "--remove" ]; then
  systemctl --user disable --now tablet-dashboard.service tablet-local-ai.service tablet-logrotate.timer 2>/dev/null || true
  rm -f "$UNITS/tablet-dashboard.service" "$UNITS/tablet-local-ai.service" \
        "$UNITS/tablet-logrotate.service" "$UNITS/tablet-logrotate.timer"
  systemctl --user daemon-reload
  echo "autostart removed; start.sh/stop.sh are back to manual mode (nothing is running now)"
  exit 0
fi

mkdir -p "$UNITS" logs run
for u in tablet-dashboard.service tablet-local-ai.service tablet-logrotate.service tablet-logrotate.timer; do
  sed "s|@DIR@|$DIR|g" "deploy/systemd/$u" > "$UNITS/$u"
done
sed "s|@DIR@|$DIR|g" deploy/logrotate/tablet-dashboard.conf > run/logrotate.conf
systemctl --user daemon-reload
systemctl --user enable --now tablet-logrotate.timer

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != yes ]; then
  loginctl enable-linger "$USER" 2>/dev/null || echo "note: run 'sudo loginctl enable-linger $USER' so it starts at boot without a login" >&2
fi

# Hand a manually started dashboard over to systemd.
scripts/stop.sh >/dev/null 2>&1 || true
if [ "${1:-}" = "--no-local-ai" ]; then
  systemctl --user enable tablet-dashboard.service
  systemctl --user disable tablet-local-ai.service 2>/dev/null || true
else
  systemctl --user enable tablet-local-ai.service tablet-dashboard.service
  systemctl --user start tablet-local-ai.service
fi
systemctl --user start tablet-dashboard.service
systemctl --user --no-pager status tablet-dashboard.service | head -5

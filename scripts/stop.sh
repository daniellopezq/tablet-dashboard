#!/usr/bin/env bash
# Stop the dashboard started by start.sh. Only signals a PID whose command line is this server.
# With autostart installed this stops the systemd service (which then stays stopped until start.sh).
set -euo pipefail
cd "$(dirname "$0")/.."
PIDFILE=run/dashboard.pid

if systemctl --user is-enabled --quiet tablet-dashboard.service 2>/dev/null; then
  PID=$(systemctl --user show -p MainPID --value tablet-dashboard.service)
  systemctl --user stop tablet-dashboard.service
  echo "dashboard stopped (PID $PID, systemd)"
  exit 0
fi

if [ ! -f "$PIDFILE" ]; then
  echo "dashboard not running (no pidfile)"
  exit 0
fi
PID=$(cat "$PIDFILE")
if ! tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null | grep -q "server.py"; then
  echo "PID $PID is not the dashboard; removing stale pidfile"
  rm -f "$PIDFILE"
  exit 0
fi
kill -TERM "$PID"
for _ in $(seq 1 20); do
  kill -0 "$PID" 2>/dev/null || { echo "dashboard stopped (PID $PID)"; rm -f "$PIDFILE"; exit 0; }
  sleep 0.25
done
echo "dashboard did not exit after SIGTERM; sending SIGKILL"
kill -KILL "$PID" 2>/dev/null || true
rm -f "$PIDFILE"

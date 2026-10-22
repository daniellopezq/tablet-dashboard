#!/usr/bin/env bash
# Start the dashboard in the background. Log: logs/dashboard.log
# With autostart installed (scripts/install-autostart.sh) this starts the systemd user services instead.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs run
PIDFILE=run/dashboard.pid
[ -f config.json ] || { echo "config.json not found: cp config.example.json config.json, then edit it" >&2; exit 1; }
PORT=$(python3 -c 'import json; print(json.load(open("config.json")).get("port", 8099))')

wait_ready() {
  for _ in $(seq 1 40); do
    if [ -f "$PIDFILE" ] && curl -fsS -m 2 -o /dev/null "http://127.0.0.1:$PORT/api/time"; then
      echo "dashboard started (PID $(cat "$PIDFILE"))$1, log: $(pwd)/logs/dashboard.log"
      return 0
    fi
    sleep 0.5
  done
  echo "dashboard failed to start; last log lines:" >&2
  tail -n 20 logs/dashboard.log >&2
  return 1
}

if systemctl --user is-enabled --quiet tablet-dashboard.service 2>/dev/null; then
  if [ "${NO_LOCAL_AI:-0}" != 1 ]; then
    systemctl --user start tablet-local-ai.service || echo "warning: local fallback models did not start (dashboard continues)" >&2
  fi
  python3 server.py --check
  systemctl --user start tablet-dashboard.service
  wait_ready " via systemd"
  exit $?
fi

# Local fallback models (see README "Local fallback"). Skip with NO_LOCAL_AI=1. Never blocks the dashboard.
if [ "${NO_LOCAL_AI:-0}" != 1 ] && [ -x scripts/local-ai-start.sh ]; then
  scripts/local-ai-start.sh || echo "warning: local fallback models did not start (dashboard continues)" >&2
fi

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "dashboard already running (PID $(cat "$PIDFILE"))"
  exit 0
fi
rm -f "$PIDFILE"

python3 server.py --check
setsid nohup python3 server.py --pidfile "$PIDFILE" >> logs/dashboard.log 2>&1 < /dev/null &
wait_ready ""

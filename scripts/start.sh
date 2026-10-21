#!/usr/bin/env bash
# Start the dashboard in the background (no autostart). Log: logs/dashboard.log
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs run
PIDFILE=run/dashboard.pid

# Local fallback models (see README "Local fallback"). Skip with NO_LOCAL_AI=1. Never blocks the dashboard.
if [ "${NO_LOCAL_AI:-0}" != 1 ] && [ -x scripts/local-ai-start.sh ]; then
  scripts/local-ai-start.sh || echo "warning: local fallback models did not start (dashboard continues)" >&2
fi

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "dashboard already running (PID $(cat "$PIDFILE"))"
  exit 0
fi
rm -f "$PIDFILE"

[ -f config.json ] || { echo "config.json not found: cp config.example.json config.json, then edit it" >&2; exit 1; }
python3 server.py --check
PORT=$(python3 -c 'import json; print(json.load(open("config.json")).get("port", 8099))')
setsid nohup python3 server.py --pidfile "$PIDFILE" >> logs/dashboard.log 2>&1 < /dev/null &

for _ in $(seq 1 20); do
  if [ -f "$PIDFILE" ] && curl -fsS -m 2 -o /dev/null "http://127.0.0.1:$PORT/api/time"; then
    echo "dashboard started (PID $(cat "$PIDFILE")), log: $(pwd)/logs/dashboard.log"
    exit 0
  fi
  sleep 0.5
done
echo "dashboard failed to start; last log lines:" >&2
tail -n 20 logs/dashboard.log >&2
exit 1

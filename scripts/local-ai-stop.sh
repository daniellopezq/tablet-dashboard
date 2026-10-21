#!/usr/bin/env bash
# Stop the local fallback models started by local-ai-start.sh. Only signals PIDs whose command line
# is the expected server binary.
set -euo pipefail
cd "$(dirname "$0")/.."

stop_one() {  # name pidfile
  local name=$1 pidfile=$2 pid
  if [ ! -f "$pidfile" ]; then
    echo "$name not running (no pidfile)"
    return 0
  fi
  pid=$(cat "$pidfile")
  if ! tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "$name"; then
    echo "PID $pid is not $name; removing stale pidfile"
    rm -f "$pidfile"
    return 0
  fi
  kill -TERM "$pid"
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || { echo "$name stopped (PID $pid)"; rm -f "$pidfile"; return 0; }
    sleep 0.25
  done
  echo "$name did not exit after SIGTERM; sending SIGKILL"
  kill -KILL "$pid" 2>/dev/null || true
  rm -f "$pidfile"
}

stop_one llama-server run/llama-server.pid
stop_one whisper-server run/whisper-server.pid

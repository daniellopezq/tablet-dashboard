#!/usr/bin/env bash
# Start the local fallback models (no autostart): llama-server (chat) and whisper-server (speech),
# both on 127.0.0.1 only, at low CPU priority. Logs: logs/llama-server.log, logs/whisper-server.log
# Settings come from measurements on this server (2 threads beat 4 on the i3's 2 physical cores).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs run

# Model/build choices from config.json "local_ai" (the admin page sets them). Variables already set
# in the environment win. Values are read as data, never evaluated.
while IFS='=' read -r k v; do
  case "$k" in
    LOCAL_AI_DIR|LOCAL_AI_LLM|LOCAL_AI_STT|LOCAL_AI_LLM_BACKEND|LOCAL_AI_STT_BACKEND|LOCAL_AI_THREADS)
      [ -n "${!k:-}" ] || printf -v "$k" '%s' "$v" ;;
  esac
done < <(python3 localai.py env 2>/dev/null || true)

LOCAL_AI_DIR=${LOCAL_AI_DIR:-$(dirname "$PWD")/local-ai}
# Build dir suffix per server (README "Local fallback"): on this GTX 745, Vulkan made the LLM slower
# (Qwen3.5-2B Q4_K_M 9.4 vs 11.3 tok/s on CPU) but whisper ~2.2x faster (2.5 s vs 5.5 s per clip).
LLM_BACKEND=${LOCAL_AI_LLM_BACKEND:-cpu}
STT_BACKEND=${LOCAL_AI_STT_BACKEND:-vulkan}
THREADS=${LOCAL_AI_THREADS:-2}
# Qwen3.5-2B Q4_K_M: clearly more accurate than the 0.6B/0.8B/Gemma 1B candidates at ~11 tok/s (README).
LLM_MODEL=${LOCAL_AI_LLM:-$LOCAL_AI_DIR/models/Qwen3.5-2B-Q4_K_M.gguf}
STT_MODEL=${LOCAL_AI_STT:-$LOCAL_AI_DIR/models/ggml-base.bin}
LLM_PORT=8091
STT_PORT=8092

running() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

start_one() {  # name pidfile log port -- command...
  local name=$1 pidfile=$2 logfile=$3 port=$4; shift 5
  if running "$pidfile"; then
    echo "$name already running (PID $(cat "$pidfile"))"
    return 0
  fi
  rm -f "$pidfile"
  if ss -ltn "sport = :$port" | grep -q LISTEN; then
    echo "$name: port $port is already in use by another process" >&2
    return 1
  fi
  setsid nohup nice -n 10 "$@" >> "$logfile" 2>&1 < /dev/null &
  echo $! > "$pidfile"
}

start_one llama-server run/llama-server.pid logs/llama-server.log $LLM_PORT -- \
  "$LOCAL_AI_DIR/build/llama-$LLM_BACKEND/bin/llama-server" -m "$LLM_MODEL" --host 127.0.0.1 --port $LLM_PORT \
  -c 2048 -t "$THREADS" --parallel 1 --jinja --no-webui --cache-ram 256  # prompt cache defaults to 8 GiB
start_one whisper-server run/whisper-server.pid logs/whisper-server.log $STT_PORT -- \
  "$LOCAL_AI_DIR/build/whisper-$STT_BACKEND/bin/whisper-server" -m "$STT_MODEL" --host 127.0.0.1 --port $STT_PORT \
  -t "$THREADS" -l auto

ok_llm=0; ok_stt=0
for _ in $(seq 1 60); do
  [ $ok_llm = 1 ] || { curl -fsS -m 2 -o /dev/null "http://127.0.0.1:$LLM_PORT/health" 2>/dev/null && ok_llm=1; }
  [ $ok_stt = 1 ] || { curl -fsS -m 2 -o /dev/null "http://127.0.0.1:$STT_PORT/" 2>/dev/null && ok_stt=1; }
  [ $ok_llm = 1 ] && [ $ok_stt = 1 ] && break
  sleep 0.5
done
[ $ok_llm = 1 ] || { echo "llama-server did not become healthy; see logs/llama-server.log" >&2; exit 1; }
[ $ok_stt = 1 ] || { echo "whisper-server did not become ready; see logs/whisper-server.log" >&2; exit 1; }

# Warm-up: the first whisper request is slow (CPU ~9.5 s; Vulkan ~11 s while GPU shaders compile).
SAMPLE="$LOCAL_AI_DIR/src/whisper.cpp/samples/jfk.wav"
if [ -f "$SAMPLE" ]; then
  curl -fsS -m 60 -o /dev/null "http://127.0.0.1:$STT_PORT/inference" -F "file=@$SAMPLE" -F response_format=json || true
fi
echo "local AI started: llama-server PID $(cat run/llama-server.pid) on 127.0.0.1:$LLM_PORT," \
     "whisper-server PID $(cat run/whisper-server.pid) on 127.0.0.1:$STT_PORT"

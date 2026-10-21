#!/usr/bin/env bash
# Store (or remove) the AI Gateway API key in the private .env file next to server.py.
#   scripts/set-assistant-key.sh            prompt for a key (input hidden), save it
#   scripts/set-assistant-key.sh --remove   delete the key from the file
# The key is never echoed, never passed as a command-line argument, and the file is created
# with owner-only permissions (600). The server re-reads the file on every question, so no
# restart is needed unless AI_GATEWAY_API_KEY is also set in the server's environment.
set -euo pipefail
cd "$(dirname "$0")/.."
ENV_FILE=.env
NAME=${KEY_NAME:-AI_GATEWAY_API_KEY}   # e.g. KEY_NAME=GROQ_API_KEY scripts/set-assistant-key.sh
if ! [[ "$NAME" =~ ^[A-Z][A-Z0-9_]{1,63}$ ]]; then
  echo "KEY_NAME must look like AI_GATEWAY_API_KEY or GROQ_API_KEY." >&2
  exit 1
fi
umask 077

other_lines() {
  [ -f "$ENV_FILE" ] && { grep -v -E "^(export[[:space:]]+)?${NAME}[[:space:]]*=" "$ENV_FILE" || true; } || true
}

if [ "${1:-}" = "--remove" ]; then
  TMP=$(mktemp ./.env.XXXXXX)
  other_lines > "$TMP"
  if [ -s "$TMP" ]; then mv "$TMP" "$ENV_FILE"; chmod 600 "$ENV_FILE"; else rm -f "$TMP" "$ENV_FILE"; fi
  echo "Key removed from $(pwd)/$ENV_FILE."
  echo "Also revoke it in the Vercel dashboard (AI Gateway > API Keys)."
  exit 0
fi

if [ ! -t 0 ]; then
  echo "Run this in an interactive terminal so the key can be typed without echo." >&2
  exit 1
fi
read -r -s -p "Paste the AI Gateway API key (input is hidden): " KEY
echo
KEY=${KEY//[$'\r\n\t ']/}
if ! [[ "$KEY" =~ ^[A-Za-z0-9._~+/=-]{8,512}$ ]]; then
  echo "That doesn't look like a valid key (unexpected characters or length). Nothing was saved." >&2
  exit 1
fi

TMP=$(mktemp ./.env.XXXXXX)
{ other_lines; printf '%s=%s\n' "$NAME" "$KEY"; } > "$TMP"
chmod 600 "$TMP"
mv "$TMP" "$ENV_FILE"
echo "Saved ${#KEY} characters to $(pwd)/$ENV_FILE (permissions $(stat -c %a "$ENV_FILE"))."
unset KEY
python3 assistant.py clear-block >/dev/null 2>&1 || true
echo "No restart needed. Open the Ask tab to try it."

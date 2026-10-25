#!/usr/bin/env bash
# Install Piper (local text-to-speech) for spoken Ask answers: the self-contained Linux x86_64 release
# into $LOCAL_AI_DIR/piper/ and one Spanish (es_MX "claude") and one English (en_US "lessac") voice into
# $LOCAL_AI_DIR/models/piper/. No sudo. Safe to re-run: files already present and matching
# SHA256SUMS are kept.
#
# Checksums: the voice files are checked against Hugging Face's published SHA-256 (X-Linked-ETag);
# the Piper release has no published checksum, so its hash is recorded on first download and checked
# on later runs. All hashes are appended to $LOCAL_AI_DIR/models/SHA256SUMS.
set -euo pipefail
cd "$(dirname "$0")/.."

LOCAL_AI_DIR=${LOCAL_AI_DIR:-$(dirname "$PWD")/local-ai}
PIPER_URL=https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz
VOICE_BASE=https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0
# Spanish: "claude" (high quality), picked by ear over "ald" (medium) on 2026-10-23.
VOICES=(es/es_MX/claude/high/es_MX-claude-high en/en_US/lessac/medium/en_US-lessac-medium)

MODELS="$LOCAL_AI_DIR/models"
VOICE_DIR="$MODELS/piper"
SUMS="$MODELS/SHA256SUMS"
mkdir -p "$LOCAL_AI_DIR/piper" "$VOICE_DIR"
touch "$SUMS"

recorded() { awk -v f="$1" '$2 == f { print $1 }' "$SUMS" | tail -1; }
record() { grep -q "  $2\$" "$SUMS" || printf '%s  %s\n' "$1" "$2" >> "$SUMS"; }
sha() { sha256sum "$1" | cut -d' ' -f1; }

# fetch URL DEST NAME [EXPECTED_SHA]
fetch() {
  local url=$1 dest=$2 name=$3 want=${4:-}
  [ -n "$want" ] || want=$(recorded "$name")
  if [ -f "$dest" ] && [ -n "$want" ] && [ "$(sha "$dest")" = "$want" ]; then
    echo "ok       $name"
    record "$want" "$name"
    return
  fi
  echo "download $name"
  curl -fsSL --retry 2 -o "$dest.part" "$url"
  local got
  got=$(sha "$dest.part")
  if [ -n "$want" ] && [ "$got" != "$want" ]; then
    rm -f "$dest.part"
    echo "checksum mismatch for $name (expected $want, got $got)" >&2
    exit 1
  fi
  mv "$dest.part" "$dest"
  record "$got" "$name"
}

# Hugging Face publishes the SHA-256 of LFS files in the X-Linked-ETag header.
hf_sha() { curl -fsIL "$1" | tr -d '\r' | awk -F'"' 'tolower($0) ~ /^x-linked-etag:/ { print $2 }' | tail -1; }

tarball="$LOCAL_AI_DIR/piper/piper_linux_x86_64.tar.gz"
fetch "$PIPER_URL" "$tarball" "piper/piper_linux_x86_64.tar.gz"
if [ ! -x "$LOCAL_AI_DIR/piper/piper/piper" ]; then
  tar -xzf "$tarball" -C "$LOCAL_AI_DIR/piper"
fi

for v in "${VOICES[@]}"; do
  name=$(basename "$v")
  want=$(hf_sha "$VOICE_BASE/$v.onnx")
  [ -n "$want" ] || { echo "could not read the published checksum for $name.onnx" >&2; exit 1; }
  fetch "$VOICE_BASE/$v.onnx" "$VOICE_DIR/$name.onnx" "piper/$name.onnx" "$want"
  fetch "$VOICE_BASE/$v.onnx.json" "$VOICE_DIR/$name.onnx.json" "piper/$name.onnx.json"
done

echo "test:"
for v in "${VOICES[@]}"; do
  name=$(basename "$v")
  start=$(date +%s.%N)
  echo "Hola, esta es una prueba. Hello, this is a test." \
    | nice -n 10 "$LOCAL_AI_DIR/piper/piper/piper" --model "$VOICE_DIR/$name.onnx" --output_raw 2>/dev/null \
    | wc -c | awk -v n="$name" -v s="$start" '{ cmd = "date +%s.%N"; cmd | getline e; printf "  %s: %d bytes of audio in %.2f s\n", n, $1, e - s }'
done
echo "Piper installed in $LOCAL_AI_DIR/piper/piper, voices in $VOICE_DIR"

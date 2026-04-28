#!/bin/zsh
set -eo pipefail
BASE="${DAMYAN_BENCH_BASE:-http://127.0.0.1:1234}"
NAME="$1"
PAYLOAD_FILE="$2"
OUT="$SCRIPT_DIR/raw/${NAME}.json"
TIME_FILE="$SCRIPT_DIR/raw/${NAME}.time"
TIME=$(curl -sS -o "$OUT" -w '%{time_total}' "$BASE/v1/chat/completions" -H 'Content-Type: application/json' -d @"$PAYLOAD_FILE")
printf '%s\n' "$TIME" > "$TIME_FILE"
printf 'saved %s (%ss)\n' "$OUT" "$TIME"

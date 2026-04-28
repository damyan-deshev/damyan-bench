#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SUITE_DIR="$SCRIPT_DIR"
RUNNER="$SUITE_DIR/run_suite.py"
LOG_BASE="$SUITE_DIR/logs"
STAMP=$(date +%Y%m%d-%H%M%S)
RUN_DIR="$LOG_BASE/$STAMP"
MASTER_LOG="$RUN_DIR/master.log"
STATUS_FILE="$RUN_DIR/status.txt"

RUN_ARGS=()
[[ -n "${DAMYAN_BENCH_BASE:-}" ]] && RUN_ARGS+=(--base "$DAMYAN_BENCH_BASE")
[[ -n "${DAMYAN_BENCH_MODEL:-}" ]] && RUN_ARGS+=(--model "$DAMYAN_BENCH_MODEL")

mkdir -p "$RUN_DIR"
ln -sfn "$RUN_DIR" "$LOG_BASE/latest"
mkdir -p "$SUITE_DIR/reports"
rm -f "$SUITE_DIR/reports"/*.md "$SUITE_DIR/reports/status.txt"

ts() {
  date '+%Y-%m-%d %H:%M:%S'
}

log() {
  printf '[%s] %s\n' "$(ts)" "$*" | tee -a "$MASTER_LOG"
}

write_status() {
  printf '[%s] %s\n' "$(ts)" "$*" | tee "$STATUS_FILE" >/dev/null
}

run_phase() {
  local group="$1"
  local slug="$2"
  log "============================================================"
  log "PHASE START: $group"
  write_status "RUNNING phase=$slug group=$group"

  python3 "$RUNNER" "${RUN_ARGS[@]}" --group "$group" 2>&1 \
    | while IFS= read -r line; do
        printf '[%s] [%s] %s\n' "$(ts)" "$slug" "$line"
      done \
    | tee -a "$MASTER_LOG"

  mkdir -p "$RUN_DIR/reports-$slug"
  cp -f "$SUITE_DIR/reports"/*.md "$RUN_DIR/reports-$slug/"
  cp -f "$SUITE_DIR/reports/status.txt" "$RUN_DIR/reports-$slug/status.txt"
  log "PHASE DONE: $group"
  write_status "DONE phase=$slug group=$group"
}

log "Overnight suite run dir: $RUN_DIR"
write_status "BOOTSTRAP run_dir=$RUN_DIR"

run_phase "Code Authoring" "authoring"
run_phase "Code Debugging" "debugging"
run_phase "Tool Discipline / Agent Boundary" "tool-boundary"
run_phase "Defensive Programming / Secure Coding" "defensive"
run_phase "Offsec Operator Commands" "offsec-operator"

log "============================================================"
log "AGGREGATE START: full-suite summary from saved raw responses"
python3 "$RUNNER" "${RUN_ARGS[@]}" --resume 2>&1 \
  | while IFS= read -r line; do
      printf '[%s] [aggregate] %s\n' "$(ts)" "$line"
    done \
  | tee -a "$MASTER_LOG"
cp -f "$SUITE_DIR/reports"/*.md "$RUN_DIR/" 2>/dev/null || true
log "AGGREGATE DONE: full-suite summary refreshed"

cp -f "$SUITE_DIR/reports"/*.md "$RUN_DIR/" 2>/dev/null || true
log "ALL PHASES COMPLETE"
log "Summary: $SUITE_DIR/reports/summary.md"
write_status "COMPLETE summary=$SUITE_DIR/reports/summary.md"

#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SUITE_DIR="$SCRIPT_DIR"
LOG_BASE="$SUITE_DIR/forensics"
STAMP=$(date +%Y%m%d-%H%M%S)
RUN_DIR="$LOG_BASE/$STAMP"
LATEST_LINK="$LOG_BASE/latest"

ROUTER_LOG=${ROUTER_LOG:-/tmp/llama-server.log}
ROUTER_PORT=${ROUTER_PORT:-1234}
MODEL_PORT=${MODEL_PORT:-58767}
INTERVAL_MEM=${INTERVAL_MEM:-5}
INTERVAL_CONN=${INTERVAL_CONN:-2}
INTERVAL_PROC=${INTERVAL_PROC:-5}

mkdir -p "$RUN_DIR"
ln -sfn "$RUN_DIR" "$LATEST_LINK"

ts() {
  date '+%Y-%m-%d %H:%M:%S'
}

log() {
  printf '[%s] %s\n' "$(ts)" "$*" | tee -a "$RUN_DIR/master.log"
}

cleanup() {
  local code=$?
  log "Stopping forensic watchers"
  for pid_file in "$RUN_DIR"/*.pid; do
    [[ -f "$pid_file" ]] || continue
    pid=$(cat "$pid_file" 2>/dev/null || true)
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  wait || true
  log "Forensics stopped exit_code=$code"
}

trap cleanup EXIT INT TERM

log "Forensics run dir: $RUN_DIR"
log "Router log: $ROUTER_LOG"
log "Ports: router=$ROUTER_PORT model=$MODEL_PORT"

(
  stdbuf -oL tail -F "$ROUTER_LOG" \
    | awk '{ print strftime("[%Y-%m-%d %H:%M:%S]"), $0; fflush() }' \
    | tee -a "$RUN_DIR/router.log"
) &
echo $! > "$RUN_DIR/router.pid"
log "Started router log watcher pid=$(cat "$RUN_DIR/router.pid")"

(
  while true; do
    {
      printf '[%s] --- meminfo ---\n' "$(ts)"
      grep -E 'MemAvailable|MemFree|SwapFree|Committed_AS|CommitLimit|AnonPages|Shmem|Slab|SReclaimable|SUnreclaim' /proc/meminfo || true
      printf '[%s] --- pressure ---\n' "$(ts)"
      cat /proc/pressure/memory || true
      printf '[%s] --- vmstat ---\n' "$(ts)"
      vmstat 1 2 | tail -n 1 || true
      printf '\n'
    } | tee -a "$RUN_DIR/memory-pressure.log"
    sleep "$INTERVAL_MEM"
  done
) &
echo $! > "$RUN_DIR/memory.pid"
log "Started memory watcher pid=$(cat "$RUN_DIR/memory.pid")"

(
  while true; do
    {
      printf '[%s] --- ss ---\n' "$(ts)"
      if command -v ss >/dev/null 2>&1; then
        ss -tnp "( sport = :$ROUTER_PORT or sport = :$MODEL_PORT or dport = :$ROUTER_PORT or dport = :$MODEL_PORT )" || true
      elif command -v netstat >/dev/null 2>&1; then
        netstat -tnp 2>/dev/null | grep -E ":($ROUTER_PORT|$MODEL_PORT)\b" || true
      elif command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP -sTCP:ESTABLISHED 2>/dev/null | grep -E "(:$ROUTER_PORT|:$MODEL_PORT)" || true
      else
        echo 'no ss/netstat/lsof available'
      fi
      printf '\n'
    } | tee -a "$RUN_DIR/connections.log"
    sleep "$INTERVAL_CONN"
  done
) &
echo $! > "$RUN_DIR/connections.pid"
log "Started connection watcher pid=$(cat "$RUN_DIR/connections.pid")"

(
  while true; do
    {
      printf '[%s] --- processes ---\n' "$(ts)"
      ps -eo pid,ppid,%cpu,%mem,etime,cmd | grep -E 'llama-server|run_llama' | grep -v grep || true
      printf '[%s] --- rocm-smi ---\n' "$(ts)"
      rocm-smi --showuse --showmemuse 2>/dev/null || true
      printf '\n'
    } | tee -a "$RUN_DIR/processes.log"
    sleep "$INTERVAL_PROC"
  done
) &
echo $! > "$RUN_DIR/processes.pid"
log "Started process watcher pid=$(cat "$RUN_DIR/processes.pid")"

log "All forensic watchers started. Press Ctrl+C to stop."
while true; do
  sleep 60
done

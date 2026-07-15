#!/usr/bin/env bash
set -euo pipefail

SERVICE_DIR="${BGE_SERVICE_DIR:-/root/autodl-tmp/bge-service}"
PID_FILE="$SERVICE_DIR/service.pid"
LOG_FILE="$SERVICE_DIR/logs/server.log"

mkdir -p "$SERVICE_DIR/logs" "$SERVICE_DIR/indexes"
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "BGE service is already running (PID $(cat "$PID_FILE"))."
  exit 0
fi

export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
export HF_HUB_OFFLINE="1"
export TRANSFORMERS_OFFLINE="1"
export BGE_MODEL_ROOT="${BGE_MODEL_ROOT:-/root/autodl-tmp/models}"
export BGE_DEVICE="${BGE_DEVICE:-cuda:0}"
export BGE_MAX_LENGTH="${BGE_MAX_LENGTH:-1024}"
export BGE_BATCH_SIZE="${BGE_BATCH_SIZE:-8}"

cd "$SERVICE_DIR"
nohup "$SERVICE_DIR/.venv/bin/uvicorn" app:app \
  --host "${BGE_HOST:-127.0.0.1}" \
  --port "${BGE_PORT:-8001}" \
  --workers 1 \
  >"$LOG_FILE" 2>&1 < /dev/null &
echo $! > "$PID_FILE"
echo "BGE service starting (PID $(cat "$PID_FILE")); log: $LOG_FILE"

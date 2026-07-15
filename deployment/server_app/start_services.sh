#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${COAL_PROJECT_DIR:-/root/autodl-tmp/coal-regulation-ai}"
VENV_DIR="${COAL_VENV_DIR:-/root/autodl-tmp/bge-service/.venv}"
MINERU_DIR="${MINERU_DIR:-/root/autodl-tmp/mineru}"
RUNTIME_DIR="$PROJECT_DIR/.runtime"
LOG_DIR="$PROJECT_DIR/logs"

mkdir -p "$RUNTIME_DIR" "$LOG_DIR" "$PROJECT_DIR/data" "$PROJECT_DIR/backend/data"
set -a
source "$PROJECT_DIR/backend/.env"
set +a
# pymilvus reserves MILVUS_URI for remote http(s) endpoints.  Move the
# application value to a namespaced variable so a Milvus Lite file URI works.
if [[ -n "${MILVUS_URI:-}" ]]; then
  export COAL_MILVUS_URI="$MILVUS_URI"
  unset MILVUS_URI
fi
export PYTHONUNBUFFERED=1
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"

MONGODB_PID_FILE="$RUNTIME_DIR/mongodb.pid"
mongodb_pid="$(cat "$MONGODB_PID_FILE" 2>/dev/null || true)"
if [[ -n "$mongodb_pid" ]] \
  && kill -0 "$mongodb_pid" 2>/dev/null \
  && tr '\0' ' ' <"/proc/$mongodb_pid/cmdline" 2>/dev/null | grep -Fq "mongod"; then
  echo "mongodb is already running (PID $(cat "$MONGODB_PID_FILE"))."
else
  rm -f "$MONGODB_PID_FILE"
  mongod --auth \
    --dbpath "$PROJECT_DIR/backend/data/mongodb" \
    --bind_ip 127.0.0.1 \
    --port 27017 \
    --fork \
    --logpath "$LOG_DIR/mongodb.log" \
    --pidfilepath "$MONGODB_PID_FILE"
  echo "mongodb started (PID $(cat "$MONGODB_PID_FILE"))."
fi

start_process() {
  local name="$1"
  local workdir="$2"
  shift 2
  local pid_file="$RUNTIME_DIR/$name.pid"
  local log_file="$LOG_DIR/$name.log"
  local expected_process="$name"

  case "$name" in
    mineru) expected_process="mineru-api" ;;
    backend) expected_process="uvicorn" ;;
    worker) expected_process="v9_worker.py" ;;
  esac

  local current_pid
  current_pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ -n "$current_pid" ]] \
    && kill -0 "$current_pid" 2>/dev/null \
    && tr '\0' ' ' <"/proc/$current_pid/cmdline" 2>/dev/null | grep -Fq "$expected_process"; then
    echo "$name is already running (PID $(cat "$pid_file"))."
    return
  fi
  rm -f "$pid_file"

  cd "$workdir"
  nohup "$@" >"$log_file" 2>&1 < /dev/null &
  echo $! > "$pid_file"
  echo "$name starting (PID $(cat "$pid_file")); log: $log_file"
}

start_process mineru "$MINERU_DIR" \
  env \
  HOME="$MINERU_DIR/home" \
  MODELSCOPE_CACHE="$MINERU_DIR/models/modelscope" \
  HF_HOME="$MINERU_DIR/models/huggingface" \
  MINERU_MODEL_SOURCE=modelscope \
  "$MINERU_DIR/.venv/bin/mineru-api" --host 127.0.0.1 --port 51071

start_process backend "$PROJECT_DIR/backend" \
  "$VENV_DIR/bin/uvicorn" main:app --host 127.0.0.1 --port 8000 --workers 1

start_process worker "$PROJECT_DIR" \
  "$VENV_DIR/bin/python" "$PROJECT_DIR/v9_worker.py"

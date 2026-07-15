#!/usr/bin/env bash
set -euo pipefail

SERVICE_DIR="${BGE_SERVICE_DIR:-/root/autodl-tmp/bge-service}"
PID_FILE="$SERVICE_DIR/service.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "BGE service is not running."
  exit 0
fi

PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "Stopped BGE service (PID $PID)."
else
  echo "Stale PID file removed (PID $PID)."
fi
rm -f "$PID_FILE"

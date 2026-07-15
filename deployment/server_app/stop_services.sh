#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${COAL_PROJECT_DIR:-/root/autodl-tmp/coal-regulation-ai}"
RUNTIME_DIR="$PROJECT_DIR/.runtime"

for name in worker backend mineru mongodb; do
  pid_file="$RUNTIME_DIR/$name.pid"
  if [[ ! -f "$pid_file" ]]; then
    echo "$name is not running."
    continue
  fi
  pid="$(cat "$pid_file")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid"
    echo "Stopped $name (PID $pid)."
  fi
  rm -f "$pid_file"
done

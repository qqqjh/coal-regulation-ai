#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${COAL_PROJECT_DIR:-/root/autodl-tmp/coal-regulation-ai}"
RUNTIME_DIR="$PROJECT_DIR/.runtime"

for name in mongodb mineru backend worker; do
  pid_file="$RUNTIME_DIR/$name.pid"
  if [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
    echo "$name: running (PID $(cat "$pid_file"))"
  else
    echo "$name: stopped"
  fi
done

curl -fsS --max-time 5 http://127.0.0.1:8000/health || true
echo
curl -fsS --max-time 5 http://127.0.0.1:51071/health || true
echo

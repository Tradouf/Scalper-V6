#!/usr/bin/env bash
# Stoppe uniquement les process paper A/B (pas le live HL2).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
AB="$REPO/experiments/ab14"

for name in simplebot_paper llmbot_paper; do
  pidf="$AB/logs/${name}.pid"
  if [ -f "$pidf" ]; then
    pid=$(cat "$pidf")
    if kill -0 "$pid" 2>/dev/null; then
      echo "SIGINT $name pid=$pid"
      kill -INT "$pid" 2>/dev/null || true
      sleep 2
      kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$pidf"
  else
    echo "$name: pas de pidfile"
  fi
done
echo "done"

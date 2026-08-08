#!/usr/bin/env bash
# Démarre les 2 bras paper A/B (ne touche pas au simplebot LIVE / son lock).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"
AB="$REPO/experiments/ab14"
ENVF="$REPO/experiments/ab14_llm_vs_simple.env"

[ -d .venv ] && # shellcheck disable=SC1091
source .venv/bin/activate
set -a
# shellcheck disable=SC1090
source "$ENVF"
set +a

# sécurité : refuse si STATE_DIR pointe encore sur le live
if [[ "$SIMPLEBOT_STATE_DIR" == *"simplebot/state" ]]; then
  echo "❌ SIMPLEBOT_STATE_DIR ressemble au live — abort"
  exit 1
fi
if [[ "${SIMPLEBOT_DRY_RUN}" != "1" ]] || [[ "${LLMBOT_DRY_RUN}" != "1" ]]; then
  echo "❌ DRY_RUN doit être 1 pour l'A/B paper"
  exit 1
fi

mkdir -p "$AB/logs"

# PID files
A_PID="$AB/logs/simplebot_paper.pid"
B_PID="$AB/logs/llmbot_paper.pid"

if [ -f "$A_PID" ] && kill -0 "$(cat "$A_PID")" 2>/dev/null; then
  echo "simplebot paper déjà up pid=$(cat "$A_PID")"
else
  echo "▶ bras A simplebot paper → $SIMPLEBOT_STATE_DIR"
  nohup python3 -m simplebot.run --live-only \
    >> "$AB/logs/simplebot_paper.log" 2>&1 &
  echo $! > "$A_PID"
  echo "  pid=$(cat "$A_PID")"
fi

if [ -f "$B_PID" ] && kill -0 "$(cat "$B_PID")" 2>/dev/null; then
  echo "llmbot paper déjà up pid=$(cat "$B_PID")"
else
  echo "▶ bras B llmbot paper → $LLMBOT_STATE_DIR"
  nohup python3 -m llmbot.run \
    >> "$AB/logs/llmbot_paper.log" 2>&1 &
  echo $! > "$B_PID"
  echo "  pid=$(cat "$B_PID")"
fi

echo ""
echo "Logs:"
echo "  tail -f $AB/logs/simplebot_paper.log"
echo "  tail -f $AB/logs/llmbot_paper.log"
echo "Stop: bash experiments/ab14/stop_paper.sh"

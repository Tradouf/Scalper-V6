#!/bin/bash
# LLMBot — trading Hyperliquid piloté par LLM (filtre quant en amont).
cd "$(dirname "$0")"
[ -d .venv ] && source .venv/bin/activate
[ -f .env ] && set -a && source .env && set +a

if [ "$1" = "--live" ]; then
  if [ -z "$HL3_PRIVATE_KEY" ]; then
    echo "❌ HL3_PRIVATE_KEY manquant — wallet dédié requis."
    exit 1
  fi
  echo "⚠️  LIVE — ordres réels wallet HL3"
  export LLMBOT_DRY_RUN=0
else
  echo "DRY-RUN (LLMBOT_DRY_RUN=0 ou --live pour le réel)"
  export LLMBOT_DRY_RUN=1
fi

python3 -m llmbot.run
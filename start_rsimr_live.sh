#!/bin/bash
# RSI-MR live — DRY-RUN par défaut. --live exige un geste explicite.
cd "$(dirname "$0")"
[ -d .venv ] && source .venv/bin/activate
[ -f .env ] && set -a && source .env && set +a

if [ "$1" = "--live" ]; then
  if [ -z "$HL2_PRIVATE_KEY" ]; then
    echo "❌ HL2_PRIVATE_KEY manquant — wallet SimpleBot requis."
    exit 1
  fi
  echo "⚠️  ORDRES RÉELS — wallet HL2. Le verdict paper est prévu mi-septembre."
  echo "    Les fonds doivent être en MARGE PERP (pas en spot) : un solde spot"
  echo "    laisse l'equity perp à 0 et rien ne peut s'ouvrir."
  export RSIMR_DRY_RUN=0
else
  echo "DRY-RUN (aucun ordre réel — utiliser --live pour le réel)"
  export RSIMR_DRY_RUN=1
fi

exec python3 -u -m rsimr.run_live

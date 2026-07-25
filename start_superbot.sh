#!/bin/bash
# Lance SuperBot (optimiseur + trader 3 sleeves + double gate HMM).
# DRY-RUN par défaut — passer --live pour envoyer des ordres réels (wallet HL3).

cd "$(dirname "$0")"

if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

: "${SUPERBOT_SYMBOLS:=ALL}"
export SUPERBOT_SYMBOLS

if [ "$1" = "--live" ]; then
    if [ -z "$HL3_PRIVATE_KEY" ]; then
        echo "❌ HL3_PRIVATE_KEY manquant dans .env — SuperBot exige un 3ᵉ wallet."
        exit 1
    fi
    echo "⚠️  Mode LIVE — ordres réels sur le wallet SuperBot (HL3)."
    export SUPERBOT_DRY_RUN=0
else
    echo "Mode DRY-RUN (papier). Utiliser: $0 --live pour le réel."
    export SUPERBOT_DRY_RUN=1
fi

python3 -m superbot.run

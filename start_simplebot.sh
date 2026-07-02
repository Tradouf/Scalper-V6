#!/bin/bash
# Lance SimpleBot (optimiseur périodique + trader live sur second wallet).
# Dry-run par défaut — passer --live pour envoyer des ordres réels.

cd "$(dirname "$0")"

if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

if [ "$1" = "--live" ]; then
    if [ -z "$HL2_PRIVATE_KEY" ]; then
        echo "❌ HL2_PRIVATE_KEY manquant dans .env — SimpleBot exige un wallet séparé de la V6."
        exit 1
    fi
    echo "⚠️  Mode LIVE — ordres réels sur le wallet SimpleBot."
    export SIMPLEBOT_DRY_RUN=0
else
    echo "Mode DRY-RUN (aucun ordre envoyé). Utiliser: $0 --live pour le réel."
    export SIMPLEBOT_DRY_RUN=1
fi

python3 -m simplebot.run

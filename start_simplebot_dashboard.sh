#!/bin/bash
# Lance le dashboard SimpleBot (lecture seule) — http://localhost:8083
# Ne trade pas, ne requiert aucun wallet : lit uniquement simplebot/state/*.
# Charge .env pour refléter le même mode (DRY-RUN/LIVE) et univers que le bot.

cd "$(dirname "$0")"

if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

: "${SIMPLEBOT_DASHBOARD_PORT:=8083}"
export SIMPLEBOT_DASHBOARD_PORT

# Accès distant : définir dans .env (ou l'environnement)
#   SIMPLEBOT_DASHBOARD_PASSWORD=...   → active la Basic Auth (obligatoire hors LAN)
#   SIMPLEBOT_DASHBOARD_USER=...       → identifiant (défaut: simplebot)
#   SIMPLEBOT_DASHBOARD_HOST=...       → interface de bind (défaut: 0.0.0.0)
export SIMPLEBOT_DASHBOARD_PASSWORD SIMPLEBOT_DASHBOARD_USER SIMPLEBOT_DASHBOARD_HOST

python3 -m simplebot.dashboard

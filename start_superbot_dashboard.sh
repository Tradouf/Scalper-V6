#!/bin/bash
# Dashboard SuperBot (lecture seule) — http://localhost:8084
cd "$(dirname "$0")"
if [ -d ".venv" ]; then source .venv/bin/activate; fi
if [ -f ".env" ]; then set -a; source .env; set +a; fi
# SUPERBOT_DASHBOARD_PASSWORD requis pour bind hors localhost (défaut host: 127.0.0.1)
export SUPERBOT_DASHBOARD_PORT SUPERBOT_DASHBOARD_PASSWORD SUPERBOT_DASHBOARD_USER SUPERBOT_DASHBOARD_HOST
python3 -m superbot.dashboard

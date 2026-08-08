#!/bin/bash
# LIQFEED — capture du flux de liquidations Hyperliquid (collecte seule).
# Aucun ordre, aucune décision de trading : alimente rsimr/liq.db.
cd "$(dirname "$0")"
[ -d .venv ] && source .venv/bin/activate
[ -f .env ] && set -a && source .env && set +a
exec python3 -u -m rsimr.liqfeed

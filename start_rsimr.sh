#!/bin/bash
# Lance RSI-MR (rachat de survente 1h) — PAPER ONLY, aucun ordre réel.
cd "$(dirname "$0")"
if [ -d ".venv" ]; then
    source .venv/bin/activate
fi
python3 -m rsimr.run

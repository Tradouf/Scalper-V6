#!/bin/bash
# ── SalleDesMarches V6 — Script de démarrage ──
cd ~/SalleDesMarches_fixed
source .venv/bin/activate
set -a
source .env
set +a
python main_v6.py

#!/bin/bash
# Lance XSMom (momentum cross-sectionnel) — PAPER ONLY, aucun ordre réel.
cd "$(dirname "$0")"
if [ -d ".venv" ]; then
    source .venv/bin/activate
fi
python3 -m xsmom.run

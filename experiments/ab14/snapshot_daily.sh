#!/usr/bin/env bash
# Snapshot daily A/B → metrics.csv + daily/day_N.md
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"
AB="$REPO/experiments/ab14"
ENVF="$REPO/experiments/ab14_llm_vs_simple.env"
set -a
# shellcheck disable=SC1090
source "$ENVF"
set +a

python3 "$REPO/experiments/ab14/snapshot_daily.py" \
  --ab-root "$AB" \
  --start-equity "${AB_START_EQUITY}"

#!/usr/bin/env bash
# J0 — prépare l'expérience A/B paper (ne touche pas au simplebot LIVE).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"
AB="$REPO/experiments/ab14"
ENVF="$REPO/experiments/ab14_llm_vs_simple.env"

mkdir -p "$AB"/{simplebot_state,llmbot_state,logs,daily,report}

set -a
# shellcheck disable=SC1090
source "$ENVF"
set +a

echo "== git SHA =="
git rev-parse HEAD | tee "$AB/git_sha.txt"
date -u | tee "$AB/start_utc.txt"

echo "== snapshot best_params (filtré AB_SYMBOLS) =="
python3 "$REPO/experiments/ab14/filter_best_params.py" \
  --src "$REPO/simplebot/state/best_params.json" \
  --dst "$AB/simplebot_state/best_params.json" \
  --symbols "$AB_SYMBOLS"

echo "== seed live_state paper simplebot =="
python3 - <<PY
import json, time
from pathlib import Path
p = Path("$AB/simplebot_state/live_state.json")
if not p.exists():
    eq = float("$AB_START_EQUITY")
    p.write_text(json.dumps({
        "dry_run": True,
        "paper": {"positions": {}, "trades": []},
        "paper_equity": eq,
        "equity_history": [[time.time(), eq]],
        "last_ts": {},
        "paused_until": 0,
        "last_flip_ts": {},
        "last_close_ts": {},
        "live_tracked": {},
        "closed_trades": [],
        "live_disabled": {},
        "exec_stats": {"maker": 0, "taker": 0, "mixed": 0, "skip": 0},
    }, indent=2))
    print("created", p)
else:
    print("exists", p, "— non écrasé")
PY

echo "== seed llmbot state =="
python3 - <<PY
import json, time
from pathlib import Path
p = Path("$AB/llmbot_state/live_state.json")
if not p.exists():
    eq = float("$AB_START_EQUITY")
    p.write_text(json.dumps({
        "trades": [],
        "paper_positions": {},
        "equity": eq,
        "equity_history": [[time.time(), eq]],
        "paused_until": 0,
    }, indent=2))
    print("created", p)
else:
    print("exists", p, "— non écrasé")
PY

echo "== LocalAI health =="
curl -sf "${LOCALAI_BASE_URL%/v1}/v1/models" -o /dev/null && echo "LocalAI OK" || echo "⚠️ LocalAI down — llmbot restera en WAIT"

echo "== metrics.csv header =="
METRICS="$AB/report/metrics.csv"
if [ ! -f "$METRICS" ]; then
  echo "date,arm,equity,day_pnl,n_trades,n_wins,n_open,fees_est,notes" > "$METRICS"
fi

echo ""
echo "J0 prêt. Démarrer avec:"
echo "  bash experiments/ab14/start_paper.sh"
echo "Snapshot daily (cron 00:05 UTC):"
echo "  bash experiments/ab14/snapshot_daily.sh"

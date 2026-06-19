#!/usr/bin/env python3
"""
Funding harvest market-neutral — SENSIBILITÉ AUX FRAIS (maker vs taker).

Le harvest est break-even net de frais TAKER (0,045%/côté). Question : l'exécution
maker (passif) le fait-elle basculer en positif, ET tient-il le gate OOS ?

Plutôt que de supposer des fills maker parfaits (malhonnête), on trace la COURBE :
on rejoue le walk-forward complet (mêmes combos, même sélection OOS) à plusieurs
coûts effectifs par côté, et on lit à quel coût l'OOS passe positif / franchit le
gate. On interprète ensuite contre l'économie maker réelle de HL.

Repères HL (à staking/volume variables) :
  - taker  ≈ 0,045% (0.00045)
  - maker  ≈ 0,015% (0.00015)
  - maker rebate (tiers élevés) : ~0 à légèrement négatif
⚠️ Le maker n'est pas gratuit : sélection adverse (on fille quand le prix va contre
soi). Pour un harvest LENT (hold heures/jours) elle est faible vs le carry, mais
non nulle → lire la marge, pas le point limite.

Usage :
    source .venv/bin/activate
    python3 backtest/run_funding_harvest_maker.py --days 200
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backtest.cross_sectional import CrossSectionalWalkForward, build_funding_matrix, fetch_panel
from backtest.run_ao_sweep import _OHLCVClient, _floats
from execution.hyperliquid_adapter import HyperliquidReadAdapter


def main() -> None:
    ap = argparse.ArgumentParser(description="Funding harvest — sensibilité maker/taker")
    ap.add_argument("--symbols", default="BTC,ETH,SOL,BNB,AAVE,LINK,SUI,DOGE")
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--lookback", default="24,48,96,168")
    ap.add_argument("--k", type=int, default=2)
    ap.add_argument("--sign", default="1,-1")
    ap.add_argument("--folds", type=int, default=5)
    # Coûts effectifs par côté à balayer (taker → maker → rebate).
    ap.add_argument("--fees", default="0.00045,0.0003,0.00015,0.0,-0.0001")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    lookbacks = [int(x) for x in _floats(args.lookback)]
    signs = [int(x) for x in _floats(args.sign)]
    fees = _floats(args.fees)

    # Fetch UNE seule fois (anti-429), réutilisé pour tous les niveaux de frais.
    adapter = HyperliquidReadAdapter()
    closes = fetch_panel(_OHLCVClient(adapter), symbols, "1h", args.days)
    syms = list(closes.columns)
    fund = build_funding_matrix(adapter, syms, closes.index, args.days)
    valid = [s for s in syms if fund[s].notna().any()]
    closes, fund = closes[valid], fund[valid]
    print(f"\nPanel {valid}  {len(closes)} barres 1h, funding aligné (carry crédité)")

    combos, rank_matrices = [], []
    for lb in lookbacks:
        cum = fund.rolling(lb, min_periods=lb).sum()
        for sg in signs:
            combos.append({"lookback": lb, "rebal": lb, "k": args.k, "sign": sg})
            rank_matrices.append(cum)

    print(f"Grille = {len(combos)} combos · {args.folds} folds\n")
    hdr = f"{'coût/côté':>10} {'~label':>14} {'OOS pnl%':>9} {'folds+':>7} {'PF méd':>7} {'t-stat':>7}  GATE"
    print(hdr); print("-" * len(hdr))
    labels = {0.00045: "taker", 0.0003: "blend 50/50", 0.00015: "maker", 0.0: "maker~0", -0.0001: "rebate"}
    for fee in fees:
        rep = CrossSectionalWalkForward(fee_pct=fee).evaluate(
            closes, combos, n_folds=args.folds, rank_matrices=rank_matrices,
            carry_matrix=fund, strategy="cs_funding_harvest",
        )
        gate = "✅ PASS" if rep.passed else "❌"
        print(f"{fee*100:>9.3f}% {labels.get(fee,''):>14} {rep.oos_total_pnl:>9.2f} "
              f"{rep.positive_folds:>4}/{args.folds} {rep.oos_median_pf:>7.2f} {rep.oos_tstat:>7.2f}  {gate}")


if __name__ == "__main__":
    main()

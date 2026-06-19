#!/usr/bin/env python3
"""
Funding fade à SEUIL EXTRÊME avec carry (hyp. #1 raffinée) — walk-forward OOS.

Seuils exprimés en funding ANNUALISÉ (ex. 0.30 = 30%/an). Crédite le carry reçu
pendant la détention (le vrai edge). Voir backtest/funding_strategy.py.

Usage :
    source .venv/bin/activate
    python3 backtest/run_funding_fade_thr.py --days 200
    python3 backtest/run_funding_fade_thr.py --days 300 --entry 0.2,0.35,0.5 --hold 72,168,336
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backtest.backtester import Backtester
from backtest.cross_sectional import build_funding_matrix, fetch_panel
from backtest.funding_strategy import FundingFadeWalkForward
from backtest.run_ao_sweep import _OHLCVClient, _floats
from execution.hyperliquid_adapter import HyperliquidReadAdapter


def main() -> None:
    ap = argparse.ArgumentParser(description="Funding fade seuil extrême + carry — walk-forward OOS")
    ap.add_argument("--symbols", default="BTC,ETH,SOL,BNB,AAVE,LINK,SUI,DOGE")
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--entry", default="0.2,0.35,0.5", help="Seuils d'entrée (funding annualisé)")
    ap.add_argument("--exit-frac", type=float, default=0.33, help="exit_thr = exit_frac × entry_thr")
    ap.add_argument("--hold", default="72,168,336", help="max_hold (heures) à balayer")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--fee", type=float, default=Backtester.DEFAULT_FEE_PCT)
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    entries = _floats(args.entry)
    holds = [int(x) for x in _floats(args.hold)]

    adapter = HyperliquidReadAdapter()
    closes = fetch_panel(_OHLCVClient(adapter), symbols, "1h", args.days)
    syms = list(closes.columns)
    fund = build_funding_matrix(adapter, syms, closes.index, args.days)
    valid = [s for s in syms if fund[s].notna().any()]
    closes, fund = closes[valid], fund[valid]
    print(f"\nPanel {valid}  {len(closes)} barres 1h, funding aligné")

    combos = [{"entry_thr": e, "exit_thr": e * args.exit_frac, "max_hold": h}
              for e in entries for h in holds]
    print(f"Grille = {len(combos)} combos (entry×hold, exit={args.exit_frac:g}×entry)\n")

    rep = FundingFadeWalkForward(fee_pct=args.fee).evaluate(closes, fund, combos, n_folds=args.folds)
    print(rep.summary())


if __name__ == "__main__":
    main()

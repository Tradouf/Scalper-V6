#!/usr/bin/env python3
"""
Hypothèse #5 — momentum/reversal cross-sectionnel (panier multi-symboles), évalué
en walk-forward OOS net de frais.

Usage :
    source .venv/bin/activate
    python3 backtest/run_cross_sectional.py --interval 1h --days 200
    python3 backtest/run_cross_sectional.py --interval 4h --days 400 --k 2
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backtest.backtester import Backtester
from backtest.cross_sectional import CrossSectionalWalkForward, fetch_panel
from backtest.run_ao_sweep import _OHLCVClient, _floats
from execution.hyperliquid_adapter import HyperliquidReadAdapter


def main() -> None:
    ap = argparse.ArgumentParser(description="Momentum/reversal cross-sectionnel — walk-forward OOS")
    ap.add_argument("--symbols", default="BTC,ETH,SOL,BNB,AAVE,LINK,SUI,DOGE")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--lookback", default="12,24,48,96", help="Lookbacks (barres) à balayer")
    ap.add_argument("--rebal", default="", help="Rebal (barres) ; défaut = lookback (hold 1 lookback)")
    ap.add_argument("--k", type=int, default=2, help="Top/bottom k du panier")
    ap.add_argument("--sign", default="1,-1", help="1=momentum, -1=reversal (balayé)")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--fee", type=float, default=Backtester.DEFAULT_FEE_PCT)
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    lookbacks = [int(x) for x in _floats(args.lookback)]
    signs = [int(x) for x in _floats(args.sign)]
    rebals = [int(x) for x in _floats(args.rebal)] if args.rebal else None

    client = _OHLCVClient(HyperliquidReadAdapter())
    closes = fetch_panel(client, symbols, args.interval, args.days)
    print(f"\nPanel {list(closes.columns)}  {len(closes)} barres {args.interval} alignées")

    combos = []
    for lb in lookbacks:
        rb_list = rebals if rebals else [lb]
        for rb in rb_list:
            for sg in signs:
                combos.append({"lookback": lb, "rebal": rb, "k": args.k, "sign": sg})
    print(f"Grille = {len(combos)} combos (lookback×rebal×sign, k={args.k})\n")

    rep = CrossSectionalWalkForward(fee_pct=args.fee).evaluate(closes, combos, n_folds=args.folds)
    print(rep.summary())


if __name__ == "__main__":
    main()

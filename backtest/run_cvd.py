#!/usr/bin/env python3
"""
Hypothèse #2 — divergence prix/CVD (épuisement d'agresseurs) — walk-forward OOS.

Charge le tape de trades (data/orderflow_hf.db, 12 j) en barres OHLCV+CVD et juge la
divergence via le harnais. ⚠️ 12 j = ~1 régime → un PASS reste fragile, à re-tester
sur plus de données avant tout live.

Usage :
    source .venv/bin/activate
    python3 backtest/run_cvd.py --bar 300 --symbols BTC,ETH,SOL
    python3 backtest/run_cvd.py --bar 60 --lookback 20,40,80 --tp 0.004,0.006
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backtest.backtester import Backtester
from backtest.evaluator import WalkForwardEvaluator
from backtest.orderflow import load_cvd_bars
from backtest.run_ao_sweep import _floats


def main() -> None:
    ap = argparse.ArgumentParser(description="CVD divergence — walk-forward OOS par symbole")
    ap.add_argument("--symbols", default="BTC,ETH,SOL")
    ap.add_argument("--strategy", default="cvd_divergence",
                    choices=["cvd_divergence", "cvd_breakout"],
                    help="divergence (reversal) ou breakout (continuation confirmée par CVD)")
    ap.add_argument("--bar", type=int, default=300, help="Taille de barre (secondes)")
    ap.add_argument("--lookback", default="10,20,40", help="Fenêtres de divergence (barres)")
    ap.add_argument("--tp", default="0.004,0.006,0.008", help="TP (fraction prix)")
    ap.add_argument("--ratio", type=float, default=2.0, help="TP/SL")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--fee", type=float, default=Backtester.DEFAULT_FEE_PCT)
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    lookbacks = [int(x) for x in _floats(args.lookback)]
    tps = _floats(args.tp)
    combos = []
    for lb in lookbacks:
        for tp in tps:
            sl = tp / args.ratio if args.ratio > 0 else 0.0
            combos.append({"cvd_lookback": lb, "tp_pct": tp, "sl_pct": sl})

    bt = Backtester(None)
    ev = WalkForwardEvaluator(bt, fee_pct=args.fee)
    for sym in symbols:
        df = load_cvd_bars(sym, bar_sec=args.bar)
        if df is None or len(df) < 300:
            print(f"\n{sym} : données insuffisantes ({0 if df is None else len(df)} barres)")
            continue
        print(f"\n===== {sym} [{args.strategy}] : {len(df)} barres de {args.bar}s ({len(combos)} combos) =====")
        rep = ev.evaluate(df, sym, args.strategy, combos, n_folds=args.folds)
        print(rep.summary())


if __name__ == "__main__":
    main()

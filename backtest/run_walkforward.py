#!/usr/bin/env python3
"""
Évaluation walk-forward (out-of-sample, net de frais) d'une stratégie AO.

Le seul protocole digne de confiance avant d'envisager le live : sélectionne les
paramètres sur des fenêtres d'entraînement, les juge sur les fenêtres suivantes
jamais vues, agrège, et applique un gate OOS. Affiche aussi le PnL « in-sample »
(l'illusion qu'un balayage naïf rapporterait) pour mesurer l'overfit.

Usage :
    source .venv/bin/activate
    # zero-cross 1h (le motif qui semblait +18% in-sample)
    python3 backtest/run_walkforward.py --motif zerocross --interval 1h --days 200 \
        --tp 0.02,0.04,0.06 --ratio 1,2,3
    # threshold 5m
    python3 backtest/run_walkforward.py --motif threshold --interval 5m --days 17 \
        --tp 0.012,0.016 --ratio 2,3 --x-long 65,120 --x-short 60,120
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backtest.backtester import Backtester
from backtest.evaluator import WalkForwardEvaluator
from backtest.run_ao_sweep import _OHLCVClient, _floats
from execution.hyperliquid_adapter import HyperliquidReadAdapter


def main() -> None:
    ap = argparse.ArgumentParser(description="Walk-forward OOS d'une stratégie AO (net de frais)")
    ap.add_argument("--symbol", default="BTC")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--motif", default="zerocross", choices=["threshold", "zerocross"])
    ap.add_argument("--tp", default="0.02,0.04,0.06")
    ap.add_argument("--ratio", default="1,2,3", help="TP/SL → SL=TP/ratio")
    ap.add_argument("--x-long", default="65", help="motif threshold")
    ap.add_argument("--x-short", default="60", help="motif threshold")
    ap.add_argument("--fast", type=int, default=5)
    ap.add_argument("--slow", type=int, default=34)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--train-frac", type=float, default=0.6)
    ap.add_argument("--fee", type=float, default=Backtester.DEFAULT_FEE_PCT, help="Frais par côté")
    args = ap.parse_args()

    strat_id = "ao_zerocross" if args.motif == "zerocross" else "ao"
    bt = Backtester(_OHLCVClient(HyperliquidReadAdapter()))
    df = bt._fetch_ohlcv(args.symbol, args.interval, args.days)
    if df is None or len(df) < 200:
        print(f"Données insuffisantes ({0 if df is None else len(df)} candles).")
        return

    # Grille : on encode le ratio en couples (tp, sl). expand_grid attend des listes
    # indépendantes → on déplie tp×ratio en deux listes alignées via des tuples.
    tps = _floats(args.tp)
    ratios = _floats(args.ratio)
    tp_list, sl_list = [], []
    for tp in tps:
        for ratio in ratios:
            tp_list.append(tp)
            sl_list.append(tp / ratio if ratio > 0 else 0.0)

    # tp_pct & sl_pct restent APPARIÉS (un SL par TP/ratio) → liste de combos explicites.
    pairs = list(zip(tp_list, sl_list))

    if args.motif == "threshold":
        xls, xss = _floats(args.x_long), _floats(args.x_short)
        combos = [{"tp_pct": tp, "sl_pct": sl, "x_long": xl, "x_short": xs,
                   "fast": args.fast, "slow": args.slow}
                  for tp, sl in pairs for xl in xls for xs in xss]
    else:
        combos = [{"tp_pct": tp, "sl_pct": sl, "fast": args.fast, "slow": args.slow}
                  for tp, sl in pairs]

    ev = WalkForwardEvaluator(bt, fee_pct=args.fee)
    rep = ev.evaluate(
        df, args.symbol, strat_id, combos,
        n_folds=args.folds, train_frac=args.train_frac,
    )
    print(f"\n{len(df)} candles {args.interval} (~{len(df) * _interval_min(args.interval) / 1440:.0f}j)  "
          f"grille={len(combos)} combos\n")
    print(rep.summary())


def _interval_min(interval):
    return {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}.get(interval, 60)


if __name__ == "__main__":
    main()

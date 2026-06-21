#!/usr/bin/env python3
"""
TSMOM — walk-forward POOLÉ cross-univers, le test de significativité honnête (2026-06-20).

Le runner par symbole donne 9-12/12 positifs mais des t-stat per-symbole modestes (peu de
trades/symbole). Le test gold-standard d'une anomalie = POOLER les trades OOS de tous les
symboles. Pour CHAQUE symbole on coupe en folds, on choisit le lookback sur le TRAIN de
chaque fold (sélection honnête, pas de look-ahead), on applique au TEST jamais vu, et on
agrège TOUS les trades OOS de tous les symboles → moyenne, t-stat, bootstrap. Param choisi
par fold = la version la plus exposée à l'overfit (≠ le param fixe) → si ça tient ici, c'est
solide.

Usage : python3 backtest/run_tsmom_pooled.py --interval 1d --folds 6
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backtest.backtester import Backtester
from backtest.run_tsmom import build_combos, fetch_df, UNIVERSE
from execution.hyperliquid_adapter import HyperliquidReadAdapter


def oos_trades_for_symbol(bt, df, combos, n_folds, fee):
    """Walk-forward ancré : fold k → train [0..edge_k], test (edge_k..edge_{k+1}].
    Lookback choisi sur le train (meilleur total_pnl), jugé sur le test. Retourne les
    PnL des trades OOS concaténés sur tous les folds."""
    n = len(df)
    edges = [int(n * (i + 1) / (n_folds + 1)) for i in range(n_folds + 1)]
    oos = []
    for k in range(n_folds):
        tr0, tr1 = 0, edges[k]
        te0, te1 = edges[k], edges[k + 1]
        train = df.iloc[tr0:tr1].reset_index(drop=True)
        test = df.iloc[te0:te1].reset_index(drop=True)
        if len(train) < 60 or len(test) < 30:
            continue
        best, best_pnl = None, -1e9
        for c in combos:
            r = bt.run_on_df(train, "X", "tsmom", fee_pct=fee, **c)
            if r.total_pnl > best_pnl:
                best_pnl, best = r.total_pnl, c
        rt = bt.run_on_df(test, "X", "tsmom", fee_pct=fee, **best)
        oos += [t["pnl"] for t in rt.trades]
    return oos


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(UNIVERSE))
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--folds", type=int, default=6)
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    adapter = HyperliquidReadAdapter()
    bt = Backtester(None)
    fee = bt.DEFAULT_FEE_PCT
    combos = build_combos("tsmom")

    print(f"\nTSMOM walk-forward POOLÉ — {args.interval}, {args.folds} folds, "
          f"lookback choisi/fold sur train, frais {fee:.3%}/côté\n")

    allp = []
    for sym in symbols:
        df = fetch_df(adapter, sym, args.interval, args.limit)
        if len(df) < 60 * (args.folds + 1):
            print(f"  {sym}: skip ({len(df)} bg)"); continue
        tp = oos_trades_for_symbol(bt, df, combos, args.folds, fee)
        allp += tp
        s = sum(tp) * 100
        print(f"  {sym:>5}: {len(tp):>3} trades OOS, total {s:+.1f}%")

    p = np.array(allp)
    n = len(p)
    mean = p.mean()
    t = mean / (p.std(ddof=1) / math.sqrt(n))
    # Bootstrap : proba que la moyenne soit > 0 (rééchantillonnage des trades).
    rng = np.random.default_rng(0)
    boots = np.array([rng.choice(p, n, replace=True).mean() for _ in range(5000)])
    p_pos = (boots > 0).mean()
    print("\n" + "=" * 64)
    print(f"POOL OOS : {n} trades  |  moyenne {mean*100:+.3f}%/trade  "
          f"({mean/(2*fee):.0f}× le frais round-trip)")
    print(f"  win {np.mean(p>0):.0%}  |  total {p.sum()*100:+.0f}%  |  "
          f"t-stat {t:+.2f}  |  bootstrap P(moyenne>0)={p_pos:.1%}")
    verdict = "EDGE SIGNIFICATIF" if t >= 2.0 and p_pos >= 0.975 else "non concluant"
    print(f"  → {verdict}")


if __name__ == "__main__":
    main()

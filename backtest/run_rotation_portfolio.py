#!/usr/bin/env python3
"""
ROTATION — EMPILER diversification stratégies × coins (2026-06-21, #2).

Le TSMOM live diversifie les COINS avec UNE stratégie. Ici on teste si ajouter la diversification
de STRATÉGIES par-dessus améliore : pour chaque coin du panier, on calcule la méta-série (ensemble
de stratégies, equal ou contra), puis on combine equal-weight à travers les coins. Comparé au
portefeuille MONO-stratégie (tsmom_30 par coin) et au buy&hold panier. Tout dans le MÊME cadre
(positions ∈[-1,1], frais sur turnover) pour une comparaison juste. Split en folds.

Usage : python3 backtest/run_rotation_portfolio.py --interval 1d --folds 4 --L 90 --R 20
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backtest.run_tsmom import fetch_df
from backtest.run_tsmom_ensemble import DEPLOYED
from strategies.strategy_pool import build_pool, build_pool_deep, strat_returns, s_tsmom
from backtest.run_rotation import weighted_ensemble, perf
from execution.hyperliquid_adapter import HyperliquidReadAdapter


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--L", type=int, default=90)
    ap.add_argument("--R", type=int, default=20)
    ap.add_argument("--warmup", type=int, default=200)
    ap.add_argument("--deep", action="store_true", help="pool élargi (43 stratégies)")
    args = ap.parse_args()

    symbols = ([s.strip().upper() for s in args.symbols.split(",")] if args.symbols else DEPLOYED)
    bpy = {"1d": 365, "4h": 6 * 365, "1h": 24 * 365}.get(args.interval, 365)
    adapter = HyperliquidReadAdapter()

    legs = ["mono_tsmom", "ens_equal", "ens_contra", "buy_hold"]
    series = {m: [] for m in legs}   # par coin : Series de rendement (alignée à droite plus tard)
    for sym in symbols:
        df = fetch_df(adapter, sym, args.interval, args.limit)
        if len(df) < args.warmup + 50:
            continue
        df = df.reset_index(drop=True)
        pool = (build_pool_deep if args.deep else build_pool)(df)
        rets = {nm: strat_returns(df, pos) for nm, pos in pool.items()}
        Pall = pd.concat([pool[nm].reset_index(drop=True) for nm in pool], axis=1).mean(axis=1)
        series["mono_tsmom"].append(pd.Series(strat_returns(df, s_tsmom(df, 30))))
        series["ens_equal"].append(pd.Series(strat_returns(df, Pall)))
        series["ens_contra"].append(pd.Series(strat_returns(df, weighted_ensemble(df, pool, rets, args.L, args.R, "invperf", 1.0))))
        series["buy_hold"].append(pd.Series(strat_returns(df, pd.Series(1.0, index=range(len(df))))))

    # Portefeuille equal-weight à travers les coins (aligné à droite sur la longueur commune).
    minlen = min(min(len(s) for s in series[m]) for m in legs)
    port = {}
    for m in legs:
        M = pd.concat([s.iloc[-minlen:].reset_index(drop=True) for s in series[m]], axis=1)
        port[m] = M.mean(axis=1).values

    idx = list(range(args.warmup, minlen))
    fold_len = len(idx) // args.folds
    print(f"\nROTATION PORTEFEUILLE (stratégies × coins) — {args.interval}, {len(series['mono_tsmom'])} coins, "
          f"{minlen} barres, {args.folds} folds, L={args.L} R={args.R}\n")
    print(f"  {'leg':<12} {'FULL Sharpe/CAGR/DD':>24}   {'folds OOS: Sharpe moyen / %>0':>30}")
    for m in legs:
        fs, fc, fdd = perf(np.asarray(port[m])[args.warmup:], bpy)
        fold_sh = [perf(np.asarray(port[m])[idx[f*fold_len:(f+1)*fold_len]], bpy)[0] for f in range(args.folds)]
        fold_sh = np.asarray(fold_sh)
        print(f"  {m:<12} {fs:6.2f} {fc:6.0%} {fdd:5.0%}          {fold_sh.mean():6.2f}   {np.mean(fold_sh>0):4.0%}")


if __name__ == "__main__":
    main()

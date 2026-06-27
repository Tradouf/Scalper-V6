#!/usr/bin/env python3
"""
ROTATION — validation WALK-FORWARD multi-fold du tilt contrarian (2026-06-21, #1).

Le tilt contrarian (ens_contra) montrait OOS BTC 0,85 / ETH 1,16 sur UNE seule moitié/actif →
peut être du bruit (SE_Sharpe ~0,6-0,8). Validation rigoureuse : les méta-stratégies sont toutes
CAUSALES (poids depuis trailing window) → toute la série est OOS. On découpe chaque actif d'un
PANIER en K folds consécutifs et on pool tous les (actif × fold) → distribution des Sharpe OOS +
TAUX DE VICTOIRE contra vs equal. Robuste = gagne sur une nette majorité d'échantillons indépendants.

Usage : python3 backtest/run_rotation_wf.py --interval 1d --folds 4 --L 90 --R 20
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
from strategies.strategy_pool import build_pool, build_pool_deep, build_pool_xdeep, strat_returns
from backtest.run_rotation import weighted_ensemble, perf
from execution.hyperliquid_adapter import HyperliquidReadAdapter

_BUILDERS = {"base": build_pool, "deep": build_pool_deep, "xdeep": build_pool_xdeep}


def meta_returns(df, L, R, pool_name="deep"):
    """Séries de rendement net (causales) des 4 méta-stratégies sur un actif."""
    df = df.reset_index(drop=True)
    pool = _BUILDERS[pool_name](df)
    rets = {nm: strat_returns(df, pos) for nm, pos in pool.items()}
    Pall = pd.concat([pool[nm].reset_index(drop=True) for nm in pool], axis=1).mean(axis=1)
    return {
        "equal": strat_returns(df, Pall),
        "contra": strat_returns(df, weighted_ensemble(df, pool, rets, L, R, "invperf", 1.0)),
        "momtm": strat_returns(df, weighted_ensemble(df, pool, rets, L, R, "perf", 1.0)),
        "riskpar": strat_returns(df, weighted_ensemble(df, pool, rets, L, R, "rp")),
    }, len(df)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--L", type=int, default=90)
    ap.add_argument("--R", type=int, default=20)
    ap.add_argument("--warmup", type=int, default=200, help="barres ignorées au début (warmup indicateurs)")
    ap.add_argument("--deep", action="store_true", help="pool élargi (43 stratégies)")
    ap.add_argument("--pool", default=None, choices=["base", "deep", "xdeep"], help="choix explicite du pool")
    args = ap.parse_args()

    symbols = ([s.strip().upper() for s in args.symbols.split(",")] if args.symbols else DEPLOYED)
    bpy = {"1d": 365, "4h": 6 * 365, "1h": 24 * 365}.get(args.interval, 365)
    adapter = HyperliquidReadAdapter()
    metas = ["equal", "contra", "momtm", "riskpar"]

    samples = {m: [] for m in metas}   # liste de Sharpe par (actif × fold)
    pairwise = {"contra>equal": 0, "riskpar>equal": 0, "momtm>equal": 0, "tot": 0}
    for sym in symbols:
        df = fetch_df(adapter, sym, args.interval, args.limit)
        if len(df) < args.warmup + args.folds * (args.L + args.R):
            continue
        pool_name = args.pool or ("deep" if args.deep else "base")
        mr, n = meta_returns(df, args.L, args.R, pool_name)
        usable = range(args.warmup, n)
        idx = list(usable)
        fold_len = len(idx) // args.folds
        for f in range(args.folds):
            sl = idx[f * fold_len:(f + 1) * fold_len]
            sh = {m: perf(np.asarray(mr[m])[sl], bpy)[0] for m in metas}
            for m in metas:
                samples[m].append(sh[m])
            pairwise["contra>equal"] += sh["contra"] > sh["equal"]
            pairwise["riskpar>equal"] += sh["riskpar"] > sh["equal"]
            pairwise["momtm>equal"] += sh["momtm"] > sh["equal"]
            pairwise["tot"] += 1

    print(f"\nROTATION WALK-FORWARD — {args.interval}, {len(symbols)} coins, {args.folds} folds/coin, "
          f"L={args.L} R={args.R}")
    tot = pairwise["tot"]
    print(f"Échantillons OOS indépendants (actif × fold) : {tot}\n")
    print(f"  {'méta':<10} {'Sharpe OOS moyen':>16} {'médian':>8} {'% >0':>6} {'t-stat':>7}")
    for m in metas:
        arr = np.asarray(samples[m])
        t = arr.mean() / (arr.std(ddof=1) / np.sqrt(len(arr))) if arr.std() > 0 else 0.0
        print(f"  {m:<10} {arr.mean():>16.2f} {np.median(arr):>8.2f} {np.mean(arr>0):>6.0%} {t:>7.2f}")
    print("\n  Taux de victoire vs equal-weight (sur échantillons indépendants) :")
    for k in ["contra>equal", "riskpar>equal", "momtm>equal"]:
        print(f"    {k:<16} {pairwise[k]}/{tot} = {pairwise[k]/tot:.0%}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
ROTATION — GO/NO-GO vol-targeted (2026-06-21) : l'edge contrarian survit-il au cadre du LIVE ?

La recherche #1-#4 était en positions brutes ∈[-1,1] (non vol-targeted). Le TSMOM LIVE, lui, est
vol-targeted par coin (pos = signal × clip(target_vol/vol_réalisée, cap), equal-risk). Avant de
porter un module, on RE-VALIDE l'ensemble-contrarian DANS CE CADRE : chaque coin reçoit le signal
méta ∈[-1,1] (direction × conviction), puis on le vol-targete exactement comme le TSMOM, puis on
combine equal-risk. Si ens_contra double toujours ~le Sharpe du mono_tsmom à ½ le drawdown → GO.

Usage : python3 backtest/run_rotation_voltarget.py --interval 1d --folds 4 --L 90 --R 20 --deep
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


def voltarget_returns(df, meta_pos, vol_win, target_vol_bar, fee, cap=3.0):
    """Vol-targeting À LA TSMOM : signal méta ∈[-1,1] × clip(target_vol/vol_réalisée_coin, cap).
    Causal (signal & scalar en t-1). Renvoie le rendement net quotidien vol-targeted."""
    close = df["close"].astype(float).reset_index(drop=True)
    ret = close.pct_change().fillna(0.0)
    realized = ret.rolling(vol_win).std().shift(1)
    scalar = (target_vol_bar / realized).clip(upper=cap).fillna(0.0)
    target = (meta_pos.reset_index(drop=True) * scalar)        # position cible (signée, vol-targeted)
    pos = target.shift(1).fillna(0.0)
    turn = np.abs(np.diff(pos.values, prepend=0.0))
    return pos.values * ret.values - turn * fee


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--folds", type=int, default=4)
    ap.add_argument("--L", type=int, default=90)
    ap.add_argument("--R", type=int, default=20)
    ap.add_argument("--vol-win", type=int, default=30)
    ap.add_argument("--target-vol", type=float, default=0.20)
    ap.add_argument("--warmup", type=int, default=200)
    ap.add_argument("--deep", action="store_true")
    args = ap.parse_args()

    symbols = ([s.strip().upper() for s in args.symbols.split(",")] if args.symbols else DEPLOYED)
    bpy = {"1d": 365, "4h": 6 * 365}.get(args.interval, 365)
    target_vol_bar = args.target_vol / np.sqrt(bpy)
    adapter = HyperliquidReadAdapter()
    fee = 0.00045
    builder = build_pool_deep if args.deep else build_pool

    legs = ["mono_tsmom", "ens_equal", "ens_contra"]
    series = {m: [] for m in legs}
    for sym in symbols:
        df = fetch_df(adapter, sym, args.interval, args.limit)
        if len(df) < args.warmup + 50:
            continue
        df = df.reset_index(drop=True)
        pool = builder(df)
        rets = {nm: strat_returns(df, pos) for nm, pos in pool.items()}
        Pall = pd.concat([pool[nm].reset_index(drop=True) for nm in pool], axis=1).mean(axis=1)
        contra = weighted_ensemble(df, pool, rets, args.L, args.R, "invperf", 1.0)
        series["mono_tsmom"].append(pd.Series(voltarget_returns(df, s_tsmom(df, 30), args.vol_win, target_vol_bar, fee)))
        series["ens_equal"].append(pd.Series(voltarget_returns(df, Pall, args.vol_win, target_vol_bar, fee)))
        series["ens_contra"].append(pd.Series(voltarget_returns(df, contra, args.vol_win, target_vol_bar, fee)))

    minlen = min(min(len(s) for s in series[m]) for m in legs)
    port = {m: pd.concat([s.iloc[-minlen:].reset_index(drop=True) for s in series[m]], axis=1).mean(axis=1).values
            for m in legs}
    idx = list(range(args.warmup, minlen))
    fold_len = len(idx) // args.folds

    print(f"\nROTATION VOL-TARGETED (cadre LIVE) — {args.interval}, {len(series['mono_tsmom'])} coins, "
          f"{minlen} barres, {args.folds} folds, vol cible {args.target_vol:.0%}/an, pool={'deep' if args.deep else 'base'}\n")
    print(f"  {'leg':<12} {'FULL Sharpe/CAGR/DD':>22}   {'folds: Sharpe moy / %>0':>24}")
    for m in legs:
        fs, fc, fdd = perf(np.asarray(port[m])[args.warmup:], bpy)
        fold_sh = np.asarray([perf(np.asarray(port[m])[idx[f*fold_len:(f+1)*fold_len]], bpy)[0] for f in range(args.folds)])
        print(f"  {m:<12} {fs:6.2f} {fc:6.0%} {fdd:5.0%}          {fold_sh.mean():6.2f}   {np.mean(fold_sh>0):4.0%}")


if __name__ == "__main__":
    main()

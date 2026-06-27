#!/usr/bin/env python3
"""
ROTATION — plafond d'ÉLARGISSEMENT de l'univers (2026-06-26).

Analogue de run_tsmom_expansion mais pour la rotation contrarian vol-targeted. À combien de coins
le Sharpe portefeuille culmine-t-il ? (le TSMOM culminait ~20, déclinait vers 41). Informe la
feuille de route capital : jusqu'où élargir quand l'equity grossit. Univers liquidité-rangé
(run_tsmom.UNIVERSE, 41 coins) ; on agrège les N premiers, contrarian R=1 deep pool, vol-targeted.

Usage : python3 backtest/run_rotation_expansion.py --tiers 8,12,16,20,24,30,41
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backtest.run_tsmom import fetch_df, UNIVERSE
from strategies.strategy_pool import build_pool_deep, strat_returns, s_tsmom
from backtest.run_rotation import weighted_ensemble, perf
from backtest.run_rotation_voltarget import voltarget_returns
from execution.hyperliquid_adapter import HyperliquidReadAdapter


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--L", type=int, default=90)
    ap.add_argument("--vol-win", type=int, default=30)
    ap.add_argument("--target-vol", type=float, default=0.40)
    ap.add_argument("--warmup", type=int, default=300)
    ap.add_argument("--tiers", default="8,12,16,20,24,30,41")
    args = ap.parse_args()

    bpy = {"1d": 365}.get(args.interval, 365)
    target_vol_bar = args.target_vol / np.sqrt(bpy)
    adapter = HyperliquidReadAdapter()
    tiers = [int(x) for x in args.tiers.split(",")]

    # Série de rendement contrarian vol-targeted PAR COIN (ordre liquidité), une fois.
    contra, mono = [], []
    used = []
    for sym in UNIVERSE:
        df = fetch_df(adapter, sym, args.interval, args.limit)
        if len(df) < args.warmup + 100:
            continue
        df = df.reset_index(drop=True)
        pool = build_pool_deep(df)
        rets = {nm: strat_returns(df, pos) for nm, pos in pool.items()}
        cpos = weighted_ensemble(df, pool, rets, args.L, 1, "invperf", 1.0)
        contra.append(pd.Series(voltarget_returns(df, cpos, args.vol_win, target_vol_bar, 0.00045)))
        mono.append(pd.Series(voltarget_returns(df, s_tsmom(df, 30), args.vol_win, target_vol_bar, 0.00045)))
        used.append(sym)
        print(f"  {len(used):>2} {sym} ok", flush=True)

    minlen = min(len(s) for s in contra)
    half = minlen // 2

    def port(series_list, N, sl):
        M = pd.concat([s.iloc[-minlen:].reset_index(drop=True) for s in series_list[:N]], axis=1)
        return M.iloc[sl].mean(axis=1).values

    print(f"\nROTATION GLIDE PATH — {args.interval}, vol cible {args.target_vol:.0%}/an, "
          f"{len(used)} coins dispo, {minlen} barres\n")
    print(f"  {'N':>3} | {'FULL contra Sh/CAGR/DD':>24} | {'OOS contra Sh/CAGR/DD':>24} | {'OOS mono_tsmom Sh':>16}")
    for N in tiers:
        if N > len(used):
            continue
        cf = perf(port(contra, N, slice(args.warmup, None)), bpy)
        co = perf(port(contra, N, slice(half, None)), bpy)
        mo = perf(port(mono, N, slice(half, None)), bpy)
        print(f"  {N:>3} | {cf[0]:6.2f} {cf[1]:6.0%} {cf[2]:5.0%}        | "
              f"{co[0]:6.2f} {co[1]:6.0%} {co[2]:5.0%}        | {mo[0]:6.2f}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
TSMOM — modes d'EXPOSITION : symétrique (long/short) vs long-flat vs always-long (2026-06-20).

Thèse documentée : le TSMOM symétrique = assurance bear mais PLAFONNE l'upside bull (le short
combat le biais long structurel du crypto). Question : en crypto, un trend filter LONG-FLAT
(long en uptrend, FLAT en downtrend — pas de short) capture-t-il plus d'upside tout en esquivant
les crashs ? On compare 3 mappings de la MÊME direction de tendance, tout le reste identique
(vol-targeting equal-risk, frais sur turnover, signal causal en t-1), split OOS :

  - symetrique  : pos_dir = sign(trail)              ∈ {-1, 0, +1}   (déployé)
  - long_flat   : pos_dir = max(sign(trail), 0)      ∈ { 0, +1}      (overlay de régime)
  - always_long : pos_dir = +1                       (buy&hold vol-targeted, référence)

Usage : python3 backtest/run_tsmom_exposure.py --deployed --lookback 30
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backtest.backtester import Backtester
from backtest.run_tsmom import fetch_df, UNIVERSE
from backtest.run_tsmom_ensemble import DEPLOYED, perf, portfolio
from execution.hyperliquid_adapter import HyperliquidReadAdapter


def _trail_sign(close: pd.Series, lookback: int) -> pd.Series:
    return np.sign(close / close.shift(lookback) - 1.0).fillna(0.0)


def symbol_returns(df, pos_dir, vol_win, target_vol_bar, fee):
    close = df["close"].astype(float).reset_index(drop=True)
    ret = close.pct_change().fillna(0.0)
    realized = ret.rolling(vol_win).std().shift(1)
    scalar = (target_vol_bar / realized).clip(upper=3.0).fillna(0.0)
    pos = pos_dir.shift(1).fillna(0.0) * scalar
    gross = pos * ret
    turnover = pos.diff().abs().fillna(pos.abs())
    strat = gross - turnover * fee
    return strat.values, ret.values, turnover.values


def report(name, S, H, T, bpy, fee):
    ss, sc, sd = perf(S, bpy)
    ann_turn = float(np.sum(T)) / len(T) * bpy
    print(f"  {name:<12} Sharpe {ss:5.2f}  CAGR {sc:6.0%}  maxDD {sd:5.0%}  "
          f"| turnover {ann_turn:5.1f}x/an  frais {ann_turn*fee:4.1%}/an")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--deployed", action="store_true")
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--lookback", type=int, default=30)
    ap.add_argument("--vol-win", type=int, default=30)
    args = ap.parse_args()

    symbols = ([s.strip().upper() for s in args.symbols.split(",")] if args.symbols
               else DEPLOYED if args.deployed else UNIVERSE)
    bpy = {"1d": 365, "4h": 6 * 365, "1h": 24 * 365}.get(args.interval, 365)
    target_vol_bar = 0.20 / np.sqrt(bpy)
    adapter = HyperliquidReadAdapter()
    fee = Backtester(None).DEFAULT_FEE_PCT

    modes = {
        "symetrique": lambda s: s,
        "long_flat": lambda s: s.clip(lower=0.0),
        "always_long": lambda s: pd.Series(1.0, index=s.index),
    }
    rows = {m: [] for m in modes}
    for sym in symbols:
        df = fetch_df(adapter, sym, args.interval, args.limit)
        if len(df) < args.lookback + args.vol_win + 5:
            continue
        close = df["close"].astype(float).reset_index(drop=True)
        sgn = _trail_sign(close, args.lookback)
        for m, fmap in modes.items():
            rows[m].append(symbol_returns(df, fmap(sgn), args.vol_win, target_vol_bar, fee))

    minlen = min(len(r[0]) for r in rows["symetrique"])
    for m in modes:
        rows[m] = [(s[-minlen:], h[-minlen:], t[-minlen:]) for s, h, t in rows[m]]
    half = minlen // 2

    print(f"\nTSMOM EXPOSITION — {args.interval}, lookback {args.lookback}, vol cible 20%/an, "
          f"frais {fee:.3%}/côté")
    print(f"{'='*78}\nPORTEFEUILLE equal-risk — {len(rows['symetrique'])} coins, "
          f"{minlen} barres ≈ {minlen/365:.1f} ans")
    for window, sl in [("FULL", slice(None)),
                       ("IS  (1re moitié)", slice(0, half)),
                       ("OOS (2e moitié)", slice(half, None))]:
        print(f"\n  — {window} —")
        for m in modes:
            S, H, T = portfolio([(s[sl], h[sl], t[sl]) for s, h, t in rows[m]], bpy, "")
            report(m, S, H, T, bpy, fee)


if __name__ == "__main__":
    main()

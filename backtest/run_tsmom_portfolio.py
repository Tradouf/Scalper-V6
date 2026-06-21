#!/usr/bin/env python3
"""
TSMOM — portefeuille VOL-TARGETED, la métrique de déploiement (2026-06-20).

La somme equal-weight de PnL/trade surévalue les coins volatils et ignore le risque. Un
trend-follower se gère à RISQUE CONSTANT : chaque position est dimensionnée par l'inverse de
sa volatilité réalisée (vol-targeting), et le portefeuille combine les coins à risque égal.
On mesure alors ce qui compte vraiment : Sharpe annualisé, max drawdown, CAGR — vs buy&hold.

Param FIXE (lookback unique, pas de sélection) = pas d'overfit. Causalité : la position du
jour t vient du signal calculé à la CLÔTURE de t-1 (shift), appliquée au rendement de t.

Usage : python3 backtest/run_tsmom_portfolio.py --interval 1d --lookback 30
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
from execution.hyperliquid_adapter import HyperliquidReadAdapter


def symbol_daily_returns(df, lookback, vol_win, target_vol_bar, fee):
    """Rendements quotidiens (par barre) de la stratégie vol-targeted sur un symbole.
    Renvoie (strat_ret, hold_ret) alignés. Pas de look-ahead : signal & sizing en t-1."""
    close = df["close"].astype(float).reset_index(drop=True)
    ret = close.pct_change().fillna(0.0)
    # État TSMOM persistant (signe du rendement trailing), DÉCALÉ d'une barre (causal).
    state = np.sign(close / close.shift(lookback) - 1.0).fillna(0.0)
    pos_dir = state.shift(1).fillna(0.0)
    # Vol-targeting : scalaire = vol cible / vol réalisée (en t-1), borné.
    realized = ret.rolling(vol_win).std().shift(1)
    scalar = (target_vol_bar / realized).clip(upper=3.0).fillna(0.0)
    pos = pos_dir * scalar
    gross = pos * ret
    # Frais sur le CHANGEMENT de position (turnover) × coût/côté.
    turnover = pos.diff().abs().fillna(pos.abs())
    cost = turnover * fee
    strat = gross - cost
    return strat.values, ret.values


def perf(daily, bars_per_year):
    daily = np.asarray(daily)
    if daily.std() == 0:
        return 0.0, 0.0, 0.0
    sharpe = daily.mean() / daily.std() * np.sqrt(bars_per_year)
    eq = np.cumprod(1 + daily)
    cagr = eq[-1] ** (bars_per_year / len(daily)) - 1
    peak = np.maximum.accumulate(eq)
    mdd = np.max((peak - eq) / peak)
    return sharpe, cagr, mdd


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=",".join(UNIVERSE))
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--lookback", type=int, default=30)
    ap.add_argument("--vol-win", type=int, default=30)
    args = ap.parse_args()

    bpy = {"1d": 365, "4h": 6 * 365, "1h": 24 * 365}.get(args.interval, 365)
    target_vol_bar = 0.20 / np.sqrt(bpy)   # 20 %/an de vol cible par position
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    adapter = HyperliquidReadAdapter()
    bt = Backtester(None)
    fee = bt.DEFAULT_FEE_PCT

    print(f"\nTSMOM portefeuille VOL-TARGETED — {args.interval}, lookback={args.lookback}, "
          f"vol cible 20%/an, frais {fee:.3%}/côté\n")
    print(f"{'sym':>5} {'Shrp_strat':>10} {'Shrp_hold':>10} {'CAGR_s':>8} {'CAGR_h':>8} {'MDD_s':>7} {'MDD_h':>7}")

    strat_mat, hold_mat = [], []
    for sym in symbols:
        df = fetch_df(adapter, sym, args.interval, args.limit)
        if len(df) < 200:
            continue
        s, h = symbol_daily_returns(df, args.lookback, args.vol_win, target_vol_bar, fee)
        ss, sc, sd = perf(s, bpy); hs, hc, hd = perf(h, bpy)
        print(f"{sym:>5} {ss:>10.2f} {hs:>10.2f} {sc:>8.0%} {hc:>8.0%} {sd:>7.0%} {hd:>7.0%}")
        strat_mat.append(pd.Series(s)); hold_mat.append(pd.Series(h))

    # Portefeuille = moyenne equal-risk des rendements (aligné sur l'index commun).
    S = pd.concat(strat_mat, axis=1).fillna(0.0).mean(axis=1).values
    H = pd.concat(hold_mat, axis=1).fillna(0.0).mean(axis=1).values
    ss, sc, sd = perf(S, bpy); hs, hc, hd = perf(H, bpy)
    print("\n" + "=" * 64)
    print(f"PORTEFEUILLE equal-risk ({len(strat_mat)} coins) :")
    print(f"  TSMOM vol-targeted : Sharpe {ss:.2f}  CAGR {sc:.0%}  maxDD {sd:.0%}")
    print(f"  buy&hold equal-wt  : Sharpe {hs:.2f}  CAGR {hc:.0%}  maxDD {hd:.0%}")
    print(f"  → TSMOM {'AMÉLIORE' if ss > hs else 'N AMÉLIORE PAS'} le Sharpe "
          f"({ss:.2f} vs {hs:.2f}) et le drawdown ({sd:.0%} vs {hd:.0%})")


if __name__ == "__main__":
    main()

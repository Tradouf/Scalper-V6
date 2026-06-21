#!/usr/bin/env python3
"""
TSMOM — test ENSEMBLE multi-lookback vs single-lookback (recherche 2026-06-20).

Hypothèse (Moskowitz-Ooi-Pedersen / Baltas-Kosowski) : combiner le SIGNE de la tendance sur
plusieurs horizons produit une direction continue dans [-1,+1] qui (a) diversifie le signal
entre horizons et (b) LISSE les retournements → moins de flips → MOINS DE TURNOVER → moins de
frais. Vu la leçon de la session (les frais sont l'ennemi, l'horizon est l'ami), le critère
de décision n'est pas seulement le Sharpe brut mais le COUPLE (Sharpe net, turnover).

Parité avec run_tsmom_portfolio : vol-targeting equal-risk, frais sur turnover, signal causal
(décalé d'une barre). Seule la DIRECTION change :
  - baseline : pos_dir = sign(close/close.shift(L) − 1)                       (binaire)
  - ensemble : pos_dir = mean_k sign(close/close.shift(L_k) − 1)             (continu)

Split OOS : on calcule les métriques sur l'ÉCHANTILLON COMPLET, puis sur la 1re moitié (IS)
et la 2e moitié (OOS) pour vérifier que le gain n'est pas un mirage in-sample.

Usage :
  python3 backtest/run_tsmom_ensemble.py --interval 1d --lookback 30 --ensemble 10,30,60,90
  python3 backtest/run_tsmom_ensemble.py --deployed   # 12 coins live
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

DEPLOYED = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE",
            "LINK", "AVAX", "LTC", "AAVE", "SUI", "ARB"]


def _pos_single(close: pd.Series, lookback: int) -> pd.Series:
    """Direction binaire {-1,0,+1} = signe du rendement trailing sur `lookback`."""
    return np.sign(close / close.shift(lookback) - 1.0).fillna(0.0)


def _pos_ensemble(close: pd.Series, lookbacks: list[int]) -> pd.Series:
    """Direction continue dans [-1,+1] = moyenne des signes sur plusieurs horizons."""
    sigs = [np.sign(close / close.shift(lb) - 1.0).fillna(0.0) for lb in lookbacks]
    return pd.concat(sigs, axis=1).mean(axis=1)


def symbol_returns(df, pos_dir, vol_win, target_vol_bar, fee):
    """Rendements quotidiens vol-targeted d'un symbole, à partir d'une DIRECTION pré-calculée.
    Renvoie (strat_ret, hold_ret, turnover) alignés, sans look-ahead (dir & sizing en t-1)."""
    close = df["close"].astype(float).reset_index(drop=True)
    ret = close.pct_change().fillna(0.0)
    realized = ret.rolling(vol_win).std().shift(1)
    scalar = (target_vol_bar / realized).clip(upper=3.0).fillna(0.0)
    pos = pos_dir.shift(1).fillna(0.0) * scalar          # position causale (signal en t-1)
    gross = pos * ret
    turnover = pos.diff().abs().fillna(pos.abs())
    strat = gross - turnover * fee
    return strat.values, ret.values, turnover.values


def perf(daily, bars_per_year):
    daily = np.asarray(daily, dtype=float)
    if len(daily) == 0 or daily.std() == 0:
        return 0.0, 0.0, 0.0
    sharpe = daily.mean() / daily.std() * np.sqrt(bars_per_year)
    eq = np.cumprod(1 + daily)
    cagr = eq[-1] ** (bars_per_year / len(daily)) - 1
    peak = np.maximum.accumulate(eq)
    mdd = np.max((peak - eq) / peak)
    return sharpe, cagr, mdd


def portfolio(rows, bpy, label):
    """rows = liste de (strat, hold, turnover) par symbole, alignés par index commun."""
    S = pd.concat([pd.Series(s) for s, _, _ in rows], axis=1).fillna(0.0).mean(axis=1)
    H = pd.concat([pd.Series(h) for _, h, _ in rows], axis=1).fillna(0.0).mean(axis=1)
    T = pd.concat([pd.Series(t) for _, _, t in rows], axis=1).fillna(0.0).mean(axis=1)
    return S.values, H.values, T.values


def report(name, S, H, T, bpy, fee):
    ss, sc, sd = perf(S, bpy)
    hs, hc, hd = perf(H, bpy)
    ann_turn = float(np.sum(T)) / len(T) * bpy           # turnover annualisé (x equity)
    ann_fee = ann_turn * fee
    print(f"  {name:<14} Sharpe {ss:5.2f}  CAGR {sc:6.0%}  maxDD {sd:5.0%}  "
          f"| turnover {ann_turn:5.1f}x/an  frais {ann_fee:5.1%}/an")
    return ss, sd, ann_turn


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--deployed", action="store_true", help="12 coins live")
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--lookback", type=int, default=30, help="baseline single lookback")
    ap.add_argument("--ensemble", default="10,30,60,90", help="lookbacks de l'ensemble (CSV)")
    ap.add_argument("--vol-win", type=int, default=30)
    args = ap.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.deployed:
        symbols = DEPLOYED
    else:
        symbols = UNIVERSE
    ens = [int(x) for x in args.ensemble.split(",") if x.strip()]

    bpy = {"1d": 365, "4h": 6 * 365, "1h": 24 * 365}.get(args.interval, 365)
    target_vol_bar = 0.20 / np.sqrt(bpy)
    adapter = HyperliquidReadAdapter()
    bt = Backtester(None)
    fee = bt.DEFAULT_FEE_PCT

    print(f"\nTSMOM ENSEMBLE — {args.interval}, baseline L={args.lookback}, "
          f"ensemble L={ens}, vol cible 20%/an, frais {fee:.3%}/côté")
    print(f"univers = {len(symbols)} coins")

    base_rows, ens_rows = [], []
    for sym in symbols:
        df = fetch_df(adapter, sym, args.interval, args.limit)
        if len(df) < max(ens + [args.lookback]) + args.vol_win + 5:
            continue
        close = df["close"].astype(float).reset_index(drop=True)
        base_rows.append(symbol_returns(df, _pos_single(close, args.lookback),
                                        args.vol_win, target_vol_bar, fee))
        ens_rows.append(symbol_returns(df, _pos_ensemble(close, ens),
                                       args.vol_win, target_vol_bar, fee))

    # Aligne les longueurs (coins d'âges différents) en tronquant à la plus courte série.
    minlen = min(len(r[0]) for r in base_rows)
    base_rows = [(s[-minlen:], h[-minlen:], t[-minlen:]) for s, h, t in base_rows]
    ens_rows = [(s[-minlen:], h[-minlen:], t[-minlen:]) for s, h, t in ens_rows]
    half = minlen // 2

    print(f"\n{'='*78}\nPORTEFEUILLE equal-risk — {len(base_rows)} coins, "
          f"{minlen} barres ≈ {minlen/365:.1f} ans")
    Sb, Hb, _ = portfolio(base_rows, bpy, "")
    print(f"  buy&hold equal-wt : Sharpe {perf(Hb, bpy)[0]:.2f}  "
          f"CAGR {perf(Hb, bpy)[1]:.0%}  maxDD {perf(Hb, bpy)[2]:.0%}")

    for window, sl in [("FULL", slice(None)),
                       ("IS  (1re moitié)", slice(0, half)),
                       ("OOS (2e moitié)", slice(half, None))]:
        br = [(s[sl], h[sl], t[sl]) for s, h, t in base_rows]
        er = [(s[sl], h[sl], t[sl]) for s, h, t in ens_rows]
        Sb2, Hb2, Tb2 = portfolio(br, bpy, "")
        Se2, He2, Te2 = portfolio(er, bpy, "")
        print(f"\n  — {window} —")
        report(f"base L={args.lookback}", Sb2, Hb2, Tb2, bpy, fee)
        report("ensemble", Se2, He2, Te2, bpy, fee)


if __name__ == "__main__":
    main()

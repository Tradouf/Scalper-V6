#!/usr/bin/env python3
"""
TSMOM — GLIDE PATH d'élargissement de l'univers selon l'equity (2026-06-20).

Problème : à $536 d'equity les 41 coins en equal-risk tombent sous le min HL $10/position → le
TSMOM resterait flat. La diversification EST l'edge (mémoire : 12→41 coins fait t-stat +2,48→+4,66,
maxDD 12%→4%) mais elle n'est ACCESSIBLE qu'au-dessus d'un seuil d'equity. Ce script calcule, sur
données réelles :
  1. par coin, le scalar vol-target TYPIQUE (médiane) → la taille relative attendue ;
  2. pour une grille d'equity, combien de coins de chaque palier (12/16/.../41) restent ≥ $10
     (avec un facteur de conviction de l'ensemble qui RÉDUIT la taille effective) ;
  3. le Sharpe/maxDD/CAGR backtesté du portefeuille des coins VIABLES à ce palier (split OOS).
⇒ donne un glide path : « à $X, élargir à N coins, perf attendue = … ».

Univers liquidité-rangé (run_tsmom.UNIVERSE) : les coins 13+ sont plus petits/volatils → scalar
plus bas → tombent sous $10 en premier → exigent plus d'equity.

Usage : python3 backtest/run_tsmom_expansion.py --interval 1d --conviction 0.7
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
from backtest.run_tsmom_ensemble import perf, portfolio
from execution.hyperliquid_adapter import HyperliquidReadAdapter


def symbol_returns(df, sgn, vol_win, target_vol_bar, fee):
    close = df["close"].astype(float).reset_index(drop=True)
    ret = close.pct_change().fillna(0.0)
    realized = ret.rolling(vol_win).std().shift(1)
    scalar = (target_vol_bar / realized).clip(upper=3.0).fillna(0.0)
    pos = sgn.shift(1).fillna(0.0) * scalar
    gross = pos * ret
    turnover = pos.diff().abs().fillna(pos.abs())
    strat = gross - turnover * fee
    return strat.values, ret.values, turnover.values, float(np.nanmedian(scalar.replace(0, np.nan)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--lookback", type=int, default=30)
    ap.add_argument("--vol-win", type=int, default=30)
    ap.add_argument("--min-notional", type=float, default=10.0)
    ap.add_argument("--conviction", type=float, default=0.7,
                    help="facteur de conviction moyen de l'ensemble (réduit la taille effective)")
    ap.add_argument("--tiers", default="12,16,20,24,30,41")
    args = ap.parse_args()

    bpy = {"1d": 365, "4h": 6 * 365, "1h": 24 * 365}.get(args.interval, 365)
    target_vol_bar = 0.20 / np.sqrt(bpy)
    adapter = HyperliquidReadAdapter()
    fee = Backtester(None).DEFAULT_FEE_PCT
    tiers = [int(x) for x in args.tiers.split(",")]

    # Fetch + métriques par coin (dans l'ordre liquidité de UNIVERSE).
    data = []   # (sym, strat, hold, turn, med_scalar)
    for sym in UNIVERSE:
        df = fetch_df(adapter, sym, args.interval, args.limit)
        if len(df) < args.lookback + args.vol_win + 5:
            continue
        close = df["close"].astype(float).reset_index(drop=True)
        sgn = np.sign(close / close.shift(args.lookback) - 1.0).fillna(0.0)
        s, h, t, msc = symbol_returns(df, sgn, args.vol_win, target_vol_bar, fee)
        data.append((sym, s, h, t, msc))

    print(f"\nTSMOM GLIDE PATH élargissement — {args.interval}, lookback {args.lookback}, "
          f"vol cible 20%/an, min ${args.min_notional:.0f}/pos, conviction ×{args.conviction}")
    print(f"{len(data)} coins disponibles (ordre liquidité)\n")

    # 1) Seuil d'equity de viabilité par coin : base_i = (E/N)×scalar×conv ≥ min → E ≥ min·N/(scalar·conv).
    #    On l'évalue AU palier où le coin entre (N = son rang). Donne « equity minimale pour ce palier ».
    print(f"{'tier N':>6} {'coin entrant':>12} {'med_scalar':>10} {'E_min viable':>13}")
    for i, (sym, *_rest, msc) in enumerate(data, start=1):
        if i in tiers or i == len(data):
            e_min = args.min_notional * i / max(msc * args.conviction, 1e-9)
            print(f"{i:>6} {sym:>12} {msc:>10.2f} {'$'+format(e_min, ',.0f'):>13}")

    # 2) Perf backtestée par palier. HISTORIQUE COMPLET par coin (concat aligné à droite +
    #    fillna(0) = un coin absent ne contribue pas, comme run_tsmom_portfolio / la mémoire).
    #    Le split OOS = 2e moitié de l'index commun le plus long.
    print(f"\n{'='*72}\nPerf portefeuille par palier (N premiers coins, equal-risk, HISTORIQUE COMPLET) :")
    maxlen = max(len(s) for _, s, *_ in data)
    half = maxlen // 2

    def align(arr):
        """Aligne à DROITE (les coins jeunes ont des NaN à gauche, ignorés par mean)."""
        s = pd.Series(arr)
        return s.reindex(range(len(s) - maxlen, len(s))).reset_index(drop=True) if len(s) < maxlen else s.reset_index(drop=True)

    for N in tiers:
        if N > len(data):
            continue
        strat = pd.concat([align(s) for _, s, _, _, _ in data[:N]], axis=1)
        line = f"  N={N:>3} : "
        for window, sl in [("FULL", slice(None)), ("OOS", slice(half, None))]:
            S = strat.iloc[sl].mean(axis=1, skipna=True).fillna(0.0).values
            ss, sc, sd = perf(S, bpy)
            line += f"{window} Sharpe {ss:5.2f} CAGR {sc:6.0%} maxDD {sd:4.0%}   "
        print(line)


if __name__ == "__main__":
    main()

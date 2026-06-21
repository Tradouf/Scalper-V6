#!/usr/bin/env python3
"""
TSMOM — vol-targeting PORTEFEUILLE vs PAR-COIN (recherche 2026-06-20, risque corrélation).

Risque documenté : en régime directionnel le TSMOM tient 12 positions de MÊME signe (ex. toutes
short) → fortement corrélées. L'equal-risk par-coin dimensionne chaque position à 20%/an de vol
SUPPOSANT l'indépendance ; mais la vol du PORTEFEUILLE = somme corrélée >> 20% → drawdowns plus
gros que la cible. Correctif classique (Baltas-Kosowski) : viser la vol au niveau PORTEFEUILLE —
scaler tout le book par target_port_vol / vol_réalisée_du_book (rolling). Ça shrink le gross quand
les corrélations montent (pari concentré) et l'augmente quand le book se décorrèle.

C'est aussi du vol-timing (Moreira-Muir) : si la vol prédit négativement le rendement, scaler
inversement à la vol ajoute du Sharpe. Churn : le multiplicateur bouge lentement (rolling) → coût
faible. On compare net de frais, split OOS.

  - par_coin   : pos_i = sign_i × clip(target_vol_bar/vol_i, 3)            (déployé)
  - port_vol   : par_coin PUIS book scalé par clip(target_port/vol_book_realisée, cap)

Usage : python3 backtest/run_tsmom_portvol.py --deployed --lookback 30 --port-vol 0.20
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
from backtest.run_tsmom_ensemble import DEPLOYED, perf
from execution.hyperliquid_adapter import HyperliquidReadAdapter


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--deployed", action="store_true")
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--lookback", type=int, default=30)
    ap.add_argument("--vol-win", type=int, default=30)
    ap.add_argument("--port-vol", type=float, default=0.20, help="vol cible PORTEFEUILLE annualisée")
    ap.add_argument("--port-win", type=int, default=30, help="fenêtre vol réalisée du book")
    ap.add_argument("--scale-cap", type=float, default=2.0, help="cap du multiplicateur de book")
    args = ap.parse_args()

    symbols = ([s.strip().upper() for s in args.symbols.split(",")] if args.symbols
               else DEPLOYED if args.deployed else UNIVERSE)
    bpy = {"1d": 365}.get(args.interval, 365)
    target_vol_bar = 0.20 / np.sqrt(bpy)
    target_port_bar = args.port_vol / np.sqrt(bpy)
    adapter = HyperliquidReadAdapter()
    fee = Backtester(None).DEFAULT_FEE_PCT

    # Construit la matrice des positions par-coin (signées, vol-targeted) et des rendements.
    pos_cols, ret_cols, turn_cols = [], [], []
    for sym in symbols:
        df = fetch_df(adapter, sym, args.interval, args.limit)
        if len(df) < args.lookback + args.vol_win + 5:
            continue
        close = df["close"].astype(float).reset_index(drop=True)
        ret = close.pct_change().fillna(0.0)
        sgn = np.sign(close / close.shift(args.lookback) - 1.0).fillna(0.0)
        realized = ret.rolling(args.vol_win).std().shift(1)
        scalar = (target_vol_bar / realized).clip(upper=3.0).fillna(0.0)
        pos = (sgn.shift(1).fillna(0.0) * scalar)
        pos_cols.append(pos.reset_index(drop=True))
        ret_cols.append(ret.reset_index(drop=True))

    # Aligne à droite sur la longueur commune.
    minlen = min(len(p) for p in pos_cols)
    P = pd.concat([p.iloc[-minlen:].reset_index(drop=True) for p in pos_cols], axis=1)
    R = pd.concat([r.iloc[-minlen:].reset_index(drop=True) for r in ret_cols], axis=1)
    P.columns = range(P.shape[1]); R.columns = range(R.shape[1])
    N = P.shape[1]

    # Rendement par-coin (book equal-risk = moyenne 1/N).
    base_daily = (P.values * R.values).sum(axis=1) / N
    base_turn = np.abs(np.diff(P.values, axis=0, prepend=0.0)).sum(axis=1) / N
    base_net = base_daily - base_turn * fee

    # Vol réalisée du BOOK (rolling, décalée) → multiplicateur de book (vol-targeting portefeuille).
    book_ret = pd.Series(base_daily)
    book_vol = book_ret.rolling(args.port_win).std().shift(1)
    mult = (target_port_bar / book_vol).clip(upper=args.scale_cap).fillna(0.0).values
    port_daily = base_daily * mult
    # Frais : turnover du book scalé = |Δ(mult·pos)|. Approx : mult bouge lentement → turnover ≈
    # mult·turnover_pos + |Δmult|·|pos|. On calcule exactement sur la matrice scalée.
    Pm = P.values * mult[:, None]
    port_turn = np.abs(np.diff(Pm, axis=0, prepend=0.0)).sum(axis=1) / N
    port_net = base_daily * mult - port_turn * fee

    half = minlen // 2
    print(f"\nTSMOM VOL-TARGET PORTEFEUILLE — {args.interval}, lookback {args.lookback}, "
          f"{N} coins, vol cible book {args.port_vol:.0%}/an, frais {fee:.3%}/côté")
    print(f"{'='*74}")
    for window, sl in [("FULL", slice(None)), ("IS ", slice(0, half)), ("OOS", slice(half, None))]:
        bs, bc, bd = perf(base_net[sl], bpy)
        ps, pc, pd_ = perf(port_net[sl], bpy)
        avg_mult = float(np.nanmean(mult[sl][mult[sl] > 0]))
        print(f"  — {window} —")
        print(f"    par_coin (equal-risk)  : Sharpe {bs:5.2f}  CAGR {bc:6.0%}  maxDD {bd:4.0%}")
        print(f"    port_vol (book-target) : Sharpe {ps:5.2f}  CAGR {pc:6.0%}  maxDD {pd_:4.0%}  "
              f"(mult moy {avg_mult:.2f}×)")


if __name__ == "__main__":
    main()

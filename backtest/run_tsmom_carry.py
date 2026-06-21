#!/usr/bin/env python3
"""
TSMOM — CARRY DE FUNDING passif + tilt (recherche 2026-06-20, exploration créative).

Le TSMOM tient ses positions des semaines → il accumule passivement le funding des perps. Sur
HL, funding>0 ⇒ les LONGS paient les SHORTS. Carry par unité de notional et par période =
−direction × funding (short d=−1, f>0 → +f reçu ; long d=+1, f>0 → −f payé). Comme le TSMOM est
souvent SHORT en downtrend et que le funding crypto est généralement POSITIF, il pourrait encaisser
un carry passif NET POSITIF = tailwind gratuit déjà présent dans le P&L live (ZÉRO trade en plus).

Ce script :
  1. MESURE le carry passif annualisé que le TSMOM (equal-weight des directions) gagne/paie.
  2. Teste un TILT de taille vers les coins à carry favorable (size ×(1+λ·sign(carry))) : ajoute-t-il
     du rendement net SANS churn matériel ? (le tilt bouge lentement, recalculé au pas journalier).

Tout est net : on additionne rendement-prix vol-targeted (frais sur turnover) + carry de funding.

Usage : python3 backtest/run_tsmom_carry.py --interval 1d --lookback 30 --days 200
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


def daily_funding(adapter, coin, days):
    """Funding HL horaire → Series indexée par jour (somme du funding du jour, en fraction)."""
    rows = adapter.get_funding_history(coin, days=days)
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["time_ms", "rate"])
    df["day"] = pd.to_datetime(df["time_ms"], unit="ms").dt.floor("D")
    return df.groupby("day")["rate"].sum()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--deployed", action="store_true")
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--lookback", type=int, default=30)
    ap.add_argument("--vol-win", type=int, default=30)
    ap.add_argument("--days", type=int, default=200, help="profondeur funding (jours)")
    ap.add_argument("--lam", type=float, default=0.3, help="force du tilt carry")
    args = ap.parse_args()

    symbols = ([s.strip().upper() for s in args.symbols.split(",")] if args.symbols
               else DEPLOYED if args.deployed else UNIVERSE)
    bpy = {"1d": 365}.get(args.interval, 365)
    target_vol_bar = 0.20 / np.sqrt(bpy)
    adapter = HyperliquidReadAdapter()
    fee = Backtester(None).DEFAULT_FEE_PCT

    base_rows, tilt_rows, carry_only = [], [], []
    n_ok = 0
    for sym in symbols:
        df = fetch_df(adapter, sym, args.interval, args.limit)
        if len(df) < args.lookback + args.vol_win + 5:
            continue
        df = df.copy()
        # index temps des bougies (UTC jour) pour aligner le funding (colonne `ts` en ms).
        if "ts" not in df.columns:
            continue
        df["day"] = pd.to_datetime(df["ts"], unit="ms").dt.floor("D")
        fund = daily_funding(adapter, sym, args.days)
        if fund is None:
            continue
        close = df["close"].astype(float).reset_index(drop=True)
        ret = close.pct_change().fillna(0.0)
        sgn = np.sign(close / close.shift(args.lookback) - 1.0).fillna(0.0)
        realized = ret.rolling(args.vol_win).std().shift(1)
        scalar = (target_vol_bar / realized).clip(upper=3.0).fillna(0.0)
        pos_dir = sgn.shift(1).fillna(0.0)
        # Funding aligné sur les jours des bougies (fraction/jour), 0 hors couverture.
        f_day = df["day"].map(fund).astype(float).reset_index(drop=True).fillna(0.0)
        # Carry par unité de position et par jour = −direction × funding (signé).
        carry_unit = -pos_dir * f_day
        # Baseline : prix vol-targeted − frais + carry passif (sur la position tenue).
        pos = pos_dir * scalar
        turnover = pos.diff().abs().fillna(pos.abs())
        base = pos * ret - turnover * fee + (-pos * f_day)      # carry = −pos×funding
        # Tilt carry : multiplie la taille par (1+λ·sign(carry_unit)), borné ≥0 → re-vol-targete implicite.
        tilt_mult = (1.0 + args.lam * np.sign(carry_unit)).clip(lower=0.0)
        pos_t = pos * tilt_mult
        turn_t = pos_t.diff().abs().fillna(pos_t.abs())
        tilt = pos_t * ret - turn_t * fee + (-pos_t * f_day)
        base_rows.append(base.values)
        tilt_rows.append(tilt.values)
        carry_only.append((-pos * f_day).values)   # composante carry seule (vol-targeted)
        n_ok += 1

    if n_ok == 0:
        print("Aucun coin avec funding+OHLC exploitable.")
        return

    minlen = min(len(r) for r in base_rows)
    B = pd.concat([pd.Series(r[-minlen:]) for r in base_rows], axis=1).mean(axis=1).values
    T = pd.concat([pd.Series(r[-minlen:]) for r in tilt_rows], axis=1).mean(axis=1).values
    C = pd.concat([pd.Series(r[-minlen:]) for r in carry_only], axis=1).mean(axis=1).values

    print(f"\nTSMOM CARRY — {args.interval}, lookback {args.lookback}, {n_ok} coins, "
          f"funding {args.days}j, frais {fee:.3%}/côté\n")
    carry_ann = float(np.mean(C)) * bpy
    print(f"  Carry de funding PASSIF (vol-targeted, equal-risk) : {carry_ann:+.2%}/an "
          f"({'TAILWIND' if carry_ann > 0 else 'HEADWIND'})")
    bs, bc, bd = perf(B, bpy)
    ts, tc, td = perf(T, bpy)
    print(f"  Baseline (prix−frais+carry) : Sharpe {bs:5.2f}  CAGR {bc:6.0%}  maxDD {bd:4.0%}")
    print(f"  Tilt carry (λ={args.lam})       : Sharpe {ts:5.2f}  CAGR {tc:6.0%}  maxDD {td:4.0%}")
    print(f"  → tilt {'AJOUTE' if ts > bs else 'N AJOUTE PAS'} du Sharpe ({ts:.2f} vs {bs:.2f})")


if __name__ == "__main__":
    main()

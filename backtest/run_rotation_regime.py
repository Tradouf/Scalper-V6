#!/usr/bin/env python3
"""
ROTATION — l'edge contrarian dépend-il du RÉGIME ? (2026-06-27, #1)

Le contrarian exploite l'anti-persistance (mean-reversion entre stratégies). Hypothèse : il
serait PLUS fort en marché choppy/range (où les perfs de stratégies retournent) et plus faible en
TREND établi (où la stratégie gagnante persiste). Si l'edge se concentre dans un régime CAUSAL
(connu d'avance), un sizing conditionnel pourrait l'améliorer. Sinon, le contrarian est robuste au
régime (bon à savoir = pas besoin d'ajouter de logique régime).

On bucket les rendements quotidiens du contrarian vol-targeted (R=1, deep) par :
  - TRENDINESS : Kaufman efficiency ratio (ER) sur 30j, décalé (causal). Haut = trend, bas = chop.
  - VOLATILITÉ : vol réalisée 30j, décalée. Haut / bas (médiane pooled).
Et on compare le Sharpe conditionnel (contrarian vs mono_tsmom pour contraste).

Usage : python3 backtest/run_rotation_regime.py --symbols BTC,ETH,SOL,BNB,XRP,DOGE,LINK,AVAX
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
from strategies.strategy_pool import build_pool_deep, strat_returns, s_tsmom
from backtest.run_rotation import weighted_ensemble, perf
from backtest.run_rotation_voltarget import voltarget_returns
from execution.hyperliquid_adapter import HyperliquidReadAdapter


def efficiency_ratio(close: pd.Series, n: int) -> pd.Series:
    net = (close - close.shift(n)).abs()
    vol = close.diff().abs().rolling(n).sum()
    return (net / vol.replace(0, np.nan)).clip(0, 1)


def sharpe(x, bpy):
    x = np.asarray(x, float)
    return x.mean() / x.std() * np.sqrt(bpy) if len(x) > 5 and x.std() > 0 else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTC,ETH,SOL,BNB,XRP,DOGE,LINK,AVAX")
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--L", type=int, default=90)
    ap.add_argument("--vol-win", type=int, default=30)
    ap.add_argument("--target-vol", type=float, default=0.40)
    ap.add_argument("--reg-win", type=int, default=30)
    args = ap.parse_args()

    syms = [s.strip().upper() for s in args.symbols.split(",")]
    bpy = 365
    tvb = args.target_vol / np.sqrt(bpy)
    adapter = HyperliquidReadAdapter()

    rows = []  # (contra_ret, mono_ret, er, vol) par jour, poolé
    per_coin = []  # (contra_series, volpct_series) pour le portefeuille overlay
    for sym in syms:
        df = fetch_df(adapter, sym, args.interval, args.limit)
        if len(df) < 400:
            continue
        df = df.reset_index(drop=True)
        close = df["close"].astype(float)
        pool = build_pool_deep(df)
        rets = {nm: strat_returns(df, p) for nm, p in pool.items()}
        cpos = weighted_ensemble(df, pool, rets, args.L, 1, "invperf", 1.0)
        cr = voltarget_returns(df, cpos, args.vol_win, tvb, 0.00045)
        mr = voltarget_returns(df, s_tsmom(df, 30), args.vol_win, tvb, 0.00045)
        er = efficiency_ratio(close, args.reg_win).shift(1).values     # causal
        rv_s = close.pct_change().rolling(args.reg_win).std().shift(1)
        rv = rv_s.values
        # percentile causal de la vol (rang dans la fenêtre glissante 180j) pour l'overlay régime.
        volpct = rv_s.rolling(180, min_periods=60).apply(
            lambda x: (x[-1] >= x).mean(), raw=True).values
        per_coin.append((pd.Series(cr), pd.Series(volpct)))
        for i in range(300, len(df)):
            if np.isfinite(er[i]) and np.isfinite(rv[i]):
                rows.append((cr[i], mr[i], er[i], rv[i]))
    R = pd.DataFrame(rows, columns=["contra", "mono", "er", "vol"])
    print(f"\nROTATION RÉGIME — {len(syms)} coins, {len(R)} jours-coin poolés, vol cible {args.target_vol:.0%}\n")

    # Tertiles d'ER (trendiness) et de vol.
    for axis, lab in [("er", "TRENDINESS (Kaufman ER ; bas=chop, haut=trend)"),
                      ("vol", "VOLATILITÉ réalisée")]:
        q1, q2 = R[axis].quantile([0.33, 0.66])
        buckets = [("bas  ", R[R[axis] <= q1]), ("moyen", R[(R[axis] > q1) & (R[axis] <= q2)]),
                   ("haut ", R[R[axis] > q2])]
        print(f"  {lab} :")
        print(f"    {'bucket':<7} {'n':>6} {'contra Sharpe':>14} {'mono Sharpe':>12}")
        for name, b in buckets:
            print(f"    {name:<7} {len(b):>6} {sharpe(b['contra'], bpy):>14.2f} {sharpe(b['mono'], bpy):>12.2f}")
        print()

    # ── OVERLAY régime : couper l'expo quand la vol est dans son top (high-vol = edge ~0) ──
    minlen = min(len(c) for c, _ in per_coin)
    C = pd.concat([c.iloc[-minlen:].reset_index(drop=True) for c, _ in per_coin], axis=1)
    V = pd.concat([v.iloc[-minlen:].reset_index(drop=True) for _, v in per_coin], axis=1)
    half = minlen // 2
    print(f"  {'='*60}\n  OVERLAY high-vol (×damp si vol-percentile > seuil) — portefeuille equal-weight :")
    print(f"    {'config':<28} {'FULL Sharpe':>12} {'OOS Sharpe':>12}")
    base = C.mean(axis=1)
    print(f"    {'baseline (toujours ON)':<28} {sharpe(base.iloc[300:], bpy):>12.2f} {sharpe(base.iloc[half:], bpy):>12.2f}")
    for thr, damp in [(0.66, 0.0), (0.66, 0.4), (0.80, 0.0), (0.80, 0.4)]:
        w = (V > thr).astype(float) * (damp - 1.0) + 1.0   # 1 si vol basse, damp si haute
        filt = (C * w.values).mean(axis=1)
        print(f"    high-vol>{thr:.2f} → ×{damp:<14.1f} {sharpe(filt.iloc[300:], bpy):>12.2f} {sharpe(filt.iloc[half:], bpy):>12.2f}")


if __name__ == "__main__":
    main()

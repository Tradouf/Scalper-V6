#!/usr/bin/env python3
"""
ROTATION de stratégies — l'alternance est-elle exploitable ? (recherche 2026-06-21)

Hypothèse francois : une stratégie marche un temps puis échoue, une autre prend le relais — c'est
l'ALTERNANCE qu'il faut encadrer. Avant de construire un méta-allocateur, on tranche la question
scientifique préalable : la performance des stratégies est-elle PERSISTANTE ? (la gagnante d'hier
gagne-t-elle demain ?) Sinon la rotation achète le gagnant juste avant qu'il retourne = mirage.

Étapes :
  1. PERSISTANCE : pour des fenêtres consécutives, corrélation de rang (Spearman) des Sharpe des
     stratégies entre fenêtre k et k+1. >0 = persistance (rotation viable) ; ~0/<0 = pas.
  2. ROTATION causale : tous les R barres, classer les stratégies par Sharpe trailing (L barres),
     prendre top-K, position méta = moyenne de leurs positions sur les R barres suivantes. Net de
     frais sur le turnover de la position COMBINÉE (capture le coût de bascule).
  3. COMPARER à : equal-weight de TOUTES les stratégies (diversifier sans tourner), K=1, et la
     meilleure statique (oracle in-sample = borne haute). Si la rotation ne bat pas l'equal-weight
     OOS, l'alternance n'est pas exploitable.

Usage : python3 backtest/run_rotation.py --symbol BTC --interval 1d --L 90 --R 20 --K 3
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
from strategies.strategy_pool import build_pool, strat_returns, FEE
from execution.hyperliquid_adapter import HyperliquidReadAdapter


def perf(daily, bpy):
    daily = np.asarray(daily, float)
    if len(daily) == 0 or daily.std() == 0:
        return 0.0, 0.0, 0.0
    sharpe = daily.mean() / daily.std() * np.sqrt(bpy)
    eq = np.cumprod(1 + daily)
    cagr = eq[-1] ** (bpy / len(daily)) - 1
    mdd = np.max((np.maximum.accumulate(eq) - eq) / np.maximum.accumulate(eq))
    return sharpe, cagr, mdd


def rolling_sharpe(r, win):
    s = pd.Series(r)
    return (s.rolling(win).mean() / s.rolling(win).std()).values


def persistence(rets: dict, win: int, bpy: int) -> tuple[float, int]:
    """Spearman moyen des Sharpe par stratégie entre fenêtres NON chevauchantes consécutives."""
    names = list(rets)
    n = len(next(iter(rets.values())))
    edges = list(range(0, n - win, win))
    sharpes = []  # par fenêtre : vecteur de Sharpe par stratégie
    for e in edges:
        row = [pd.Series(rets[nm][e:e + win]) for nm in names]
        sharpes.append([(x.mean() / x.std() * np.sqrt(bpy)) if x.std() > 0 else 0.0 for x in row])
    S = pd.DataFrame(sharpes, columns=names)
    cors = [S.iloc[i].corr(S.iloc[i + 1], method="spearman") for i in range(len(S) - 1)]
    cors = [c for c in cors if pd.notna(c)]
    return (float(np.mean(cors)) if cors else 0.0), len(cors)


def rotation_position(df, pool, rets, L, R, K, mode="top"):
    """Position méta causale : tous R barres, score = Sharpe trailing L, choisit top-K (ou bottom-K
    si mode='contrarian'), position = moyenne des positions de ces K sur les R barres suivantes."""
    names = list(pool)
    P = pd.concat([pool[nm].reset_index(drop=True) for nm in names], axis=1)
    P.columns = names
    n = len(P)
    meta = np.zeros(n)
    for t in range(L, n, R):
        window = {nm: rets[nm][t - L:t] for nm in names}
        sc = {nm: (np.mean(w) / np.std(w)) if np.std(w) > 0 else -np.inf for nm, w in window.items()}
        ranked = sorted(names, key=lambda nm: sc[nm], reverse=(mode == "top"))
        chosen = ranked[:K]
        seg = slice(t, min(t + R, n))
        meta[seg] = P[chosen].iloc[seg].mean(axis=1).values
    return pd.Series(meta)


def weighted_ensemble(df, pool, rets, L, R, mode="invperf", temp=1.0):
    """Ensemble DIVERSIFIÉ à poids variables (garde TOUTES les stratégies, ne concentre pas).
    Tous R barres, poids ∝ softmax(±score/temp) : 'invperf' = tilt contrarian (vers les perdants
    récents, cohérent avec l'anti-persistance) ; 'perf' = tilt momentum ; 'rp' = inverse-vol."""
    names = list(pool)
    P = pd.concat([pool[nm].reset_index(drop=True) for nm in names], axis=1)
    P.columns = names
    n = len(P)
    meta = np.zeros(n)
    for t in range(L, n, R):
        if mode == "rp":
            vol = np.array([np.std(rets[nm][t - L:t]) or 1e-9 for nm in names])
            w = (1.0 / vol); w /= w.sum()
        else:
            sc = np.array([(np.mean(rets[nm][t - L:t]) / (np.std(rets[nm][t - L:t]) or 1e-9)) for nm in names])
            sign = -1.0 if mode == "invperf" else 1.0
            z = sign * (sc - sc.mean()) / (sc.std() or 1e-9) / temp
            w = np.exp(z - z.max()); w /= w.sum()
        seg = slice(t, min(t + R, n))
        meta[seg] = (P.iloc[seg].values * w).sum(axis=1)
    return pd.Series(meta)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTC")
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--L", type=int, default=90, help="fenêtre de scoring trailing")
    ap.add_argument("--R", type=int, default=20, help="période de rebalancement")
    ap.add_argument("--K", type=int, default=3, help="nb de stratégies sélectionnées")
    args = ap.parse_args()

    bpy = {"1d": 365, "4h": 6 * 365, "1h": 24 * 365}.get(args.interval, 365)
    adapter = HyperliquidReadAdapter()
    df = fetch_df(adapter, args.symbol, args.interval, args.limit)
    if len(df) < 300:
        print("pas assez de données"); return
    df = df.reset_index(drop=True)

    pool = build_pool(df)
    rets = {nm: strat_returns(df, pos) for nm, pos in pool.items()}

    print(f"\nROTATION — {args.symbol} {args.interval}, {len(df)} barres ≈ {len(df)/bpy:.1f} ans, "
          f"{len(pool)} stratégies, frais {FEE:.3%}/côté")

    # Perf statique de chaque stratégie (tri par Sharpe) — pour voir la dispersion.
    print("\n  Stratégies (Sharpe FULL, tri décroissant) :")
    stat = sorted(((nm, perf(r, bpy)[0]) for nm, r in rets.items()), key=lambda x: -x[1])
    for nm, sh in stat[:6]:
        print(f"    {nm:<20} Sharpe {sh:5.2f}")
    print(f"    … {len(stat)-12} autres …")
    for nm, sh in stat[-6:]:
        print(f"    {nm:<20} Sharpe {sh:5.2f}")

    # 1) PERSISTANCE à plusieurs horizons.
    print("\n  PERSISTANCE (Spearman moyen des Sharpe entre fenêtres consécutives) :")
    for win in [30, 60, 90, 180]:
        rho, k = persistence(rets, win, bpy)
        verdict = "persistant" if rho > 0.1 else ("anti-persistant" if rho < -0.1 else "≈ aléatoire")
        print(f"    fenêtre {win:>3}j : ρ={rho:+.3f}  ({k} paires) → {verdict}")

    # 2) ROTATION causale + benchmarks. Split OOS (2e moitié).
    half = len(df) // 2
    benasses = {}
    benasses["rotation_top"] = strat_returns(df, rotation_position(df, pool, rets, args.L, args.R, args.K, "top"))
    benasses["rotation_K1"] = strat_returns(df, rotation_position(df, pool, rets, args.L, args.R, 1, "top"))
    benasses["rotation_contra"] = strat_returns(df, rotation_position(df, pool, rets, args.L, args.R, args.K, "contrarian"))
    # Equal-weight de toutes les stratégies (diversifier sans tourner).
    Pall = pd.concat([pool[nm].reset_index(drop=True) for nm in pool], axis=1).mean(axis=1)
    benasses["equal_all"] = strat_returns(df, Pall)
    # Ensembles diversifiés à poids variables (tous gardés, tilt doux).
    benasses["ens_contra"] = strat_returns(df, weighted_ensemble(df, pool, rets, args.L, args.R, "invperf", 1.0))
    benasses["ens_momtm"] = strat_returns(df, weighted_ensemble(df, pool, rets, args.L, args.R, "perf", 1.0))
    benasses["ens_riskpar"] = strat_returns(df, weighted_ensemble(df, pool, rets, args.L, args.R, "rp"))
    # Buy & hold.
    benasses["buy_hold"] = strat_returns(df, pd.Series(1.0, index=range(len(df))))

    print(f"\n  {'='*66}\n  ROTATION vs BENCHMARKS (L={args.L} R={args.R} K={args.K}) :")
    print(f"  {'stratégie':<18} {'FULL Sharpe/CAGR/DD':>26}   {'OOS Sharpe/CAGR/DD':>26}")
    for nm, r in benasses.items():
        fs, fc, fdd = perf(r, bpy)
        os_, oc, odd = perf(r[half:], bpy)
        print(f"  {nm:<18} {fs:6.2f} {fc:6.0%} {fdd:5.0%}        {os_:6.2f} {oc:6.0%} {odd:5.0%}")


if __name__ == "__main__":
    main()

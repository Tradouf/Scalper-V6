"""
Pouls du marché BTC 1m — mesure la durée de validité du classement des
stratégies.

Question à laquelle l'outil répond : quand on identifie quelles stratégies
marchent sur les W dernières minutes, combien de temps cette information
reste-t-elle exploitable ?

Méthode (coût zéro : on mesure le signal, pas les frais) :
1. les 148 stratégies de la grille sont simulées en continu sur tout
   l'historique 1 m disponible (~3,5 j — plafond de l'API Hyperliquid) →
   une courbe d'equity au pas 1 min par stratégie ;
2. IC(W, H) : à chaque instant t, corrélation de rang (Spearman) entre la
   performance passée (fenêtre W) et la performance future (horizon H),
   fenêtres forward non chevauchantes pour des t-stats honnêtes ;
3. stabilité du classement : spearman(classement à t, classement à t+Δ) —
   au-delà de Δ ≥ W les fenêtres ne partagent plus de données, toute
   corrélation résiduelle est de la vraie persistance ;
4. champion : survie du top-1 dans le top-décile et PnL forward.

    python -m minutelab.pulse [--hours 84]

Résultat de référence (2026-07-06, 75 h) : anti-persistance à H=5-10 min
(IC −0,04, t=−3,1 : ce qui vient de gagner perd juste après), poche positive
à H=20-30 min pour W=20-30 (IC +0,04/+0,06, t=2,0-2,5, spread top−bottom
décile ≈ +0,013 %/30 min brut), classement sans aucune mémoire au-delà du
chevauchement des fenêtres, champion top-1 : 5 min de survie médiane.
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor
from typing import List

from minutelab import config
from minutelab.backtester import run_lab_backtest
from minutelab.data1m import fetch_recent_1m
from minutelab.strategies import build_grid

WARMUP = 360
WINDOWS = [10, 20, 30, 60, 120]
HORIZONS = [5, 10, 15, 20, 30, 45, 60, 90, 120, 180]
T_STEP = 5

_CANDLES: List[dict] = []
_CLOSES = None


def _stream_for(idx: int):
    """Incréments de PnL par bougie (% prix, coût zéro) de la stratégie idx."""
    import numpy as np
    strat = build_grid()[idx]
    r = run_lab_backtest(_CANDLES, strat, fee_pct=0.0, slippage_pct=0.0,
                         start_index=WARMUP, recent_index=0,
                         hard_sl_pct=config.HARD_SL_PCT,
                         max_hold_bars=config.MAX_HOLD_MIN,
                         exit_min_gain=None)
    inc = np.zeros(len(_CANDLES))
    for t in r.trades:
        d, e = t["dir"], t["entry"]
        b0, b1, xp = t["entry_bar"], t["exit_bar"], t["exit"]
        if b0 == b1:
            inc[b0] += d * (xp - e) / e
            continue
        inc[b0] += d * (_CLOSES[b0] - e) / e
        for b in range(b0 + 1, b1):
            inc[b] += d * (_CLOSES[b] - _CLOSES[b - 1]) / e
        inc[b1] += d * (xp - _CLOSES[b1 - 1]) / e
    return idx, inc


def _rank_avg(x):
    import numpy as np
    order = np.argsort(x, kind="mergesort")
    ranks = np.empty(len(x))
    sx = x[order]
    i = 0
    while i < len(x):
        j = i
        while j + 1 < len(x) and sx[j + 1] == sx[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0
        i = j + 1
    return ranks


def _spearman(a, b):
    import numpy as np
    ra, rb = _rank_avg(a), _rank_avg(b)
    ra -= ra.mean()
    rb -= rb.mean()
    den = np.sqrt((ra ** 2).sum() * (rb ** 2).sum())
    return float((ra * rb).sum() / den) if den > 0 else float("nan")


def main() -> int:
    global _CANDLES, _CLOSES
    import numpy as np

    parser = argparse.ArgumentParser(
        description="Durée de validité du pouls du marché (BTC 1m)")
    parser.add_argument("--hours", type=float, default=84.0,
                        help="historique demandé (plafond API ~3,5 j)")
    args = parser.parse_args()

    _CANDLES = fetch_recent_1m(config.SYMBOL, args.hours)
    _CLOSES = np.array([c["close"] for c in _CANDLES])
    n = len(_CANDLES)
    grid = build_grid()
    need = WARMUP + max(WINDOWS) + max(HORIZONS) + 60
    if n < need:
        print(f"Historique insuffisant : {n} bougies, besoin {need}.")
        return 1
    print(f"{n} bougies ({n / 60:.0f} h), {len(grid)} stratégies — "
          f"simulation continue coût zéro...")

    M = np.zeros((len(grid), n))
    with ProcessPoolExecutor(max_workers=6) as ex:
        for idx, inc in ex.map(_stream_for, range(len(grid))):
            M[idx] = inc
    P = np.cumsum(M, axis=1)

    def win_ret(t0, t1):
        return P[:, t1] - P[:, t0]

    t_min = WARMUP + max(WINDOWS)
    t_max = n - max(HORIZONS) - 1

    print("\n=== IC de Spearman passé(W) → futur(H) — stratégies actives, "
          "fenêtres forward non chevauchantes ===")
    print(f"{'W\\H':>5} " + " ".join(f"{h:>13}" for h in HORIZONS))
    for W in WINDOWS:
        cells = []
        for H in HORIZONS:
            step = max(T_STEP, H)
            ics = []
            for t in range(t_min, t_max, step):
                past = win_ret(t - W, t)
                fwd = win_ret(t, t + H)
                act = past != 0.0
                if act.sum() < 15:
                    continue
                ics.append(_spearman(past[act], fwd[act]))
            ics = np.array(ics)
            m = float(np.nanmean(ics))
            ts = m / (np.nanstd(ics, ddof=1) / np.sqrt(len(ics)))
            cells.append(f"{m:+.3f}(t{ts:+.1f})")
        print(f"{W:>5} " + " ".join(f"{c:>13}" for c in cells))

    print("\n=== Stabilité du classement : spearman(rang t, rang t+Δ) — "
          "au-delà de Δ ≥ W, toute corrélation est de la vraie mémoire ===")
    print(f"{'W\\Δ':>5} " + " ".join(f"{d:>7}" for d in HORIZONS))
    for W in WINDOWS:
        row = []
        for D in HORIZONS:
            vals = []
            for t in range(t_min, t_max - D, max(T_STEP, D)):
                a = win_ret(t - W, t)
                b = win_ret(t + D - W, t + D)
                act = (a != 0) | (b != 0)
                if act.sum() < 15:
                    continue
                vals.append(_spearman(a[act], b[act]))
            row.append(float(np.nanmean(vals)))
        print(f"{W:>5} " + " ".join(f"{v:>+7.3f}" for v in row))

    W = 20
    surv, champ_fwd = [], {H: [] for H in HORIZONS}
    for t in range(t_min, t_max, T_STEP):
        past = win_ret(t - W, t)
        if (past > 0).sum() < 5:
            continue
        c = int(np.argmax(past))
        for H in HORIZONS:
            champ_fwd[H].append(win_ret(t, t + H)[c])
        life = 0
        for D in range(T_STEP, 181, T_STEP):
            if t + D >= t_max:
                break
            p2 = win_ret(t + D - W, t + D)
            if p2[c] >= np.quantile(p2, 0.9):
                life = D
            else:
                break
        surv.append(life)
    surv = np.array(surv)
    print(f"\n=== Champion top-1 (W={W}) ===")
    print(f"survie dans le top-décile : méd {np.median(surv):.0f} min, "
          f"moy {surv.mean():.0f} min, p75 {np.percentile(surv, 75):.0f} min "
          f"(n={len(surv)})")
    print("PnL forward moyen du champion (% brut, coût zéro) :")
    for H in HORIZONS:
        print(f"  H={H:>3} min : {np.mean(champ_fwd[H]) * 100:+.4f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Diagnostic de l'écart appris↔oracle du bandit d'exécution (#2, 2026-06-18).

Le shadow apprend +0,46 bps/ordre d'économie alors que l'oracle a posteriori
montre ~3 bps sur la table. Question : OÙ est le gain inexploité ? (par coin, par
bras, taux de remplissage passif). Réponse → ça dit s'il faut un modèle PAR COIN,
de meilleures features, ou si le passif ne remplit tout simplement pas.

Replay sur points aléatoires (full-information, comme le self-test) avec suivi
par coin et par bras. Lecture seule sur orderflow_hf.db. Ne touche à rien.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.exec_bandit_shadow import ARMS, hf_conn, l2_at, replay_arms, TIMEOUT_S


def main(n_per_coin: int = 500) -> None:
    rng = np.random.default_rng(7)
    conn = hf_conn()
    lo, hi = conn.execute("SELECT MIN(ts_ms), MAX(ts_ms) FROM l2_1s").fetchone()
    coins = [r[0] for r in conn.execute("SELECT DISTINCT coin FROM l2_1s")]

    # accumulateurs
    per_coin = defaultdict(lambda: {"n": 0, "taker": 0.0, "oracle": 0.0})
    arm_cost = defaultdict(lambda: [0.0, 0])   # bras -> [somme coût, n]
    arm_fill = defaultdict(lambda: [0, 0])      # bras passif -> [rempli, total]
    # taux de remplissage : on ré-inspecte le fill en rejouant la règle.

    print(f"Replay {n_per_coin} points/coin sur {len(coins)} coins "
          f"(fenêtre {(hi-lo)/86400000:.1f}j)...\n")

    for coin in coins:
        got = 0
        tries = 0
        while got < n_per_coin and tries < n_per_coin * 6:
            tries += 1
            ts = int(rng.integers(lo + 70_000, hi - (TIMEOUT_S + 2) * 1000))
            side = "B" if rng.random() < 0.5 else "A"
            costs = replay_arms(conn, coin, side, ts, actual_px=None)
            if costs is None:
                continue
            got += 1
            per_coin[coin]["n"] += 1
            per_coin[coin]["taker"] += costs[0]
            per_coin[coin]["oracle"] += min(costs.values())
            for k, c in costs.items():
                arm_cost[k][0] += c
                arm_cost[k][1] += 1
            # taux de remplissage passif : on re-déduit via la règle de fill du replay
            sgn = 1.0 if side == "B" else -1.0
            snap0 = l2_at(conn, coin, ts)
            if snap0 and snap0["mid"]:
                mid0 = snap0["mid"]
                trades = conn.execute(
                    "SELECT px FROM trades WHERE coin=? AND ts_ms>? AND ts_ms<=?",
                    (coin, ts, ts + TIMEOUT_S * 1000)).fetchall()
                for k, off in {1: 0.0, 2: 1.0, 3: 3.0}.items():
                    lim = mid0 * (1 - sgn * off / 1e4)
                    filled = any((sgn > 0 and px[0] < lim) or (sgn < 0 and px[0] > lim)
                                 for px in trades)
                    arm_fill[k][1] += 1
                    arm_fill[k][0] += int(filled)

    conn.close()

    print(f"{'coin':>6} {'n':>5} {'taker bps':>10} {'oracle bps':>11} {'gain dispo':>11}")
    print("-" * 48)
    tot_gain = []
    for coin in sorted(per_coin, key=lambda c: (per_coin[c]['taker']-per_coin[c]['oracle'])/max(per_coin[c]['n'],1), reverse=True):
        d = per_coin[coin]
        if d["n"] == 0:
            continue
        tk, orc = d["taker"]/d["n"], d["oracle"]/d["n"]
        gain = tk - orc
        tot_gain.append(gain)
        print(f"{coin:>6} {d['n']:>5} {tk:>10.2f} {orc:>11.2f} {gain:>11.2f}")
    print("-" * 48)
    print(f"{'MOYEN':>6} {'':>5} {'':>10} {'':>11} {np.mean(tot_gain):>11.2f}\n")

    print("Par BRAS — coût moyen (bps) :")
    for k in range(len(ARMS)):
        s, n = arm_cost[k]
        if n:
            print(f"  {ARMS[k]:>20} : {s/n:+.2f} bps  ({n} obs)")
    print("\nTaux de REMPLISSAGE passif (probabilité qu'un trade traverse notre prix en 30s) :")
    for k, name in ((1, "limit_mid"), (2, "passif_1bps"), (3, "passif_3bps")):
        f, t = arm_fill[k]
        if t:
            print(f"  {name:>20} : {100*f/t:5.1f}% rempli  (sinon fallback taker)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Confirme la politique d'exécution FIXE limit_mid sur les VRAIS fills (#2, 2026-06-18).

Le diagnostic random a montré que limit_mid (poster au mid, timeout 30s → fallback
taker) est le meilleur bras EN MOYENNE, plus simple et plus robuste que le bandit
contextuel. Reste à confirmer sur la VRAIE distribution des fills du compte, et
séparé ENTRÉES (où l'on posterait passivement) vs SORTIES (toujours taker en live).

Replay full-information de chaque fill réel récent tombant dans la fenêtre HF.
Compare : toujours-taker vs toujours-limit_mid vs passifs vs oracle. Lecture seule.
"""
from __future__ import annotations

import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.exec_bandit_shadow import (
    ARMS, TIMEOUT_S, account_address, fetch_fills, hf_conn, replay_arms,
)


def main() -> None:
    addr = account_address()
    if not addr:
        print("HL_ACCOUNT_ADDRESS introuvable"); return
    conn = hf_conn()
    hf_min, hf_max = conn.execute("SELECT MIN(ts_ms), MAX(ts_ms) FROM l2_1s").fetchone()
    fills = fetch_fills(addr)
    now_ms = time.time() * 1000

    # accumulateurs par classe (entrée / sortie / global)
    acc = defaultdict(lambda: {"n": 0, "arm": defaultdict(float), "oracle": 0.0})

    for f in fills:
        ts_ms = int(f.get("time", 0))
        if ts_ms < hf_min + 65_000 or ts_ms > hf_max - (TIMEOUT_S + 2) * 1000:
            continue  # hors fenêtre HF replayable
        if ts_ms > now_ms - (TIMEOUT_S + 5) * 1000:
            continue
        coin, side = f.get("coin", ""), f.get("side", "")
        px, sz = float(f.get("px", 0) or 0), float(f.get("sz", 0) or 0)
        if not coin or side not in ("B", "A") or px <= 0:
            continue
        costs = replay_arms(conn, coin, side, ts_ms, actual_px=px)
        if costs is None:
            continue
        dir_ = str(f.get("dir", ""))
        klass = "ENTRÉE" if dir_.startswith("Open") else ("SORTIE" if dir_.startswith("Close") else "AUTRE")
        for grp in (klass, "GLOBAL"):
            acc[grp]["n"] += 1
            for k, c in costs.items():
                acc[grp]["arm"][k] += c
            acc[grp]["oracle"] += min(costs.values())
    conn.close()

    if not acc:
        print("Aucun fill réel replayable dans la fenêtre HF (12j). "
              "Le shadow accumule au fil de l'eau ; relancer plus tard si vide.")
        return

    print(f"\nVrais fills replayés (fenêtre HF {(hf_max-hf_min)/86400000:.1f}j) :\n")
    for grp in ("ENTRÉE", "SORTIE", "GLOBAL"):
        if grp not in acc or acc[grp]["n"] == 0:
            continue
        d = acc[grp]; n = d["n"]
        taker = d["arm"][0] / n
        mid = d["arm"][1] / n
        p1 = d["arm"][2] / n
        p3 = d["arm"][3] / n
        orc = d["oracle"] / n
        print(f"── {grp}  ({n} fills) ──")
        print(f"   toujours-taker     : {taker:+.2f} bps/ordre")
        print(f"   toujours-limit_mid : {mid:+.2f} bps/ordre   → économie {taker-mid:+.2f} bps")
        print(f"   toujours-passif_1  : {p1:+.2f} bps/ordre   → économie {taker-p1:+.2f} bps")
        print(f"   toujours-passif_3  : {p3:+.2f} bps/ordre   → économie {taker-p3:+.2f} bps")
        print(f"   oracle (futur connu): {orc:+.2f} bps/ordre")
        verdict = "✅ ≥1 bps" if (taker - mid) >= 1.0 else "❌ < 1 bps"
        print(f"   → limit_mid sur {grp.lower()} : {verdict}\n")


if __name__ == "__main__":
    main()

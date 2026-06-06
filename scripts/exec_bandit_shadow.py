#!/usr/bin/env python3
"""
Bandit contextuel d'exécution — SHADOW MODE (2026-06-06).

Ne modifie RIEN au trading live. Pour chaque fill réel du compte :
  1. contexte au moment de l'ordre (orderflow_hf.db : spread, vol 1m,
     imbalance, intensité des trades)
  2. replay contrefactuel des 4 bras sur les 30 s suivantes (l2_1s + trades) :
       A0 taker_now      — comportement actuel du bot (prix du fill réel)
       A1 limit@mid      — limit au mid, timeout 30 s → fallback taker
       A2 limit_passif_1 — mid ∓ 1 bps (côté passif)
       A3 limit_passif_3 — mid ∓ 3 bps
     Règle de fill CONSERVATRICE : rempli seulement si un trade public
     traverse STRICTEMENT notre prix (la position dans la file est inconnue).
  3. coût (bps) = implementation shortfall vs mid(t0) + frais (taker 4.5,
     maker 1.5) ; fallback inclus si non rempli.
  4. apprentissage : régression ridge par bras (le replay donne le coût de
     TOUS les bras → full information, pas de dilemme exploration).

État persistant : memory/exec_bandit_state.json (matrices ridge + métriques).
Rapport : politique apprise vs politique actuelle (toujours-taker), en bps.

Usage :
  python3 scripts/exec_bandit_shadow.py --selftest    # valide le replay sur
                                                      # des points aléatoires HF
  python3 scripts/exec_bandit_shadow.py --once        # traite les fills récents
  python3 scripts/exec_bandit_shadow.py               # daemon (boucle 300 s)
  python3 scripts/exec_bandit_shadow.py --report      # affiche l'état appris
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

import numpy as np
import requests

REPO = Path(__file__).resolve().parent.parent
HF_DB = REPO / "data" / "orderflow_hf.db"
STATE_PATH = REPO / "memory" / "exec_bandit_state.json"
HL_API = "https://api.hyperliquid.xyz/info"

ARMS = ["taker_now", "limit_mid", "limit_passif_1bps", "limit_passif_3bps"]
ARM_OFFSETS_BPS = {1: 0.0, 2: 1.0, 3: 3.0}   # offset passif par bras limit
TIMEOUT_S = 30
FEE_TAKER_BPS = 4.5
FEE_MAKER_BPS = 1.5
RIDGE_LAMBDA = 1.0
N_CTX = 6   # [1, spread, vol1m, imb5_signé, trade_rate, log_notional]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger("sdm.exec_bandit")


# ── Accès données HF ──────────────────────────────────────────────────────────
def hf_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{HF_DB}?mode=ro", uri=True)
    return conn


def l2_at(conn, coin: str, ts_ms: int, tol_ms: int = 3000) -> dict | None:
    row = conn.execute(
        "SELECT ts_ms, mid_px, spread_bps, imb5 FROM l2_1s "
        "WHERE coin=? AND ts_ms BETWEEN ? AND ? ORDER BY ABS(ts_ms-?) LIMIT 1",
        (coin, ts_ms - tol_ms, ts_ms + tol_ms, ts_ms)).fetchone()
    if not row:
        return None
    return {"ts_ms": row[0], "mid": row[1], "spread_bps": row[2], "imb5": row[3]}


def context_at(conn, coin: str, ts_ms: int, side: str, notional: float) -> np.ndarray | None:
    snap = l2_at(conn, coin, ts_ms)
    if not snap or not snap["mid"]:
        return None
    mids = [r[0] for r in conn.execute(
        "SELECT mid_px FROM l2_1s WHERE coin=? AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms",
        (coin, ts_ms - 60_000, ts_ms))]
    if len(mids) < 10:
        return None
    rets = np.diff(np.log(np.array(mids, dtype=float))) * 1e4
    vol_1m = float(np.std(rets)) if len(rets) > 2 else 0.0
    n_tr = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE coin=? AND ts_ms BETWEEN ? AND ?",
        (coin, ts_ms - 60_000, ts_ms)).fetchone()[0]
    sgn = 1.0 if side == "B" else -1.0
    return np.array([
        1.0, snap["spread_bps"], vol_1m, sgn * (snap["imb5"] or 0.0),
        n_tr / 60.0, np.log10(max(notional, 1.0)),
    ], dtype=float)


# ── Replay contrefactuel ──────────────────────────────────────────────────────
def replay_arms(conn, coin: str, side: str, ts_ms: int,
                actual_px: float | None) -> dict[int, float] | None:
    """→ {arm_idx: coût bps} (coût positif = on paye). None si données absentes."""
    snap0 = l2_at(conn, coin, ts_ms)
    if not snap0 or not snap0["mid"]:
        return None
    mid0 = snap0["mid"]
    sgn = 1.0 if side == "B" else -1.0   # buy : payer plus haut = coût

    def shortfall(px: float) -> float:
        return sgn * (px - mid0) / mid0 * 1e4

    costs: dict[int, float] = {}

    # A0 — taker immédiat : prix réel si dispo, sinon traversée du spread
    px0 = actual_px if actual_px else mid0 * (1 + sgn * snap0["spread_bps"] / 2e4)
    costs[0] = shortfall(px0) + FEE_TAKER_BPS

    trades = conn.execute(
        "SELECT ts_ms, px FROM trades WHERE coin=? AND ts_ms > ? AND ts_ms <= ? "
        "ORDER BY ts_ms", (coin, ts_ms, ts_ms + TIMEOUT_S * 1000)).fetchall()

    snap_end = l2_at(conn, coin, ts_ms + TIMEOUT_S * 1000)
    if snap_end is None or not snap_end["mid"]:
        return None  # fenêtre HF incomplète → échantillon inexploitable

    for arm, off in ARM_OFFSETS_BPS.items():
        limit_px = mid0 * (1 - sgn * off / 1e4)
        filled = any(
            (sgn > 0 and px < limit_px) or (sgn < 0 and px > limit_px)
            for _, px in trades)
        if filled:
            costs[arm] = shortfall(limit_px) + FEE_MAKER_BPS
        else:
            fb_px = snap_end["mid"] * (1 + sgn * snap_end["spread_bps"] / 2e4)
            costs[arm] = shortfall(fb_px) + FEE_TAKER_BPS
    return costs


# ── Bandit (ridge par bras, full information) ────────────────────────────────
class RidgeBandit:
    def __init__(self) -> None:
        self.A = [np.eye(N_CTX) * RIDGE_LAMBDA for _ in ARMS]
        self.b = [np.zeros(N_CTX) for _ in ARMS]
        self.n = 0
        self.cost_actual = 0.0   # politique du bot (toujours A0)
        self.cost_policy = 0.0   # politique apprise (argmin prédiction)
        self.cost_oracle = 0.0   # meilleur bras a posteriori
        self.arm_counts = [0] * len(ARMS)

    def predict(self, x: np.ndarray) -> np.ndarray:
        return np.array([
            float(x @ np.linalg.solve(self.A[k], self.b[k])) for k in range(len(ARMS))])

    def update(self, x: np.ndarray, costs: dict[int, float]) -> None:
        # évaluation AVANT mise à jour (prequential, sans fuite)
        if self.n >= 50:   # politique gelée tant que <50 obs
            chosen = int(np.argmin(self.predict(x)))
        else:
            chosen = 0
        self.arm_counts[chosen] += 1
        self.cost_actual += costs[0]
        self.cost_policy += costs[chosen]
        self.cost_oracle += min(costs.values())
        for k, c in costs.items():
            self.A[k] += np.outer(x, x)
            self.b[k] += x * c
        self.n += 1

    # — persistance —
    def to_json(self) -> dict:
        return {
            "A": [a.tolist() for a in self.A], "b": [v.tolist() for v in self.b],
            "n": self.n, "cost_actual": self.cost_actual,
            "cost_policy": self.cost_policy, "cost_oracle": self.cost_oracle,
            "arm_counts": self.arm_counts, "arms": ARMS,
            "updated_at": dt.datetime.now().isoformat(),
        }

    @classmethod
    def from_json(cls, d: dict) -> "RidgeBandit":
        rb = cls()
        rb.A = [np.array(a) for a in d["A"]]
        rb.b = [np.array(v) for v in d["b"]]
        rb.n = d["n"]
        rb.cost_actual = d["cost_actual"]
        rb.cost_policy = d["cost_policy"]
        rb.cost_oracle = d["cost_oracle"]
        rb.arm_counts = d["arm_counts"]
        return rb


def load_state() -> tuple[RidgeBandit, set[int]]:
    if STATE_PATH.exists():
        d = json.loads(STATE_PATH.read_text())
        return RidgeBandit.from_json(d["bandit"]), set(d.get("seen_tids", []))
    return RidgeBandit(), set()


def save_state(bandit: RidgeBandit, seen: set[int]) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(
        {"bandit": bandit.to_json(), "seen_tids": sorted(seen)[-20000:]}))


# ── Fills réels ───────────────────────────────────────────────────────────────
def account_address() -> str:
    env = REPO / ".env"
    for line in env.read_text().splitlines():
        if line.startswith("HL_ACCOUNT_ADDRESS="):
            return line.split("=", 1)[1].strip()
    return os.environ.get("HL_ACCOUNT_ADDRESS", "")


def fetch_fills(addr: str) -> list[dict]:
    r = requests.post(HL_API, json={"type": "userFills", "user": addr}, timeout=15)
    r.raise_for_status()
    return r.json()


def process_fills(bandit: RidgeBandit, seen: set[int]) -> int:
    addr = account_address()
    if not addr:
        logger.error("HL_ACCOUNT_ADDRESS introuvable")
        return 0
    conn = hf_conn()
    hf_min = conn.execute("SELECT MIN(ts_ms) FROM l2_1s").fetchone()[0] or 0
    fills = fetch_fills(addr)
    done = 0
    for f in fills:
        tid = int(f.get("tid", 0))
        ts_ms = int(f.get("time", 0))
        if tid in seen or ts_ms < hf_min + 65_000:
            continue
        if ts_ms > time.time() * 1000 - (TIMEOUT_S + 5) * 1000:
            continue   # fenêtre de replay pas encore complète
        coin, side = f.get("coin", ""), f.get("side", "")
        px, sz = float(f.get("px", 0) or 0), float(f.get("sz", 0) or 0)
        if not coin or side not in ("B", "A") or px <= 0:
            seen.add(tid)
            continue
        x = context_at(conn, coin, ts_ms, side, px * sz)
        costs = replay_arms(conn, coin, side, ts_ms, actual_px=px)
        seen.add(tid)
        if x is None or costs is None:
            continue
        bandit.update(x, costs)
        done += 1
    conn.close()
    return done


# ── Self-test : replay sur des points aléatoires HF ──────────────────────────
def selftest(n: int = 400) -> None:
    rng = np.random.default_rng(42)
    conn = hf_conn()
    lo, hi = conn.execute("SELECT MIN(ts_ms), MAX(ts_ms) FROM l2_1s").fetchone()
    if not lo or hi - lo < 300_000:
        logger.error("Pas assez de données HF (%s)", HF_DB)
        return
    coins = [r[0] for r in conn.execute("SELECT DISTINCT coin FROM l2_1s")]
    bandit = RidgeBandit()
    tried = 0
    while bandit.n < n and tried < n * 5:
        tried += 1
        coin = coins[rng.integers(len(coins))]
        ts = int(rng.integers(lo + 70_000, hi - (TIMEOUT_S + 2) * 1000))
        side = "B" if rng.random() < 0.5 else "A"
        x = context_at(conn, coin, ts, side, notional=50.0)
        costs = replay_arms(conn, coin, side, ts, actual_px=None)
        if x is None or costs is None:
            continue
        bandit.update(x, costs)
    conn.close()
    report(bandit, title=f"SELF-TEST ({bandit.n} ordres simulés, fills aléatoires)")


def report(bandit: RidgeBandit, title: str = "État du bandit") -> None:
    print(f"\n── {title} ──")
    if bandit.n == 0:
        print("Aucune observation pour l'instant.")
        return
    n = bandit.n
    print(f"observations           : {n}")
    print(f"coût politique bot     : {bandit.cost_actual / n:+.2f} bps/ordre (toujours taker)")
    print(f"coût politique bandit  : {bandit.cost_policy / n:+.2f} bps/ordre "
          f"(gelée→taker avant 50 obs)")
    print(f"coût oracle a posteriori: {bandit.cost_oracle / n:+.2f} bps/ordre")
    print(f"économie bandit vs bot : {(bandit.cost_actual - bandit.cost_policy) / n:+.2f} bps/ordre")
    print("répartition des choix  : " + ", ".join(
        f"{a}={c}" for a, c in zip(ARMS, bandit.arm_counts)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return
    if args.report:
        bandit, _ = load_state()
        report(bandit)
        return

    bandit, seen = load_state()
    if args.once:
        k = process_fills(bandit, seen)
        save_state(bandit, seen)
        logger.info("%d fill(s) traités (total=%d)", k, bandit.n)
        report(bandit)
        return

    logger.info("Daemon shadow démarré (boucle 300 s) — state: %s", STATE_PATH)
    while True:
        try:
            k = process_fills(bandit, seen)
            if k:
                save_state(bandit, seen)
                logger.info("%d fill(s) traités (total=%d, éco=%.2f bps/ordre)",
                            k, bandit.n,
                            (bandit.cost_actual - bandit.cost_policy) / max(bandit.n, 1))
        except Exception as e:
            logger.warning("cycle: %r", e)
        time.sleep(300)


if __name__ == "__main__":
    main()

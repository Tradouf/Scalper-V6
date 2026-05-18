#!/usr/bin/env python3
"""
Order flow collector — Option C de la roadmap 2026-05-18.

Tourne en daemon (cron @reboot ou systemd). Pour chaque symbole de la watchlist :
  - Sample toutes les 30s :
      * L2 imbalance multi-niveaux (top 1/5/10/20)
      * Bid/ask depth ratio
      * Funding rate courant + predicted
      * Spread
      * Mid price
  - Sample toutes les 5s (haute fréquence, en bonus si non lourd) :
      * Last trade taker side / size
  - Append dans SQLite : memory/orderflow.db

Objectif : accumuler 2-4 semaines de données pour ajouter ces features
au prochain backtest XGBoost (passe attendue de AUC 0.627 → 0.70+).

Usage : python3 scripts/orderflow_collector.py
Stop  : Ctrl+C ou kill PID (auto-flush sur SIGTERM)
"""
from __future__ import annotations
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List

# Charge env pour l'auth HL si besoin
for line in open(Path(__file__).parent.parent / ".env"):
    if "=" in line and not line.startswith("#"):
        k, v = line.strip().split("=", 1)
        os.environ.setdefault(k, v)

sys.path.insert(0, str(Path(__file__).parent.parent))

from hyperliquid.info import Info
import requests

# ── CONFIG ────────────────────────────────────────────────────────────────────
REPO         = Path(__file__).parent.parent
DB_PATH      = REPO / "memory" / "orderflow.db"
WATCHLIST    = ["BTC", "ETH", "SOL", "BNB", "APE", "ATOM", "DYDX", "ZEC", "LINK", "HYPE"]
SAMPLE_SEC   = 30          # fréquence de sampling orderbook (par symbole)
HL_API_URL   = "https://api.hyperliquid.xyz/info"


# ── DB ────────────────────────────────────────────────────────────────────────
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orderflow (
            ts            INTEGER NOT NULL,
            coin          TEXT    NOT NULL,
            mid_px        REAL,
            spread_bps    REAL,
            bid1_sz       REAL,
            ask1_sz       REAL,
            bid5_sz       REAL,
            ask5_sz       REAL,
            bid20_sz      REAL,
            ask20_sz      REAL,
            imb1          REAL,
            imb5          REAL,
            imb20         REAL,
            funding       REAL,
            mark_px       REAL,
            PRIMARY KEY (ts, coin)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_coin_ts ON orderflow(coin, ts)")
    conn.commit()
    return conn


# ── HL API helpers ────────────────────────────────────────────────────────────
def fetch_l2(coin: str) -> dict:
    try:
        r = requests.post(HL_API_URL, json={"type": "l2Book", "coin": coin}, timeout=4)
        return r.json() if r.ok else {}
    except Exception:
        return {}


def fetch_meta_ctxs() -> List[dict]:
    """Funding rates + mark prices pour tous les coins en 1 call."""
    try:
        r = requests.post(HL_API_URL, json={"type": "metaAndAssetCtxs"}, timeout=4)
        if not r.ok:
            return []
        data = r.json()
        # Format : [{universe: [...]}, [ctxs...]]
        if isinstance(data, list) and len(data) >= 2:
            return data[1]
        return []
    except Exception:
        return []


def parse_l2(l2: dict) -> dict:
    """Extrait métriques imbalance + depth du carnet."""
    if not l2 or "levels" not in l2:
        return {}
    levels = l2["levels"]   # [[bids], [asks]]
    if len(levels) < 2 or not levels[0] or not levels[1]:
        return {}
    bids, asks = levels[0], levels[1]

    best_bid = float(bids[0]["px"]) if bids else 0
    best_ask = float(asks[0]["px"]) if asks else 0
    mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 0
    spread = (best_ask - best_bid) if best_bid and best_ask else 0
    spread_bps = (spread / mid * 10000) if mid > 0 else 0

    def sz_at(side_levels, n):
        return sum(float(lv["sz"]) for lv in side_levels[:n]) if side_levels else 0

    bid1, ask1 = sz_at(bids, 1), sz_at(asks, 1)
    bid5, ask5 = sz_at(bids, 5), sz_at(asks, 5)
    bid20, ask20 = sz_at(bids, 20), sz_at(asks, 20)

    def imb(b, a):
        tot = b + a
        return (b - a) / tot if tot > 0 else 0

    return {
        "mid_px": mid, "spread_bps": spread_bps,
        "bid1_sz": bid1, "ask1_sz": ask1, "imb1": imb(bid1, ask1),
        "bid5_sz": bid5, "ask5_sz": ask5, "imb5": imb(bid5, ask5),
        "bid20_sz": bid20, "ask20_sz": ask20, "imb20": imb(bid20, ask20),
    }


# ── MAIN LOOP ─────────────────────────────────────────────────────────────────
_RUNNING = True


def _sig(*_):
    global _RUNNING
    _RUNNING = False
    print("Stop demandé, flush + exit...", flush=True)


def main() -> int:
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    conn = init_db()
    print(f"[orderflow] DB → {DB_PATH}")
    print(f"[orderflow] watchlist : {WATCHLIST}")
    print(f"[orderflow] sample {SAMPLE_SEC}s | Ctrl+C pour stop")

    last_meta_ts = 0
    meta_cache: Dict[str, dict] = {}

    while _RUNNING:
        t0 = time.time()
        now_ms = int(t0 * 1000)

        # Refresh funding/mark cache toutes les 60s
        if t0 - last_meta_ts > 60:
            ctxs = fetch_meta_ctxs()
            try:
                # Récupère l'universe pour mapper index → coin name
                u = requests.post(HL_API_URL, json={"type": "meta"}, timeout=4).json()
                universe = u.get("universe", [])
                for i, ctx in enumerate(ctxs):
                    if i < len(universe):
                        coin = universe[i]["name"].upper()
                        meta_cache[coin] = {
                            "funding": float(ctx.get("funding", 0) or 0),
                            "mark_px": float(ctx.get("markPx", 0) or 0),
                        }
                last_meta_ts = t0
            except Exception as e:
                print(f"[orderflow] meta refresh failed : {e}")

        # Sample chaque symbole
        rows = []
        for coin in WATCHLIST:
            l2 = fetch_l2(coin)
            features = parse_l2(l2)
            if not features:
                continue
            meta = meta_cache.get(coin, {})
            features.update(meta)
            features["ts"] = now_ms
            features["coin"] = coin
            rows.append(features)

        if rows:
            try:
                cols = ("ts", "coin", "mid_px", "spread_bps",
                        "bid1_sz", "ask1_sz", "bid5_sz", "ask5_sz", "bid20_sz", "ask20_sz",
                        "imb1", "imb5", "imb20", "funding", "mark_px")
                conn.executemany(
                    f"INSERT OR REPLACE INTO orderflow ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})",
                    [tuple(r.get(c) for c in cols) for r in rows],
                )
                conn.commit()
            except Exception as e:
                print(f"[orderflow] DB write failed : {e}")

        # Pace pour rester à SAMPLE_SEC entre cycles
        elapsed = time.time() - t0
        sleep_s = max(0, SAMPLE_SEC - elapsed)
        if sleep_s > 0:
            time.sleep(sleep_s)

    conn.close()
    print("[orderflow] exit propre")
    return 0


if __name__ == "__main__":
    sys.exit(main())

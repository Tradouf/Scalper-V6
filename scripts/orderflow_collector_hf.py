#!/usr/bin/env python3
"""
Orderflow collector HAUTE FRÉQUENCE — fondations RL/Transformer (2026-06-06).

Différences vs scripts/orderflow_collector.py (REST 30s, repo _fixed) :
  - WebSocket HL (push) au lieu de polling REST → cadence 1s sans rate-limit
  - Couvre les 8 symboles tradés par la V7 (le collecteur 30s en manque 3)
  - Flux trades brut (côté taker, taille, prix) → indispensable pour le futur
    simulateur d'exécution RL
  - Funding/mark/OI via activeAssetCtx (push)

Tables (data/orderflow_hf.db, WAL) :
  l2_1s  : snapshot 1s par coin — mid, spread, depth/imbalance top 1/5/10/20,
           n_updates (activité du carnet dans la seconde), funding, mark, oi
  trades : chaque trade public — ts_ms, coin, side (B=buy taker), px, sz

Le collecteur 30s de _fixed continue de tourner en parallèle (continuité du
dataset XGB baseline). Volume attendu ici : ~150 Mo/jour.

Usage : python3 scripts/orderflow_collector_hf.py
Stop  : SIGTERM/SIGINT (flush propre). Conçu pour systemd Restart=always.
"""
from __future__ import annotations

import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from hyperliquid.info import Info  # noqa: E402

# ── CONFIG ────────────────────────────────────────────────────────────────────
SYMBOLS = ["BTC", "ETH", "SOL", "BNB", "AAVE", "LINK", "SUI", "DOGE"]  # = allocation.yaml
DB_PATH = REPO / "data" / "orderflow_hf.db"
SAMPLE_SEC = 1.0          # cadence snapshot L2
FLUSH_SEC = 5.0           # batch insert SQLite
STALE_WS_SEC = 30.0       # aucun message WS depuis N s → tentative de reconnexion
# 2026-06-13 : si AUCUNE donnée réelle écrite depuis N s malgré les reconnexions,
# le process est wedgé (WS connecté mais muet — observé 06-08→06-13, 5j de mort
# silencieuse sous "active"). On sort en erreur → systemd Restart=always relance
# un process neuf (Info + souscriptions fraîches). Watchdog dur, pas de reconnexion
# in-process qui s'enlise.
WATCHDOG_DEAD_SEC = 120.0
# 2026-07-04 : le watchdog ci-dessus vit DANS la boucle principale → si connect()/
# disconnect() bloque (souscription sans timeout pendant une panne réseau), la boucle
# gèle et le watchdog avec elle (incident 06-21→07-01 : 9,5j de zombie silencieux,
# dernier log « Connection lost » 06-21 01:29 puis plus rien). Second étage : thread
# daemon imblocable qui os._exit(1) si aucune donnée depuis 2× le seuil — le soft
# (flush propre) tire en premier quand la boucle est vivante ; le hard ne sert que
# quand la boucle elle-même est wedgée.
WATCHDOG_HARD_SEC = 2 * WATCHDOG_DEAD_SEC

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("sdm.orderflow_hf")


# ── DB ────────────────────────────────────────────────────────────────────────
def init_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS l2_1s (
            ts_ms     INTEGER NOT NULL,
            coin      TEXT    NOT NULL,
            mid_px    REAL,
            spread_bps REAL,
            bid1_sz   REAL, ask1_sz  REAL,
            bid5_sz   REAL, ask5_sz  REAL,
            bid10_sz  REAL, ask10_sz REAL,
            bid20_sz  REAL, ask20_sz REAL,
            imb1      REAL, imb5 REAL, imb10 REAL, imb20 REAL,
            n_updates INTEGER,
            funding   REAL,
            mark_px   REAL,
            oi        REAL,
            PRIMARY KEY (ts_ms, coin)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            ts_ms INTEGER NOT NULL,
            coin  TEXT    NOT NULL,
            side  TEXT,
            px    REAL,
            sz    REAL,
            tid   INTEGER
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_l2_coin_ts ON l2_1s(coin, ts_ms)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tr_coin_ts ON trades(coin, ts_ms)")
    conn.commit()
    return conn


# ── État partagé (callbacks WS → boucle de sampling) ─────────────────────────
class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.books: dict[str, dict] = {}        # coin → dernier l2Book
        self.book_updates: dict[str, int] = {}  # coin → compteur depuis dernier sample
        self.ctx: dict[str, dict] = {}          # coin → {funding, mark_px, oi}
        self.trade_rows: list[tuple] = []
        self.last_msg_ts = time.time()
        self.last_good_ts = time.time()  # dernier instant où des books réels sont arrivés
        self.running = True


STATE = State()


def on_l2(msg: dict) -> None:
    data = msg.get("data", {})
    coin = data.get("coin")
    if not coin:
        return
    with STATE.lock:
        STATE.books[coin] = data
        STATE.book_updates[coin] = STATE.book_updates.get(coin, 0) + 1
        STATE.last_msg_ts = time.time()


def on_trades(msg: dict) -> None:
    rows = []
    for t in msg.get("data", []):
        try:
            rows.append((
                int(t["time"]), t["coin"], t.get("side", ""),
                float(t["px"]), float(t["sz"]), int(t.get("tid", 0)),
            ))
        except (KeyError, ValueError, TypeError):
            continue
    if rows:
        with STATE.lock:
            STATE.trade_rows.extend(rows)
            STATE.last_msg_ts = time.time()


def on_ctx(msg: dict) -> None:
    data = msg.get("data", {})
    coin = data.get("coin")
    ctx = data.get("ctx", {})
    if not coin or not ctx:
        return
    with STATE.lock:
        STATE.ctx[coin] = {
            "funding": float(ctx.get("funding", 0) or 0),
            "mark_px": float(ctx.get("markPx", 0) or 0),
            "oi": float(ctx.get("openInterest", 0) or 0),
        }
        STATE.last_msg_ts = time.time()


# ── Features L2 ───────────────────────────────────────────────────────────────
def _depth(levels: list[dict], n: int) -> float:
    return sum(float(l["sz"]) for l in levels[:n])


def book_row(coin: str, book: dict, n_updates: int, ctx: dict, ts_ms: int) -> tuple | None:
    levels = book.get("levels")
    if not levels or len(levels) != 2 or not levels[0] or not levels[1]:
        return None
    bids, asks = levels
    best_bid, best_ask = float(bids[0]["px"]), float(asks[0]["px"])
    if best_bid <= 0 or best_ask <= 0:
        return None
    mid = (best_bid + best_ask) / 2
    spread_bps = (best_ask - best_bid) / mid * 1e4
    row = [ts_ms, coin, mid, spread_bps]
    imbs = []
    for n in (1, 5, 10, 20):
        b, a = _depth(bids, n), _depth(asks, n)
        row += [b, a]
        imbs.append((b - a) / (b + a) if (b + a) > 0 else 0.0)
    row += imbs
    row += [n_updates, ctx.get("funding"), ctx.get("mark_px"), ctx.get("oi")]
    return tuple(row)


# ── WS lifecycle ──────────────────────────────────────────────────────────────
def connect() -> Info:
    info = Info(skip_ws=False)
    for coin in SYMBOLS:
        info.subscribe({"type": "l2Book", "coin": coin}, on_l2)
        info.subscribe({"type": "trades", "coin": coin}, on_trades)
        info.subscribe({"type": "activeAssetCtx", "coin": coin}, on_ctx)
    logger.info("WS connecté — %d souscriptions (%d coins)", 3 * len(SYMBOLS), len(SYMBOLS))
    return info


def disconnect(info: Info) -> None:
    try:
        info.disconnect_websocket()
    except Exception as e:
        logger.warning("disconnect_websocket: %r", e)


# ── Watchdog dur (thread daemon, imblocable) ──────────────────────────────────
def hard_watchdog() -> None:
    """os._exit(1) si aucune donnée réelle depuis WATCHDOG_HARD_SEC.

    Tourne dans un thread daemon : survit à une boucle principale gelée dans
    connect()/disconnect() (cause du zombie 06-21→07-01). os._exit court-circuite
    tout (pas de flush — perte max = FLUSH_SEC de batch, le WAL SQLite encaisse) ;
    systemd Restart=always relance un process neuf.
    """
    while True:
        time.sleep(10.0)
        if not STATE.running:
            return  # arrêt propre en cours (SIGTERM/SIGINT) — ne pas interférer
        dead_for = time.time() - STATE.last_good_ts
        if dead_for > WATCHDOG_HARD_SEC:
            logger.critical(
                "WATCHDOG DUR : aucune donnée réelle depuis %.0fs (> %.0fs) et le soft "
                "n'a pas tiré (boucle principale gelée ?) → os._exit(1) pour relance systemd",
                dead_for, WATCHDOG_HARD_SEC,
            )
            logging.shutdown()
            os._exit(1)


# ── Main loop ─────────────────────────────────────────────────────────────────
def main() -> None:
    conn = init_db()
    info = connect()

    def _stop(signum, frame):
        logger.info("Signal %s reçu, arrêt…", signum)
        STATE.running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    l2_batch: list[tuple] = []
    last_flush = time.time()
    n_l2_total = n_tr_total = 0
    last_report = time.time()
    STATE.last_good_ts = time.time()
    threading.Thread(target=hard_watchdog, name="hard_watchdog", daemon=True).start()

    while STATE.running:
        t0 = time.time()
        ts_ms = int(t0 * 1000)

        with STATE.lock:
            books = dict(STATE.books)
            updates = dict(STATE.book_updates)
            STATE.book_updates = {}
            ctxs = dict(STATE.ctx)
            tr_rows, STATE.trade_rows = STATE.trade_rows, []
            stale = (t0 - STATE.last_msg_ts) > STALE_WS_SEC

        got_data = False
        for coin, book in books.items():
            row = book_row(coin, book, updates.get(coin, 0), ctxs.get(coin, {}), ts_ms)
            if row:
                l2_batch.append(row)
                got_data = True
        if got_data:
            STATE.last_good_ts = t0

        # Watchdog dur : données réelles absentes trop longtemps → exit, systemd
        # relance un process neuf (la reconnexion in-process ne re-souscrit pas
        # fiablement et finit wedgée — cf. incident 06-08→06-13).
        if t0 - STATE.last_good_ts > WATCHDOG_DEAD_SEC:
            logger.critical(
                "WATCHDOG : aucune donnée réelle depuis %.0fs (> %.0fs) → exit pour relance systemd",
                t0 - STATE.last_good_ts, WATCHDOG_DEAD_SEC,
            )
            # Flush du buffer avant de sortir.
            try:
                if l2_batch:
                    with conn:
                        conn.executemany(
                            "INSERT OR IGNORE INTO l2_1s VALUES (" + ",".join("?" * 20) + ")", l2_batch)
            except sqlite3.Error:
                pass
            disconnect(info)
            conn.close()
            sys.exit(1)

        if stale:
            logger.warning("WS muet depuis >%ss → tentative de reconnexion", STALE_WS_SEC)
            disconnect(info)
            with STATE.lock:
                STATE.books.clear()
                STATE.last_msg_ts = time.time()
            info = connect()

        if tr_rows or (l2_batch and t0 - last_flush >= FLUSH_SEC):
            try:
                with conn:
                    if l2_batch:
                        conn.executemany(
                            "INSERT OR IGNORE INTO l2_1s VALUES (" + ",".join("?" * 20) + ")",
                            l2_batch,
                        )
                        n_l2_total += len(l2_batch)
                        l2_batch = []
                    if tr_rows:
                        conn.executemany(
                            "INSERT INTO trades VALUES (?,?,?,?,?,?)", tr_rows,
                        )
                        n_tr_total += len(tr_rows)
                last_flush = t0
            except sqlite3.Error as e:
                logger.error("SQLite flush: %r", e)

        if t0 - last_report >= 300:
            logger.info("collecte: l2_1s=%d trades=%d coins_actifs=%d",
                        n_l2_total, n_tr_total, len(books))
            last_report = t0

        time.sleep(max(0.0, SAMPLE_SEC - (time.time() - t0)))

    # Flush final
    with conn:
        if l2_batch:
            conn.executemany(
                "INSERT OR IGNORE INTO l2_1s VALUES (" + ",".join("?" * 20) + ")", l2_batch)
        with STATE.lock:
            if STATE.trade_rows:
                conn.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?)", STATE.trade_rows)
    disconnect(info)
    conn.close()
    logger.info("Arrêt propre. Total l2_1s=%d trades=%d", n_l2_total, n_tr_total)


if __name__ == "__main__":
    main()

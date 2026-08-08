"""
LIQFEED — capture du flux de liquidations Hyperliquid (2026-08-08).

POURQUOI
--------
La fenêtre de tir RSI-MR (`FENETRE_DE_TIR_2026-08-08.md`) conditionne sur le
RÉGIME DE VOLATILITÉ, qui n'est qu'une *ombre* de la vraie cause : des
vendeurs contraints (liquidations forcées) qui finissent par s'épuiser.
Ce module observe la cause directement.

CE QUE HYPERLIQUID EXPOSE RÉELLEMENT (sondé le 08-08-2026)
----------------------------------------------------------
- ❌ aucun flux public global de liquidations (ni `info`, ni souscription WS) ;
- ✅ `activeAssetCtx` (WS, push) : openInterest, funding, premium, markPx ;
- ✅ `trades` (WS, push) : px, sz, side taker, et les DEUX contreparties ;
- ✅ `userFills` (REST, PUBLIC pour n'importe quelle adresse) : le champ
  `liquidation` {liquidatedUser, markPx, method} et `dir` = "Liquidated ...".

D'où une capture à deux étages :

1. SIGNATURE (complète, push, sans coût API) — par coin et par seconde :
   ΔopenInterest + volume taker signé. Une liquidation de longs = OI qui
   BAISSE pendant que le taker vend. Un short volontaire = OI qui MONTE.
   C'est ce qui distingue le vendeur contraint du vendeur qui choisit.

2. VÉRITÉ TERRAIN (exacte, éparse) — quand une rafale est détectée, on note
   les adresses présentes dans les trades, puis (à froid, via le rate-limiter
   partagé) on interroge leurs `userFills` : tout fill portant `liquidation`
   est enregistré tel quel. Cela donne un jeu ÉTIQUETÉ pour mesurer si la
   signature de l'étage 1 attrape vraiment les liquidations.

AUCUN ORDRE, AUCUNE DÉCISION DE TRADING ICI — collecte seule.

Tables (rsimr/liq.db, WAL) :
  sec    : (ts_sec, coin) → oi, d_oi, mark, funding, premium,
           buy_ntl, sell_ntl, n_trades, max_ntl
  liq    : liquidations confirmées (ts, coin, side, px, sz, dir,
           liquidated_user, method, source)
  probe  : journal des vérifications (combien d'adresses testées, trouvées)
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
import urllib.request
from collections import defaultdict, deque
from pathlib import Path

import websocket

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from hl_rate_limit import throttle_before_hl_request  # noqa: E402

WS_URL = "wss://api.hyperliquid.xyz/ws"
INFO_URL = "https://api.hyperliquid.xyz/info"
DB_PATH = Path(os.environ.get("LIQFEED_DB", REPO / "rsimr" / "liq.db"))
STALE_SEC = float(os.environ.get("LIQFEED_STALE_SEC", "90"))
FLUSH_SEC = 5.0
# rafale = ΔOI négatif marqué sur la fenêtre + agression vendeuse
BURST_DOI_PCT = float(os.environ.get("LIQFEED_BURST_DOI_PCT", "0.0015"))
BURST_WINDOW_SEC = int(os.environ.get("LIQFEED_BURST_WINDOW_SEC", "60"))
PROBE_MAX_ADDR = int(os.environ.get("LIQFEED_PROBE_MAX_ADDR", "6"))
PROBE_MIN_GAP_SEC = float(os.environ.get("LIQFEED_PROBE_MIN_GAP_SEC", "45"))
# Budget GLOBAL de requêtes de vérification. Indispensable : une vraie cascade
# fait éclater les 45 coins EN MÊME TEMPS (c'est sa définition), soit ~270
# requêtes d'un coup sur le rate-limiter partagé avec les autres bots. On
# préfère échantillonner la cascade plutôt que d'étrangler l'infra.
PROBE_BUDGET_PER_MIN = int(os.environ.get("LIQFEED_PROBE_BUDGET_PER_MIN", "20"))
# sous-vaults HLP servant de backstop liquidator (vérité terrain gratuite)
BACKSTOP = ["0x2ed5c4484ea3ff8b57d5f2fb152a40d9f2b68308",
            "0xb0a55f13d22f66e6d495ac98113841b2326e9540"]
BACKSTOP_POLL_SEC = float(os.environ.get("LIQFEED_BACKSTOP_POLL_SEC", "600"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("sdm.liqfeed")

_stop = threading.Event()


def universe() -> list[str]:
    """Univers = celui de la fenêtre de tir : les alts du cache 15m."""
    raw = os.environ.get("LIQFEED_SYMBOLS", "").strip()
    if raw:
        return [s.strip() for s in raw.split(",") if s.strip()]
    cache = REPO / "state" / "ohlcv_cache"
    majors = {"BTC", "ETH", "SOL"}
    syms = []
    for p in sorted(cache.glob("*__15m.json")):
        try:
            n = len(json.loads(p.read_text())["candles"])
        except Exception:
            continue
        s = p.name.split("__")[0]
        if n >= 4000 and s not in majors:
            syms.append(s)
    return syms


def db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False : la connexion est partagée entre le thread de
    # flush, celui du backstop et les sondes de rafale — tous les accès passent
    # par self.lock, la sérialisation est donc assurée côté application.
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS sec (
            ts_sec INTEGER, coin TEXT, oi REAL, d_oi REAL, mark REAL,
            funding REAL, premium REAL, buy_ntl REAL, sell_ntl REAL,
            n_trades INTEGER, max_ntl REAL,
            PRIMARY KEY (ts_sec, coin));
        CREATE TABLE IF NOT EXISTS liq (
            ts INTEGER, coin TEXT, side TEXT, px REAL, sz REAL, ntl REAL,
            dir TEXT, liquidated_user TEXT, method TEXT, source TEXT,
            tid INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS probe (
            ts INTEGER, coin TEXT, n_addr INTEGER, n_liq INTEGER,
            d_oi_pct REAL, sell_ratio REAL);
        CREATE INDEX IF NOT EXISTS idx_liq_ts ON liq(ts);
        CREATE INDEX IF NOT EXISTS idx_sec_coin ON sec(coin, ts_sec);
    """)
    con.commit()
    return con


def post_info(body: dict, timeout: float = 20.0):
    throttle_before_hl_request()
    req = urllib.request.Request(
        INFO_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


class Collector:
    def __init__(self, symbols: list[str]):
        self.symbols = symbols
        self.con = db_connect()
        self.lock = threading.Lock()
        self.ctx: dict[str, dict] = {}
        self.prev_oi: dict[str, float] = {}
        self.buckets: dict[tuple[int, str], dict] = defaultdict(
            lambda: {"buy": 0.0, "sell": 0.0, "n": 0, "max": 0.0})
        # historique court pour la détection de rafale
        self.hist: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=BURST_WINDOW_SEC))
        self.recent_addr: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=400))
        self.last_probe: dict[str, float] = {}
        self.probe_times: deque = deque(maxlen=4 * PROBE_BUDGET_PER_MIN)
        self.probe_skipped = 0
        self.last_msg = time.time()
        self.n_trades = 0
        self.n_liq = 0

    # ── WebSocket ────────────────────────────────────────────────────────────

    def on_trade(self, d: dict):
        coin = d["coin"]
        px, sz = float(d["px"]), float(d["sz"])
        ntl = px * sz
        ts_sec = int(d["time"]) // 1000
        with self.lock:
            b = self.buckets[(ts_sec, coin)]
            # side "B" = taker acheteur, "A" = taker vendeur
            if d["side"] == "B":
                b["buy"] += ntl
            else:
                b["sell"] += ntl
            b["n"] += 1
            b["max"] = max(b["max"], ntl)
            self.n_trades += 1
            for u in d.get("users", []):
                self.recent_addr[coin].append((int(d["time"]), u))

    def on_ctx(self, d: dict):
        coin = d["coin"]
        c = d["ctx"]
        try:
            oi = float(c["openInterest"])
        except (KeyError, TypeError, ValueError):
            return
        with self.lock:
            self.ctx[coin] = {
                "oi": oi, "mark": float(c.get("markPx") or 0),
                "funding": float(c.get("funding") or 0),
                "premium": float(c.get("premium") or 0)}

    def ws_loop(self):
        while not _stop.is_set():
            try:
                ws = websocket.WebSocket()
                ws.connect(WS_URL, timeout=20)
                for s in self.symbols:
                    ws.send(json.dumps({"method": "subscribe", "subscription": {
                        "type": "trades", "coin": s}}))
                    ws.send(json.dumps({"method": "subscribe", "subscription": {
                        "type": "activeAssetCtx", "coin": s}}))
                    time.sleep(0.02)
                ws.settimeout(10)
                logger.info("WS connecté — %d symboles souscrits", len(self.symbols))
                while not _stop.is_set():
                    try:
                        m = json.loads(ws.recv())
                    except websocket.WebSocketTimeoutException:
                        if time.time() - self.last_msg > STALE_SEC:
                            logger.warning("WS muet %.0fs → reconnexion", STALE_SEC)
                            break
                        continue
                    self.last_msg = time.time()
                    ch = m.get("channel")
                    if ch == "trades":
                        for t in m["data"]:
                            self.on_trade(t)
                    elif ch == "activeAssetCtx":
                        self.on_ctx(m["data"])
                try:
                    ws.close()
                except Exception:
                    pass
            except Exception as e:
                logger.warning("WS erreur: %r — retry 5s", e)
                _stop.wait(5.0)

    # ── Persistance seconde par seconde ─────────────────────────────────────

    def flush_loop(self):
        while not _stop.is_set():
            _stop.wait(FLUSH_SEC)
            now = int(time.time())
            rows = []
            bursts = []
            with self.lock:
                keys = [k for k in self.buckets if k[0] < now - 1]
                for k in keys:
                    ts_sec, coin = k
                    b = self.buckets.pop(k)
                    c = self.ctx.get(coin)
                    if not c:
                        continue
                    prev = self.prev_oi.get(coin)
                    d_oi = (c["oi"] - prev) if prev is not None else 0.0
                    self.prev_oi[coin] = c["oi"]
                    rows.append((ts_sec, coin, c["oi"], d_oi, c["mark"],
                                 c["funding"], c["premium"], b["buy"],
                                 b["sell"], b["n"], b["max"]))
                    self.hist[coin].append((ts_sec, d_oi, c["oi"],
                                            b["buy"], b["sell"]))
                # détection de rafale par coin
                for coin, h in self.hist.items():
                    if len(h) < 10:
                        continue
                    oi_now = h[-1][2]
                    d_sum = sum(x[1] for x in h)
                    buy = sum(x[3] for x in h)
                    sell = sum(x[4] for x in h)
                    tot = buy + sell
                    if oi_now <= 0 or tot <= 0:
                        continue
                    d_pct = d_sum / oi_now
                    sell_ratio = sell / tot
                    if d_pct <= -BURST_DOI_PCT and sell_ratio >= 0.6:
                        if time.time() - self.last_probe.get(coin, 0) > PROBE_MIN_GAP_SEC:
                            self.last_probe[coin] = time.time()
                            addrs = [u for _, u in list(self.recent_addr[coin])[-120:]]
                            bursts.append((coin, d_pct, sell_ratio, addrs))
            if rows:
                with self.lock:
                    self.con.executemany(
                        "INSERT OR REPLACE INTO sec VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        rows)
                    self.con.commit()
            for coin, d_pct, sr, addrs in bursts:
                threading.Thread(target=self.verify_burst, daemon=True,
                                 args=(coin, d_pct, sr, addrs)).start()

    # ── Vérification : liquidations exactes via userFills (public) ───────────

    def record_fills(self, fills, coin_filter=None, source="probe",
                     recent_after_ms: int | None = None) -> tuple[int, int]:
        """Enregistre les fills portant `liquidation`.

        Sonder une adresse renvoie TOUT son historique (jusqu'à 2000 fills) :
        on garde tout — ce sont de vraies liquidations horodatées, donc de
        l'historique gratuit — mais on distingue ce qui vient de la fenêtre
        courante, sans quoi une rafale semblerait avoir provoqué des
        liquidations vieilles de plusieurs semaines.

        Renvoie (total enregistré, dont récents).
        """
        n = 0
        n_recent = 0
        rows = []
        for f in fills or []:
            liq = f.get("liquidation")
            if not liq:
                continue
            if coin_filter and f.get("coin") != coin_filter:
                continue
            px, sz = float(f["px"]), float(f["sz"])
            ts = int(f["time"])
            rows.append((ts, f["coin"], f.get("side"), px, sz,
                         px * sz, f.get("dir"), liq.get("liquidatedUser"),
                         str(liq.get("method")), source, int(f["tid"])))
            n += 1
            if recent_after_ms is not None and ts >= recent_after_ms:
                n_recent += 1
        if rows:
            with self.lock:
                self.con.executemany(
                    "INSERT OR IGNORE INTO liq VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    rows)
                self.con.commit()
                self.n_liq += n
        return n, n_recent

    def _take_probe_token(self) -> bool:
        """Budget glissant sur 60 s, partagé par toutes les rafales."""
        now = time.time()
        with self.lock:
            while self.probe_times and now - self.probe_times[0] > 60.0:
                self.probe_times.popleft()
            if len(self.probe_times) >= PROBE_BUDGET_PER_MIN:
                return False
            self.probe_times.append(now)
            return True

    def verify_burst(self, coin: str, d_pct: float, sell_ratio: float,
                     addrs: list[str]):
        """Interroge quelques contreparties de la rafale : liquidation ou pas ?"""
        uniq = []
        for a in reversed(addrs):
            if a not in uniq:
                uniq.append(a)
            if len(uniq) >= PROBE_MAX_ADDR:
                break
        found = recent = 0
        cutoff = int((time.time() - 4 * BURST_WINDOW_SEC) * 1000)
        for a in uniq:
            if _stop.is_set():
                break
            if not self._take_probe_token():
                self.probe_skipped += 1
                break
            try:
                fills = post_info({"type": "userFills", "user": a})
            except Exception as e:
                logger.debug("probe %s: %r", a[:10], e)
                continue
            tot, rec = self.record_fills(fills, coin_filter=coin,
                                         source="burst", recent_after_ms=cutoff)
            found += tot
            recent += rec
        with self.lock:
            self.con.execute("INSERT INTO probe VALUES (?,?,?,?,?,?)",
                             (int(time.time() * 1000), coin, len(uniq), recent,
                              d_pct, sell_ratio))
            self.con.commit()
        logger.info("rafale %s ΔOI %.2f%% vente %.0f%% → %d fills de "
                    "liquidation pendant la rafale (%d au total avec "
                    "l'historique des %d adresses)", coin, 100 * d_pct,
                    100 * sell_ratio, recent, found, len(uniq))

    def backstop_loop(self):
        """Vérité terrain gratuite : fills du liquidator de secours HLP."""
        while not _stop.is_set():
            for a in BACKSTOP:
                if _stop.is_set():
                    break
                try:
                    n, _ = self.record_fills(post_info(
                        {"type": "userFills", "user": a}), source="backstop")
                    if n:
                        logger.info("backstop %s: %d fills de liquidation",
                                    a[:10], n)
                except Exception as e:
                    logger.debug("backstop %s: %r", a[:10], e)
            _stop.wait(BACKSTOP_POLL_SEC)

    def status_loop(self):
        while not _stop.is_set():
            _stop.wait(300)
            with self.lock:
                n_sec = self.con.execute("SELECT COUNT(*) FROM sec").fetchone()[0]
                n_liq = self.con.execute("SELECT COUNT(*) FROM liq").fetchone()[0]
            logger.info("état: %d trades vus, %d lignes/sec en base, "
                        "%d liquidations confirmées, %d sondes hors budget",
                        self.n_trades, n_sec, n_liq, self.probe_skipped)

    def run(self):
        logger.info("LIQFEED démarre — %d symboles, base %s",
                    len(self.symbols), DB_PATH)
        threads = [threading.Thread(target=f, daemon=True) for f in
                   (self.ws_loop, self.flush_loop, self.backstop_loop,
                    self.status_loop)]
        for t in threads:
            t.start()
        while not _stop.is_set():
            _stop.wait(1.0)
        logger.info("arrêt demandé — flush")
        with self.lock:
            self.con.commit()
            self.con.close()


def main():
    def _sig(_s, _f):
        _stop.set()
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    syms = universe()
    if not syms:
        logger.error("univers vide")
        return 1
    Collector(syms).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

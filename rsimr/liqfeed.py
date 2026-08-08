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
# ── Détection de rafale — CALIBRÉE sur vérité terrain le 08-08 ──────────────
# v1 (devinée) : ΔOI ≤ −0.15 % ET ≥60 % d'agression vendeuse
#   → 4 confirmations sur 513 rafales, soit ~0.8 % de précision. Inutilisable.
# Mesuré sur 40 liquidations confirmées vs 2507 témoins (fenêtre 60 s) :
#   - ΔOI relatif SÉPARE : méd −0.19 % contre 0.00 % (une liquidation ferme
#     une position, donc l'open interest baisse — mécanique, pas statistique) ;
#   - taille du plus gros trade SÉPARE : méd 2081 $ contre 400 $ ;
#   - le RATIO DE VENTE NE SÉPARE PAS : 0.37 sur les liquidations contre 0.42
#     sur les témoins. Le côté taker publié ne reflète pas le sens de la
#     liquidation ⇒ l'exiger EXCLUAIT les vrais événements. Critère supprimé,
#     mais toujours enregistré pour analyse.
# v2 retenue : ΔOI ≤ −0.20 % ET plus gros trade ≥ 800 $
#   → précision 48.8 %, rappel 50 %, lift 31× (20 vrais / 21 faux).
# Réserve : 40 événements seulement, une fenêtre de 4.6 h. À recalibrer quand
# la base aura quelques jours (script calib_liq2.py).
BURST_DOI_PCT = float(os.environ.get("LIQFEED_BURST_DOI_PCT", "0.002"))
BURST_MIN_MAX_NTL = float(os.environ.get("LIQFEED_BURST_MIN_MAX_NTL", "800"))
BURST_WINDOW_SEC = int(os.environ.get("LIQFEED_BURST_WINDOW_SEC", "60"))
PROBE_MAX_ADDR = int(os.environ.get("LIQFEED_PROBE_MAX_ADDR", "6"))
# Délai de garde ≥ fenêtre d'analyse : à 45 s pour une fenêtre de 60 s, la même
# chute d'OI restait dans la fenêtre et se redéclenchait indéfiniment (observé :
# INJ compté 4 fois en 3 min avec des features identiques). Le délai seul ne
# suffit pas — voir l'hystérésis ci-dessous.
PROBE_MIN_GAP_SEC = float(os.environ.get("LIQFEED_PROBE_MIN_GAP_SEC", "120"))
# Hystérésis : après un déclenchement, le coin reste DÉSARMÉ tant que la chute
# n'est pas retombée sous la moitié du seuil. Un événement = un déclenchement.
BURST_REARM_FRAC = float(os.environ.get("LIQFEED_BURST_REARM_FRAC", "0.5"))

# ── Sens du flux forcé, calibré le 08-08 ────────────────────────────────────
# Le prix classe le sens de façon quasi parfaite (79/79 sur les événements
# confirmés) : vente forcée méd −0.60 % sur 60 s, achat forcé méd +0.99 %.
#
# Piège corrigé au passage : 595 des 604 lignes de `liq` sont vues du côté de
# la CONTREPARTIE, pas du liquidé. Si un long est liquidé (vente forcée), la
# contrepartie ACHÈTE — donc `Close Long` chez elle signifie qu'un SHORT a été
# liquidé. Lire naïvement « Long dans dir = long liquidé » inverse le sens et
# détruit le signal (c'est ce qui m'a d'abord fait conclure que le prix ne
# discriminait pas).
#
# La stratégie RSI-MR achète des creux : seule la VENTE FORCÉE la concerne.
# On classe toutes les rafales, mais on ne dépense le budget de sondes que sur
# le côté utile ; les autres sont journalisées avec 0 sonde.
BURST_SIDE = os.environ.get("LIQFEED_BURST_SIDE", "vente")  # vente|achat|les_deux


def forced_side(d_px: float) -> str:
    """Sens du flux forcé d'après la variation de prix."""
    return "vente" if d_px <= 0 else "achat"
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
            d_oi_pct REAL, sell_ratio REAL, d_px_pct REAL, max_ntl REAL,
            side TEXT);
        CREATE INDEX IF NOT EXISTS idx_liq_ts ON liq(ts);
        CREATE INDEX IF NOT EXISTS idx_sec_coin ON sec(coin, ts_sec);
    """)
    # colonnes ajoutées après coup (bases créées avant la calibration v2)
    for col in ("d_px_pct REAL", "max_ntl REAL", "side TEXT"):
        try:
            con.execute(f"ALTER TABLE probe ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
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
        self.armed: dict[str, bool] = defaultdict(lambda: True)
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
            # on retient la TAILLE avec l'adresse : les fills de liquidation
            # sont gros (médiane 2081 $ contre 400 $), donc sonder les plus
            # grosses contreparties trouve bien plus souvent le liquidé que
            # sonder les plus récentes.
            for u in d.get("users", []):
                self.recent_addr[coin].append((int(d["time"]), u, ntl))

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
                    self.hist[coin].append((ts_sec, d_oi, c["oi"], b["buy"],
                                            b["sell"], c["mark"], b["max"]))
                # détection de rafale par coin (règle calibrée v2)
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
                    max_ntl = max(x[6] for x in h)
                    px0 = next((x[5] for x in h if x[5] > 0), 0.0)
                    px1 = next((x[5] for x in reversed(h) if x[5] > 0), 0.0)
                    d_px = (px1 - px0) / px0 if px0 > 0 else 0.0
                    # hystérésis : réarmer dès que la chute s'est résorbée
                    if d_pct > -BURST_DOI_PCT * BURST_REARM_FRAC:
                        self.armed[coin] = True
                    if d_pct <= -BURST_DOI_PCT and max_ntl >= BURST_MIN_MAX_NTL:
                        if (self.armed[coin] and time.time()
                                - self.last_probe.get(coin, 0) > PROBE_MIN_GAP_SEC):
                            self.last_probe[coin] = time.time()
                            self.armed[coin] = False
                            # les plus GROSSES contreparties d'abord
                            recent = list(self.recent_addr[coin])[-200:]
                            recent.sort(key=lambda x: -x[2])
                            addrs = [u for _, u, _ in recent]
                            bursts.append((coin, d_pct, sell_ratio, d_px,
                                           max_ntl, addrs))
            if rows:
                with self.lock:
                    self.con.executemany(
                        "INSERT OR REPLACE INTO sec VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        rows)
                    self.con.commit()
            for coin, d_pct, sr, d_px, max_ntl, addrs in bursts:
                threading.Thread(
                    target=self.verify_burst, daemon=True,
                    args=(coin, d_pct, sr, addrs, d_px, max_ntl)).start()

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
                     addrs: list[str], d_px: float = 0.0,
                     max_ntl: float = 0.0):
        """Interroge les plus grosses contreparties : liquidation ou pas ?

        `addrs` arrive trié par taille décroissante (les fills de liquidation
        sont gros). On enregistre le nombre d'adresses RÉELLEMENT sondées, pas
        celui qu'on aurait voulu sonder — sinon la précision mesurée est
        fausse dès que le budget coupe.
        """
        uniq = []
        for a in addrs:
            if a not in uniq:
                uniq.append(a)
            if len(uniq) >= PROBE_MAX_ADDR:
                break
        side = forced_side(d_px)
        found = recent = probed = 0
        cutoff = int((time.time() - 4 * BURST_WINDOW_SEC) * 1000)
        if BURST_SIDE not in ("les_deux", side):
            # rafale du mauvais côté : journalisée pour les statistiques, mais
            # aucune sonde dépensée (le budget va au côté qui nous concerne)
            with self.lock:
                self.con.execute(
                    "INSERT INTO probe (ts, coin, n_addr, n_liq, d_oi_pct, "
                    "sell_ratio, d_px_pct, max_ntl, side) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (int(time.time() * 1000), coin, 0, 0, d_pct, sell_ratio,
                     d_px, max_ntl, side))
                self.con.commit()
            logger.info("rafale %s ignorée — %s forcé(e) (Δpx %+.2f%%), "
                        "on ne sonde que « %s »", coin, side, 100 * d_px,
                        BURST_SIDE)
            return
        for a in uniq:
            if _stop.is_set():
                break
            if not self._take_probe_token():
                self.probe_skipped += 1
                break
            probed += 1
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
            self.con.execute(
                "INSERT INTO probe (ts, coin, n_addr, n_liq, d_oi_pct, "
                "sell_ratio, d_px_pct, max_ntl, side) VALUES (?,?,?,?,?,?,?,?,?)",
                (int(time.time() * 1000), coin, probed, recent, d_pct,
                 sell_ratio, d_px, max_ntl, side))
            self.con.commit()
        logger.info("rafale %s [%s forcé(e)] ΔOI %.2f%% Δpx %.2f%% gros trade "
                    "%.0f$ → %d liquidation(s) confirmée(s) sur %d adresses "
                    "sondées (%d fills avec leur historique)", coin, side,
                    100 * d_pct, 100 * d_px, max_ntl, recent, probed, found)

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

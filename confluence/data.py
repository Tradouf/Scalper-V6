"""
Chargement de l'historique profond — SPEC §3 et §9.2.

Le §9.2 exige au minimum 3 ans de données. L'endpoint `candleSnapshot`
d'Hyperliquid plafonne à ~5000 bougies par requête et **tronque
silencieusement** au-delà : demander 3 ans de 15m en une fois renverrait ~52
jours sans le moindre avertissement, et le walk-forward tournerait sur une
fenêtre 20 fois trop courte en croyant faire son travail. D'où la pagination en
reculant, avec contrôle d'intégrité systématique.

Trois garanties à la sortie de ce module :

* séries triées, dédoublonnées, sans bougie en cours ;
* trous SIGNALÉS et jamais comblés — une bougie inventée fausse tous les
  indicateurs qui la traversent ;
* bougies aberrantes (OHLC impossible, saut > 50 %) comptées et rapportées.

Le funding est chargé séparément (`fundingHistory`, paginé lui aussi) et aligné
strictement causalement : à chaque bougie, le dernier taux CONNU à cet instant.
"""

from __future__ import annotations

import bisect
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from confluence.indicators import INTERVAL_MS, anomalies, find_gaps, sort_dedup

logger = logging.getLogger("sdm.confluence.data")

CACHE_DIR = Path(__file__).resolve().parent / "state" / "history"
MAX_CANDLES_PER_REQUEST = 5000
Candle = Dict[str, float]


@dataclass
class SeriesReport:
    """Ce qu'on sait de la qualité d'une série. Rapporté, jamais avalé."""

    timeframe: str
    bars: int = 0
    first_ts: int = 0
    last_ts: int = 0
    gaps: List[tuple] = field(default_factory=list)
    anomaly_count: int = 0
    requests: int = 0

    @property
    def missing_bars(self) -> int:
        return sum(g[2] for g in self.gaps)

    @property
    def days(self) -> float:
        if not self.bars:
            return 0.0
        return (self.last_ts - self.first_ts) / 86_400_000.0

    def summary(self) -> str:
        return (f"{self.timeframe}: {self.bars} bougies, {self.days:.0f} j, "
                f"{len(self.gaps)} trou(s) / {self.missing_bars} barres manquantes, "
                f"{self.anomaly_count} bougie(s) aberrante(s), {self.requests} requête(s)")


@dataclass
class FundingProvenance:
    """D'où viennent les taux de funding, et ce qu'ils valent.

    **Le funding du backtest n'est JAMAIS validé.** Quelle que soit sa source,
    il reste une estimation jusqu'à ce que le paper trading l'ait mesuré en
    réel sur le compte, position par position. Deux raisons distinctes :

    * source `binance` — autre lieu, autre cadence de règlement (8 h contre
      1 h), autre déséquilibre long/short. Le taux n'est simplement pas celui
      qu'on paiera ;
    * source `hyperliquid` — le bon lieu et la bonne cadence, mais un taux
      HISTORIQUE appliqué à des positions SIMULÉES. Il ne dit rien du taux que
      le bot rencontrera, ni du moment où il se retrouvera du côté qui paie.

    C'est le poste de coût le moins vérifiable du §9, et sur des positions
    tenues plusieurs jours il peut dépasser les frais de transaction. D'où ce
    marquage explicite, remonté dans tous les rapports.
    """

    source: str = "unknown"          # hyperliquid | binance | none
    points: int = 0
    first_ms: int = 0
    last_ms: int = 0
    settlement_hours: Optional[float] = None
    validated: bool = False          # ne passe à True qu'après mesure en paper

    @property
    def days(self) -> float:
        if not self.points:
            return 0.0
        return (self.last_ms - self.first_ms) / 86_400_000.0

    def summary(self) -> str:
        if not self.points:
            return ("funding: AUCUNE donnée — la couche 1h vetera tout (§4.2), "
                    "et le coût de portage sera compté nul")
        cadence = (f"règlement ~{self.settlement_hours:.0f}h"
                   if self.settlement_hours else "cadence inconnue")
        return (f"funding: {self.points} règlements sur {self.days:.0f} j "
                f"(source {self.source}, {cadence}) — "
                f"{'VALIDÉ' if self.validated else 'NON VALIDÉ jusqu au paper trading'}")


@dataclass
class History:
    symbol: str
    candles: Dict[str, List[Candle]] = field(default_factory=dict)
    funding: List[tuple] = field(default_factory=list)   # [(ts_ms, taux du règlement)]
    reports: Dict[str, SeriesReport] = field(default_factory=dict)
    funding_provenance: "FundingProvenance" = field(default_factory=lambda: FundingProvenance())
    # Début de la fenêtre de DÉCISION (hors warmup). 0 = « dès que le warmup
    # le permet ». Distinguer les deux évite de tester 1301 jours en croyant
    # en tester 1100.
    decision_start_ms: int = 0

    def funding_at(self, ts_ms: int) -> Optional[float]:
        """Dernier taux de funding CONNU à `ts_ms`. Strictement causal : jamais
        le taux qui sera publié après la bougie qu'on est en train de décider."""
        if not self.funding:
            return None
        times = [t for t, _ in self.funding]
        j = bisect.bisect_right(times, ts_ms) - 1
        return self.funding[j][1] if j >= 0 else None

    def slice(self, start_ms: int, end_ms: int) -> "History":
        """Sous-période, pour les fenêtres IS/OOS du walk-forward."""
        return History(
            symbol=self.symbol,
            candles={tf: [c for c in cs if start_ms <= int(c["ts"]) < end_ms]
                     for tf, cs in self.candles.items()},
            funding=[(t, r) for t, r in self.funding if t < end_ms],
            reports=dict(self.reports),
            funding_provenance=self.funding_provenance,
            decision_start_ms=self.decision_start_ms,
        )


# ── Pagination ───────────────────────────────────────────────────────────────

def fetch_paginated(symbol: str, timeframe: str, days: float,
                    end_ms: Optional[int] = None, fetch=None,
                    max_requests: int = 500, throttle_s: float = 0.0
                    ) -> tuple:
    """Récupère `days` jours d'un TF en reculant fenêtre par fenêtre.

    Chaque requête demande au plus `MAX_CANDLES_PER_REQUEST - marge` bougies :
    coller exactement au plafond expose à la troncature silencieuse quand
    l'exchange renvoie une bougie de plus que prévu (bougie en cours incluse).

    `fetch` est injectable pour les tests — sinon `simplebot.data.fetch_ohlcv`,
    qui porte déjà le cache disque partagé et les retries anti-429 du repo.
    """
    if fetch is None:
        from simplebot.data import fetch_ohlcv as fetch

    step = INTERVAL_MS[timeframe]
    now_ms = int(time.time() * 1000) if end_ms is None else int(end_ms)
    target_start = now_ms - int(days * 86_400_000)

    per_request = MAX_CANDLES_PER_REQUEST - 50
    window_days = (per_request * step) / 86_400_000.0

    collected: List[Candle] = []
    cursor = now_ms
    requests = 0
    while cursor > target_start and requests < max_requests:
        requests += 1
        batch = fetch(symbol, timeframe, window_days, end_ms=cursor)
        if not batch:
            logger.warning("%s/%s: fenêtre vide à %s — arrêt de la pagination",
                           symbol, timeframe, cursor)
            break
        collected.extend(batch)
        oldest = min(int(c["ts"]) for c in batch)
        if oldest >= cursor:
            # Aucune progression : l'exchange ne remonte pas plus loin.
            logger.info("%s/%s: pas d'historique avant %s", symbol, timeframe, oldest)
            break
        cursor = oldest
        if throttle_s:
            time.sleep(throttle_s)

    if requests >= max_requests:
        logger.warning("%s/%s: plafond de %d requêtes atteint — historique tronqué",
                       symbol, timeframe, max_requests)

    series = [c for c in sort_dedup(collected) if int(c["ts"]) >= target_start]
    # La dernière bougie renvoyée par l'API est EN COURS (§3) : on la coupe ici
    # plutôt que de compter sur chaque appelant pour y penser.
    series = [c for c in series if int(c["ts"]) + step <= now_ms]
    return series, requests


def load_history(symbol: str, days: float, timeframes: Sequence[str] = ("1d", "1h", "15m"),
                 end_ms: Optional[int] = None, fetch=None, cache: bool = True,
                 throttle_s: float = 0.0, source: str = "native") -> History:
    """Charge l'historique complet, avec cache disque et rapport d'intégrité.

    `source="native"` interroge Hyperliquid. **Attention** : son endpoint ne
    rend que les 5000 dernières bougies par intervalle, soit 52 jours en 15m —
    largement insuffisant pour les 3 ans du §9.2. `source="binance"` bascule
    sur le proxy profond ; voir `confluence/sources.py` pour ce que ce choix
    vaut et ce qu'il ne vaut pas.

    `1m` n'est PAS dans les timeframes par défaut : 3 ans de 1m font ~1,6
    million de bougies, pour une couche qui ne porte aucune décision (§4.4).
    Le backtest modélise le fill maker au 15m.
    """
    if fetch is None and source != "native":
        from confluence.sources import resolve_source

        fetch = resolve_source(source)

    hist = History(symbol=symbol)
    for tf in timeframes:
        cached = _cache_read(symbol, tf, days, end_ms, source) if cache else None
        if cached is not None:
            series, requests = cached, 0
        elif hasattr(fetch, "fetch_range"):
            # Source à pagination avant (proxy profond) : une seule plage, elle
            # sait remonter le temps toute seule.
            end = int(end_ms) if end_ms is not None else int(time.time() * 1000)
            series = fetch.fetch_range(symbol, tf, end - int(days * 86_400_000), end)
            requests = 1
            if cache and series:
                _cache_write(symbol, tf, days, end_ms, series, source)
        else:
            series, requests = fetch_paginated(symbol, tf, days, end_ms=end_ms,
                                               fetch=fetch, throttle_s=throttle_s)
            if cache and series:
                _cache_write(symbol, tf, days, end_ms, series, source)

        report = SeriesReport(
            timeframe=tf,
            bars=len(series),
            first_ts=int(series[0]["ts"]) if series else 0,
            last_ts=int(series[-1]["ts"]) if series else 0,
            gaps=find_gaps(series, tf),
            anomaly_count=len(anomalies(series)),
            requests=requests,
        )
        hist.candles[tf] = series
        hist.reports[tf] = report
        logger.info("%s %s", symbol, report.summary())
        if report.missing_bars:
            logger.warning("%s/%s: %d barres manquantes — NON comblées (§3)",
                           symbol, tf, report.missing_bars)
    return hist


def load_funding(symbol: str, days: float, end_ms: Optional[int] = None,
                 fetch=None, source: str = "hyperliquid") -> tuple:
    """Historique de funding, et sa provenance.

    Contrairement à `candleSnapshot`, l'endpoint `fundingHistory` d'Hyperliquid
    HONORE `startTime` et pagine correctement : le funding natif remonte, lui,
    jusqu'au lancement de la plateforme (2023). Un backtest peut donc mêler des
    prix proxy et un funding natif — ce qui est strictement meilleur, à
    condition de le dire.

    Rend `(points, provenance)`. La provenance n'est pas décorative : elle
    porte le drapeau `validated=False` que tous les rapports doivent afficher.
    """
    cached = _funding_cache_read(symbol, days, end_ms, source) if fetch is None else None
    if cached is not None:
        points = cached
    else:
        if source == "binance":
            points = _fetch_binance_funding(symbol, days, end_ms)
        elif fetch is not None:
            points = fetch(symbol, days)
        else:
            points = _fetch_hl_funding(symbol, days, end_ms)
        if fetch is None and points:
            _funding_cache_write(symbol, days, end_ms, source, points)
    points = sorted(set(points))
    if end_ms is not None:
        points = [(t, r) for t, r in points if t <= end_ms]

    cadence = None
    if len(points) > 2:
        gaps = sorted(b - a for (a, _), (b, _) in zip(points, points[1:]))
        cadence = gaps[len(gaps) // 2] / 3_600_000.0      # médiane, robuste aux trous
    provenance = FundingProvenance(
        source=source if points else "none",
        points=len(points),
        first_ms=points[0][0] if points else 0,
        last_ms=points[-1][0] if points else 0,
        settlement_hours=cadence,
        validated=False,        # jamais True avant mesure en paper trading
    )
    logger.info("%s %s", symbol, provenance.summary())
    return points, provenance


def _funding_cache_path(symbol: str, days: float, end_ms: Optional[int],
                        source: str) -> Path:
    tag = "now" if end_ms is None else str(int(end_ms))
    return CACHE_DIR / f"funding__{symbol}__{int(days)}d__{tag}__{source}.json"


def _funding_cache_read(symbol: str, days: float, end_ms: Optional[int],
                        source: str) -> Optional[List[tuple]]:
    """Cache du funding : ~28 000 points et 70 requêtes, c'est le poste qui
    déclenche le 429. Le refetch à chaque run est à la fois lent et risqué."""
    path = _funding_cache_path(symbol, days, end_ms, source)
    if not path.exists():
        return None
    if end_ms is None and time.time() * 1000 - path.stat().st_mtime * 1000 > 3_600_000:
        return None                              # fenêtre « jusqu'à maintenant » : 1 h
    try:
        return [(int(t), float(r)) for t, r in json.loads(path.read_text(encoding="utf-8"))]
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _funding_cache_write(symbol: str, days: float, end_ms: Optional[int],
                         source: str, points: List[tuple]) -> None:
    try:
        path = _funding_cache_path(symbol, days, end_ms, source)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(points), encoding="utf-8")
    except OSError as exc:
        logger.warning("cache funding non écrit (%s)", exc)


def _fetch_hl_funding(symbol: str, days: float,
                      end_ms: Optional[int] = None) -> List[tuple]:
    """Funding horaire Hyperliquid, paginé sans plafond prématuré.

    `superbot.data.fetch_funding_history` fait le même travail mais s'arrête à
    40 pages, soit 20 000 points — 833 jours. Ce module en demande 1500. On ne
    relève pas son plafond : SuperBot tourne en production dessus, et changer
    la profondeur qu'il récupère changerait son comportement pour un besoin qui
    n'est pas le sien.

    Le piège évité ici est silencieux : sans funding sur la première moitié de
    la période, la couche 1h oppose son veto à TOUT (§4.2), et le backtest
    rendrait un résultat calculé sur 833 jours en affichant 1500.
    """
    import time as _time

    import requests

    from hl_rate_limit import throttle_before_hl_request

    end = int(end_ms) if end_ms is not None else int(_time.time() * 1000)
    cursor = end - int(days * 86_400_000)
    out: List[tuple] = []
    guard = 0
    max_pages = int(days * 24 / 500) + 20        # 500 points/page, marge confortable

    while cursor < end and guard < max_pages:
        guard += 1
        batch = None
        # Une pagination de ~70 pages d'affilée déclenche le 429 d'Hyperliquid.
        # Sans backoff, la fonction remontait la première erreur, l'appelant la
        # journalisait et CONTINUAIT avec un funding vide — un backtest entier
        # tournait alors avec la couche 1h en veto permanent.
        for attempt in range(1, 6):
            try:
                throttle_before_hl_request()
                resp = requests.post(
                    "https://api.hyperliquid.xyz/info",
                    json={"type": "fundingHistory", "coin": symbol,
                          "startTime": int(cursor)},
                    timeout=20)
                resp.raise_for_status()
                batch = resp.json() or []
                break
            except requests.exceptions.RequestException as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and status != 429 and not 500 <= status < 600:
                    raise
                delay = 2.0 * (2 ** (attempt - 1))
                logger.warning("funding %s: %s — retry %d/5 dans %.0fs",
                               symbol, status or type(exc).__name__, attempt, delay)
                _time.sleep(delay)
        if batch is None:
            raise RuntimeError(
                f"funding {symbol}: 5 tentatives échouées — mieux vaut échouer "
                f"que rendre un historique de funding troué")
        if not batch:
            break
        out.extend((int(x["time"]), float(x["fundingRate"])) for x in batch)
        last = int(batch[-1]["time"])
        if last <= cursor:
            break
        cursor = last + 1

    if guard >= max_pages:
        logger.warning("%s: plafond de pagination funding atteint — historique tronqué",
                       symbol)
    return [(t, r) for t, r in out if t <= end]


def _fetch_binance_funding(symbol: str, days: float,
                           end_ms: Optional[int] = None) -> List[tuple]:
    """Funding Binance USD-M, réglé toutes les 8 h (pagination par startTime).

    Utilisé seulement quand aucun funding natif n'existe pour la période — par
    exemple avant le lancement d'Hyperliquid.
    """
    import time as _time

    import requests

    from confluence.sources import SYMBOL_MAP

    pair = SYMBOL_MAP.get(symbol.upper())
    if pair is None:
        raise ValueError(f"aucune correspondance Binance pour {symbol!r}")
    end = int(end_ms) if end_ms is not None else int(_time.time() * 1000)
    cursor = end - int(days * 86_400_000)
    out: List[tuple] = []
    guard = 0
    while cursor < end and guard < 400:
        guard += 1
        resp = requests.get("https://fapi.binance.com/fapi/v1/fundingRate",
                            params={"symbol": pair, "startTime": int(cursor),
                                    "limit": 1000}, timeout=20)
        resp.raise_for_status()
        batch = resp.json() or []
        if not batch:
            break
        out.extend((int(x["fundingTime"]), float(x["fundingRate"])) for x in batch)
        last = int(batch[-1]["fundingTime"])
        if last <= cursor:
            break
        cursor = last + 1
    return out


# ── Cache disque ─────────────────────────────────────────────────────────────

def _cache_path(symbol: str, timeframe: str, days: float, end_ms: Optional[int],
                source: str) -> Path:
    tag = "now" if end_ms is None else str(int(end_ms))
    # La source fait partie de la clé : mélanger des bougies Hyperliquid et
    # Binance dans un même fichier produirait une série chimère, avec un saut
    # de base à la jointure que rien ne signalerait.
    return CACHE_DIR / f"{symbol}__{timeframe}__{int(days)}d__{tag}__{source}.json"


def _cache_read(symbol: str, timeframe: str, days: float,
                end_ms: Optional[int], source: str = "native") -> Optional[List[Candle]]:
    path = _cache_path(symbol, timeframe, days, end_ms, source)
    if not path.exists():
        return None
    # Un cache « jusqu'à maintenant » périme au bout d'une bougie ; un cache
    # borné dans le passé est immuable.
    if end_ms is None:
        age_ms = time.time() * 1000 - path.stat().st_mtime * 1000
        if age_ms > INTERVAL_MS[timeframe]:
            return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [dict(c) for c in data]
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("cache %s illisible (%s) — rechargement", path.name, exc)
        return None


def _cache_write(symbol: str, timeframe: str, days: float, end_ms: Optional[int],
                 series: List[Candle], source: str = "native") -> None:
    path = _cache_path(symbol, timeframe, days, end_ms, source)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(series), encoding="utf-8")
    except OSError as exc:
        logger.warning("cache non écrit (%s)", exc)


__all__ = ["CACHE_DIR", "History", "SeriesReport", "fetch_paginated",
           "load_funding", "load_history"]

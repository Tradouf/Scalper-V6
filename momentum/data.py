"""
Chargement multi-actifs — SPEC §9.1 amendé.

Perps USD-M Binance : univers, prix ET funding. C'est l'instrument tradable, et
le funding vient nativement par actif.

Le point délicat est le **calendrier de listing**. `exchangeInfo` donne une
`onboardDate` par symbole : on ne charge que ce qui existait, et l'absence de
données avant cette date est un fait du marché, pas un trou à combler.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("sdm.momentum.data")

CACHE_DIR = Path(__file__).resolve().parent / "state" / "history"
FAPI = "https://fapi.binance.com"
DAY_MS = 86_400_000


@dataclass
class MultiAssetHistory:
    daily: Dict[str, List[dict]] = field(default_factory=dict)
    hourly: Dict[str, List[dict]] = field(default_factory=dict)
    funding: Dict[str, List[tuple]] = field(default_factory=dict)
    onboard: Dict[str, int] = field(default_factory=dict)

    @property
    def symbols(self) -> List[str]:
        return sorted(self.daily)

    def summary(self) -> Dict[str, object]:
        spans = {s: (c[-1]["ts"] - c[0]["ts"]) / DAY_MS for s, c in self.daily.items() if c}
        return {
            "symbols": len(self.daily),
            "daily_bars": sum(len(c) for c in self.daily.values()),
            "hourly_bars": sum(len(c) for c in self.hourly.values()),
            "funding_points": sum(len(f) for f in self.funding.values()),
            "median_span_days": round(sorted(spans.values())[len(spans) // 2], 0)
            if spans else 0,
        }


def list_perps(min_onboard_ms: Optional[int] = None) -> Dict[str, int]:
    """Perps USD-M en USDT, avec leur date de listing.

    `min_onboard_ms` ne garde que ceux listés AVANT cette date — c'est ainsi
    qu'on évite de charger des actifs qui n'existaient pas sur la fenêtre testée.
    """
    import requests

    info = requests.get(f"{FAPI}/fapi/v1/exchangeInfo", timeout=25).json()
    out = {}
    for s in info.get("symbols", []):
        if (s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"
                and s.get("onboardDate")):
            onboard = int(s["onboardDate"])
            if min_onboard_ms is None or onboard <= min_onboard_ms:
                out[s["symbol"]] = onboard
    return out


def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int,
                 throttle_s: float = 0.12) -> List[dict]:
    """Klines paginées par `startTime` croissant."""
    import requests

    step = {"1d": DAY_MS, "1h": 3_600_000}[interval]
    out: List[dict] = []
    cursor = start_ms
    guard, max_pages = 0, int((end_ms - start_ms) / (step * 1500)) + 12

    while cursor < end_ms and guard < max_pages:
        guard += 1
        for attempt in range(1, 5):
            try:
                r = requests.get(f"{FAPI}/fapi/v1/klines", params={
                    "symbol": symbol, "interval": interval,
                    "startTime": int(cursor), "limit": 1500}, timeout=25)
                r.raise_for_status()
                batch = r.json()
                break
            except requests.exceptions.RequestException as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and status not in (429, 418) and status < 500:
                    raise
                time.sleep(2.0 * (2 ** (attempt - 1)))
        else:
            raise RuntimeError(f"klines {symbol}/{interval}: 4 tentatives échouées")

        if not batch:
            break
        out.extend({"ts": int(k[0]), "open": float(k[1]), "high": float(k[2]),
                    "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])}
                   for k in batch)
        last = int(batch[-1][0])
        if last < cursor:
            break
        cursor = last + step
        if throttle_s:
            time.sleep(throttle_s)

    now = int(time.time() * 1000)
    return [c for c in out if c["ts"] < end_ms and c["ts"] + step <= now]


def fetch_funding(symbol: str, start_ms: int, end_ms: int) -> List[tuple]:
    """Funding par actif, réglé toutes les 8 h sur Binance."""
    import requests

    out: List[tuple] = []
    cursor, guard = start_ms, 0
    while cursor < end_ms and guard < 400:
        guard += 1
        r = requests.get(f"{FAPI}/fapi/v1/fundingRate", params={
            "symbol": symbol, "startTime": int(cursor), "limit": 1000}, timeout=25)
        r.raise_for_status()
        batch = r.json() or []
        if not batch:
            break
        out.extend((int(x["fundingTime"]), float(x["fundingRate"])) for x in batch)
        last = int(batch[-1]["fundingTime"])
        if last <= cursor:
            break
        cursor = last + 1
    return [(t, r) for t, r in out if t <= end_ms]


def load_history(days: int, end_ms: Optional[int], basket_pool: int = 25,
                 cache: bool = True, throttle_s: float = 0.12) -> MultiAssetHistory:
    """Charge un pool d'actifs candidats sur la fenêtre.

    `basket_pool` dépasse volontairement `basket_size` : la sélection §1 doit
    pouvoir CHOISIR à chaque date. Charger exactement 10 actifs reviendrait à
    figer l'univers, c'est-à-dire à réintroduire le biais du survivant par la
    porte du chargement.
    """
    end = int(end_ms) if end_ms else int(time.time() * 1000)
    start = end - days * DAY_MS

    key = f"{days}d__{end_ms or 'now'}__{basket_pool}"
    path = CACHE_DIR / f"multi__{key}.json"
    if cache and path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            hist = MultiAssetHistory(
                daily={k: v for k, v in raw["daily"].items()},
                hourly={k: v for k, v in raw["hourly"].items()},
                funding={k: [tuple(x) for x in v] for k, v in raw["funding"].items()},
                onboard={k: int(v) for k, v in raw["onboard"].items()})
            logger.info("cache: %s", hist.summary())
            return hist
        except (OSError, json.JSONDecodeError, KeyError):
            logger.warning("cache %s illisible — rechargement", path.name)

    # Seuls les perps listés AVANT le début de la fenêtre sont candidats : un
    # actif apparu en cours de route entrera par `select_universe`, à sa date.
    perps = list_perps(min_onboard_ms=start + 30 * DAY_MS)
    ranked = sorted(perps.items(), key=lambda kv: kv[1])[:basket_pool]
    logger.info("%d perps candidats listés avant %s", len(ranked), start)

    hist = MultiAssetHistory(onboard=dict(ranked))
    for symbol, onboard in ranked:
        s0 = max(start, onboard)
        hist.daily[symbol] = fetch_klines(symbol, "1d", s0, end, throttle_s)
        hist.hourly[symbol] = fetch_klines(symbol, "1h", s0, end, throttle_s)
        hist.funding[symbol] = fetch_funding(symbol, s0, end)
        logger.info("  %-12s %5d j, %6d h, %5d funding", symbol,
                    len(hist.daily[symbol]), len(hist.hourly[symbol]),
                    len(hist.funding[symbol]))

    if cache:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "daily": hist.daily, "hourly": hist.hourly,
                "funding": {k: [list(x) for x in v] for k, v in hist.funding.items()},
                "onboard": hist.onboard}), encoding="utf-8")
        except OSError as exc:
            logger.warning("cache non écrit (%s)", exc)

    logger.info("chargé: %s", hist.summary())
    return hist


__all__ = ["CACHE_DIR", "MultiAssetHistory", "fetch_funding", "fetch_klines",
           "list_perps", "load_history"]


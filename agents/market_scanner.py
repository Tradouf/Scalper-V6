"""
MarketScannerAgent — Salle des Marchés
Scanne les meilleures paires Hyperliquid et les score selon :
- Volume 24h, Spread, Funding, Momentum, Whale Alert

OPTIMISATION : on pré-filtre les 30 paires par volume AVANT de
faire les appels ticker individuels → scan 10x plus rapide.

v2 : les actifs de la whitelist dynamique sont TOUJOURS inclus
     dans le scan, peu importe leur volume.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import List

import requests

logger = logging.getLogger("sdm.market_scanner")

WHALE_ALERT_API_KEY = "3gD6CElj0tdMGqi3XLCMUdl5qhvx1Z43"
WHALE_ALERT_URL     = "https://api.whale-alert.io/v1/transactions"
WHALE_MIN_VALUE_USD = 1_000_000
WHALE_LOOKBACK_SEC  = 3600
PRE_FILTER_TOP      = 30


@dataclass
class MarketScore:
    symbol:        str
    score:         float
    price:         float
    volume_24h:    float
    spread_bps:    float
    funding_rate:  float
    momentum_pct:  float
    open_interest: float
    whale_signal:  float
    whale_details: List[dict] = field(default_factory=list)
    max_leverage:  int = 20
    closes:        List[float] = field(default_factory=list)


class MarketScannerAgent:

    def __init__(self, exchange_client, whale_api_key: str = WHALE_ALERT_API_KEY):
        self._client               = exchange_client
        self._whale_key            = whale_api_key
        self._whale_cache: List[dict] = []
        self._whale_cache_time: float = 0
        self._WHALE_CACHE_TTL      = 300

    def _get_whitelist(self) -> set:
        """Récupère la whitelist dynamique depuis le supervisor."""
        try:
            import supervisor.trading_supervisor as ts
            return set(ts.TREND_WHITELIST)
        except Exception:
            return set()

    def scan(self, top_n: int = 15) -> List[MarketScore]:
        logger.info("Scan des marchés en cours...")

        whale_txs  = self._fetch_whale_alerts()
        whitelist  = self._get_whitelist()

        # Récupération une seule fois de tous les mid-prices + meta
        all_markets = self._client.get_markets()

        # Pré-filtre : garder les 30 paires avec le plus gros volume 24h
        all_markets_sorted = sorted(
            all_markets,
            key=lambda m: float(m.get("day_notional_volume", 0)),
            reverse=True,
        )[:PRE_FILTER_TOP]

        # Forcer l'inclusion des actifs de la whitelist
        symbols_in_prefilter = {m.get("symbol", "") for m in all_markets_sorted}
        for m in all_markets:
            sym = m.get("symbol", "")
            if sym in whitelist and sym not in symbols_in_prefilter:
                all_markets_sorted.append(m)
                symbols_in_prefilter.add(sym)
                logger.debug("Whitelist force-include: %s", sym)

        scores = []
        for m in all_markets_sorted:
            symbol = m.get("symbol", "")
            if not symbol:
                continue
            try:
                ms = self._score_market(m, whale_txs, whitelist)
                scores.append(ms)
            except Exception as e:
                logger.debug("Erreur scoring %s: %s", symbol, e)

        scores.sort(key=lambda x: x.score, reverse=True)
        top = scores[:top_n]

        logger.info(
            "Scan terminé — %d marchés analysés, top %d: %s",
            len(scores), top_n,
            [f"{s.symbol}({s.score:.1f})" for s in top],
        )
        return top

    def _score_market(self, market: dict, whale_txs: List[dict], whitelist: set = None) -> MarketScore:
        symbol     = market["symbol"]
        volume_24h = float(market.get("day_notional_volume", 0))
        oi         = float(market.get("open_interest", 0))
        max_lev    = int(market.get("max_leverage", 20))

        ticker     = self._client._client.get_ticker(symbol)
        price      = float(ticker.get("price", 0))
        spread_bps = float(ticker.get("spread_bps", 999))
        funding    = float(ticker.get("funding_rate", 0))
        prev_price = float(ticker.get("prev_day_price", 0))
        momentum   = ((price - prev_price) / prev_price * 100) if prev_price > 0 else 0.0

        whale_signal, whale_details = self._whale_signal_for(symbol, whale_txs)

        score = 0.0

        # 1) Volume (40 pts)
        if volume_24h > 0:
            score += min(40.0, math.log10(volume_24h / 1_000_000 + 1) * 20)

        # 2) Spread (20 pts)
        if spread_bps < 2:
            score += 20
        elif spread_bps < 5:
            score += 15
        elif spread_bps < 10:
            score += 10
        elif spread_bps < 20:
            score += 5

        # 3) Momentum (20 pts)
        abs_mom = abs(momentum)
        if abs_mom > 5:
            score += 20
        elif abs_mom > 3:
            score += 15
        elif abs_mom > 1:
            score += 10
        elif abs_mom > 0.5:
            score += 5

        # 4) Funding extrême (10 pts)
        abs_funding = abs(funding)
        if abs_funding > 0.001:
            score += 10
        elif abs_funding > 0.0005:
            score += 6
        elif abs_funding > 0.0002:
            score += 3

        # 5) Whale signal (10 pts)
        if abs(whale_signal) > 5_000_000:
            score += 10
        elif abs(whale_signal) > 1_000_000:
            score += 6

        # 6) Bonus whitelist (5 pts) — garantit leur présence dans le top
        if whitelist and symbol in whitelist:
            score += 5

        # Récupérer les closes 1h pour S/R
        try:
            candles = self._client.get_candles(symbol, interval="1h", limit=50)
            closes_list = [c["close"] for c in candles] if candles else []
        except Exception:
            closes_list = []

        return MarketScore(
            symbol        = symbol,
            score         = round(score, 2),
            price         = price,
            volume_24h    = volume_24h,
            spread_bps    = spread_bps,
            funding_rate  = funding,
            momentum_pct  = round(momentum, 3),
            open_interest = oi,
            whale_signal  = whale_signal,
            closes        = closes_list,
            whale_details = whale_details,
            max_leverage  = max_lev,
        )

    def _fetch_whale_alerts(self) -> List[dict]:
        now = time.monotonic()
        if self._whale_cache and (now - self._whale_cache_time) < self._WHALE_CACHE_TTL:
            return self._whale_cache

        if not self._whale_key or self._whale_key in ("TA_CLE_WHALE_ALERT_ICI", "DISABLED"):
            logger.debug("Whale Alert: clé non configurée, signal ignoré.")
            return []

        try:
            start  = int(time.time()) - WHALE_LOOKBACK_SEC
            params = {
                "api_key":   self._whale_key,
                "min_value": WHALE_MIN_VALUE_USD,
                "start":     start,
                "limit":     100,
            }
            resp = requests.get(WHALE_ALERT_URL, params=params, timeout=10)
            resp.raise_for_status()
            txs  = resp.json().get("transactions", [])
            self._whale_cache      = txs
            self._whale_cache_time = now
            logger.info("Whale Alert: %d transactions chargées", len(txs))
            return txs
        except Exception as e:
            logger.warning("Whale Alert API erreur: %s", e)
            return []

    def _whale_signal_for(self, symbol: str, txs: List[dict]) -> tuple[float, List[dict]]:
        coin     = symbol.upper()
        relevant = []
        signal   = 0.0

        for tx in txs:
            if tx.get("symbol", "").upper() != coin:
                continue
            amount_usd = float(tx.get("amount_usd", 0))
            from_owner = tx.get("from", {}).get("owner_type", "")
            to_owner   = tx.get("to",   {}).get("owner_type", "")

            if from_owner == "exchange" and to_owner == "unknown":
                signal += amount_usd
            elif from_owner == "unknown" and to_owner == "exchange":
                signal -= amount_usd

            relevant.append({
                "amount_usd": amount_usd,
                "from":       from_owner,
                "to":         to_owner,
                "hash":       tx.get("hash", ""),
            })

        return signal, relevant

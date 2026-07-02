"""
Récupération OHLCV Hyperliquid (endpoint public /info, pas de wallet requis).
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

import requests

logger = logging.getLogger("sdm.simplebot.data")

HL_INFO_URL = "https://api.hyperliquid.xyz/info"


def fetch_ohlcv(
    symbol: str,
    interval: str,
    days: float,
    end_ms: Optional[int] = None,
    timeout: float = 10.0,
) -> List[dict]:
    """
    Retourne les bougies triées par ts croissant :
    [{"ts", "open", "high", "low", "close", "volume"}, ...]
    La dernière bougie peut être EN COURS (non clôturée) — à filtrer côté appelant.
    """
    if end_ms is None:
        end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(days * 24 * 3600 * 1000)

    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
        },
    }

    try:
        resp = requests.post(
            HL_INFO_URL,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        logger.warning("fetch_ohlcv %s/%s: %r", symbol, interval, e)
        return []

    candles = [
        {
            "ts": int(c["t"]),
            "open": float(c["o"]),
            "high": float(c["h"]),
            "low": float(c["l"]),
            "close": float(c["c"]),
            "volume": float(c["v"]),
        }
        for c in raw
    ]
    candles.sort(key=lambda c: c["ts"])
    return candles


def closed_candles(candles: List[dict], interval_ms: int, now_ms: Optional[int] = None) -> List[dict]:
    """Ne garde que les bougies dont la fenêtre est terminée."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return [c for c in candles if c["ts"] + interval_ms <= now_ms]

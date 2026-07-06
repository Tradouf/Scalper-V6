"""
Récupération des bougies 1 m clôturées — réutilise la couche data de SimpleBot
(endpoint public /info, retries anti-429 inclus).
"""

from __future__ import annotations

from typing import List

from simplebot.data import closed_candles, fetch_ohlcv

_MINUTE_MS = 60_000


def fetch_recent_1m(symbol: str, hours: float) -> List[dict]:
    """Bougies 1 m CLÔTURÉES des `hours` dernières heures, triées par ts."""
    candles = fetch_ohlcv(symbol, "1m", days=hours / 24.0)
    return closed_candles(candles, _MINUTE_MS)

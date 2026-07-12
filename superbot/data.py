"""
Accès données SuperBot — ré-export direct des briques simplebot (SPEC §12 :
« import direct, pas de copie aveugle »). Une seule implémentation du fetch
OHLCV/funding/univers pour tout le repo, avec retries anti-429 déjà durcis.
"""

from __future__ import annotations

from simplebot.data import (  # noqa: F401 — API publique SuperBot
    closed_candles,
    fetch_funding_rates,
    fetch_ledger_updates,
    fetch_ohlcv,
    fetch_perp_universe,
    net_transfer_flow,
)

from superbot import config


def fetch_days_for(timeframe: str, wanted_days: float) -> float:
    """Jours effectivement récupérables sur un TF : l'endpoint candleSnapshot
    plafonne à ~5000 bougies/requête — au-delà il TRONQUE SILENCIEUSEMENT.
    (60 j demandés en 15m = 5760 bougies → seuls ~52 j reviendraient.)"""
    cap = config.MAX_FETCH_DAYS.get(timeframe)
    return min(wanted_days, cap) if cap else wanted_days


def fetch_closed(symbol: str, timeframe: str, days: float, fetch=None) -> list:
    """OHLCV clôturés d'un symbole sur un TF, borné au cap API du TF."""
    fetch = fetch or fetch_ohlcv
    eff_days = fetch_days_for(timeframe, days)
    candles = fetch(symbol, timeframe, eff_days)
    return closed_candles(candles, config.INTERVAL_MS[timeframe])

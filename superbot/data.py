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


def fetch_funding_history(symbol: str, days: float, timeout: float = 10.0) -> list:
    """Historique de funding HORAIRE [(ts_ms, taux), ...] — endpoint public
    fundingHistory, paginé (500 points max/requête → ~21 j ; on avance par
    startTime jusqu'à couvrir `days`)."""
    import time as _time

    import requests

    from simplebot.data import HL_INFO_URL

    now_ms = int(_time.time() * 1000)
    start = now_ms - int(days * 86_400_000)
    out: list = []
    guard = 0
    while start < now_ms - 3_600_000 and guard < 40:
        guard += 1
        from hl_rate_limit import throttle_before_hl_request

        throttle_before_hl_request()
        resp = requests.post(
            HL_INFO_URL,
            headers={"Content-Type": "application/json"},
            json={"type": "fundingHistory", "coin": symbol, "startTime": int(start)},
            timeout=timeout,
        )
        resp.raise_for_status()
        batch = resp.json() or []
        if not batch:
            break
        out.extend((int(x["time"]), float(x["fundingRate"])) for x in batch)
        start = out[-1][0] + 1
        if len(batch) < 400:      # dernière page
            break
        _time.sleep(config.FETCH_THROTTLE_SEC)
    return out


def align_funding(candles: list, funding_points: list) -> list:
    """Taux de funding horaire COURANT à chaque bougie (dernier taux connu au
    ts de la bougie — strictement causal). 0.0 avant le premier point."""
    import bisect

    times = [t for t, _ in funding_points]
    rates = [r for _, r in funding_points]
    out = []
    for c in candles:
        j = bisect.bisect_right(times, c["ts"]) - 1
        out.append(rates[j] if j >= 0 else 0.0)
    return out


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

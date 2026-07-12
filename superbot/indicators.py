"""
Indicateurs SuperBot pour les features HMM — pur Python (zéro pandas),
complémentaires à simplebot.strategy (ema/rsi/atr importés directement).

Vecteurs d'observation (SPEC §4) :
  marché  (4D) : [log_return_1bar, atr_pct, adx_norm, funding_hourly]
  symbole (5D) : [log_return_1bar, atr_pct, adx_norm, rsi_distance, volume_ratio]
"""

from __future__ import annotations

import math
from typing import List, Optional

from simplebot.strategy import atr, rsi


def adx(candles: List[dict], length: int = 14) -> List[float]:
    """ADX de Wilder (0-100). Convergence après ~2×length bougies."""
    n = len(candles)
    out = [0.0] * n
    if n < 2:
        return out
    alpha = 1.0 / length
    sm_tr = sm_pdm = sm_ndm = 0.0
    adx_val: Optional[float] = None
    prev_dx: List[float] = []
    for i in range(1, n):
        c, p = candles[i], candles[i - 1]
        up = c["high"] - p["high"]
        dn = p["low"] - c["low"]
        pdm = up if (up > dn and up > 0) else 0.0
        ndm = dn if (dn > up and dn > 0) else 0.0
        tr = max(c["high"] - c["low"],
                 abs(c["high"] - p["close"]),
                 abs(c["low"] - p["close"]))
        if i == 1:
            sm_tr, sm_pdm, sm_ndm = tr, pdm, ndm
        else:
            sm_tr = sm_tr * (1 - alpha) + tr * alpha
            sm_pdm = sm_pdm * (1 - alpha) + pdm * alpha
            sm_ndm = sm_ndm * (1 - alpha) + ndm * alpha
        if sm_tr <= 0:
            dx = 0.0
        else:
            pdi = 100.0 * sm_pdm / sm_tr
            ndi = 100.0 * sm_ndm / sm_tr
            dx = 100.0 * abs(pdi - ndi) / (pdi + ndi) if (pdi + ndi) > 0 else 0.0
        if adx_val is None:
            prev_dx.append(dx)
            if len(prev_dx) >= length:
                adx_val = sum(prev_dx) / len(prev_dx)
        else:
            adx_val = adx_val * (1 - alpha) + dx * alpha
        out[i] = adx_val if adx_val is not None else 0.0
    return out


def sma(values: List[float], length: int) -> List[float]:
    out = [0.0] * len(values)
    acc = 0.0
    for i, v in enumerate(values):
        acc += v
        if i >= length:
            acc -= values[i - length]
        out[i] = acc / min(i + 1, length)
    return out


def _log_returns(candles: List[dict]) -> List[float]:
    out = [0.0] * len(candles)
    for i in range(1, len(candles)):
        prev = candles[i - 1]["close"]
        cur = candles[i]["close"]
        out[i] = math.log(cur / prev) if (prev > 0 and cur > 0) else 0.0
    return out


def build_market_features(candles: List[dict],
                          funding: Optional[List[float]] = None) -> List[List[float]]:
    """Matrice (n, 4) pour le HMM marché. `funding` = taux horaire aligné par
    bougie (0.0 si indisponible — la feature reste présente, variance ~0)."""
    n = len(candles)
    rets = _log_returns(candles)
    atr_v = atr(candles, 14)
    adx_v = adx(candles, 14)
    fund = funding if funding is not None else [0.0] * n
    rows = []
    for i in range(n):
        close = candles[i]["close"] or 1.0
        rows.append([
            rets[i],
            atr_v[i] / close,
            adx_v[i] / 100.0,
            fund[i] if i < len(fund) else 0.0,
        ])
    return rows


def build_symbol_features(candles: List[dict]) -> List[List[float]]:
    """Matrice (n, 5) pour le HMM par symbole."""
    n = len(candles)
    rets = _log_returns(candles)
    atr_v = atr(candles, 14)
    adx_v = adx(candles, 14)
    rsi_v = rsi([c["close"] for c in candles], 14)
    vols = [c.get("volume", 0.0) for c in candles]
    vol_sma = sma(vols, 20)
    rows = []
    for i in range(n):
        close = candles[i]["close"] or 1.0
        rows.append([
            rets[i],
            atr_v[i] / close,
            adx_v[i] / 100.0,
            (rsi_v[i] - 50.0) / 50.0,
            (vols[i] / vol_sma[i]) if vol_sma[i] > 0 else 1.0,
        ])
    return rows

"""
Indicateurs techniques en Python pur (pas de numpy/pandas dans le repo).

Convention : toutes les fonctions retournent une liste alignée sur l'entrée,
avec None pendant la période de chauffe.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

Series = List[Optional[float]]


def sma(values: List[float], n: int) -> Series:
    out: Series = [None] * len(values)
    if n <= 0:
        return out
    acc = 0.0
    for i, v in enumerate(values):
        acc += v
        if i >= n:
            acc -= values[i - n]
        if i >= n - 1:
            out[i] = acc / n
    return out


def ema(values: List[float], n: int) -> Series:
    out: Series = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    seed = sum(values[:n]) / n
    out[n - 1] = seed
    k = 2.0 / (n + 1)
    prev = seed
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes: List[float], n: int) -> Series:
    """RSI de Wilder."""
    out: Series = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_g, avg_l = gains / n, losses / n
    out[n] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1 + avg_g / avg_l)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = max(d, 0.0)
        l = max(-d, 0.0)
        avg_g = (avg_g * (n - 1) + g) / n
        avg_l = (avg_l * (n - 1) + l) / n
        out[i] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1 + avg_g / avg_l)
    return out


def stochastic(candles: List[dict], k_len: int, d_len: int) -> Tuple[Series, Series]:
    """%K brut sur k_len bougies, %D = SMA(%K, d_len)."""
    n = len(candles)
    k: Series = [None] * n
    for i in range(k_len - 1, n):
        window = candles[i - k_len + 1 : i + 1]
        hh = max(c["high"] for c in window)
        ll = min(c["low"] for c in window)
        rng = hh - ll
        k[i] = 50.0 if rng == 0 else (candles[i]["close"] - ll) / rng * 100.0
    d: Series = [None] * n
    acc: List[float] = []
    for i in range(n):
        if k[i] is None:
            continue
        acc.append(k[i])
        if len(acc) >= d_len:
            d[i] = sum(acc[-d_len:]) / d_len
    return k, d


def atr(candles: List[dict], n: int) -> Series:
    """ATR de Wilder."""
    out: Series = [None] * len(candles)
    if len(candles) <= n:
        return out
    trs: List[float] = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    seed = sum(trs[:n]) / n
    out[n] = seed
    prev = seed
    for i in range(n + 1, len(candles)):
        prev = (prev * (n - 1) + trs[i - 1]) / n
        out[i] = prev
    return out


def supertrend(candles: List[dict], n: int, mult: float) -> Series:
    """
    Direction Supertrend : +1 (haussier) / -1 (baissier) / None (chauffe).
    Bandes cliquet classiques sur hl2 ± mult × ATR.
    """
    a = atr(candles, n)
    out: Series = [None] * len(candles)
    up = dn = None      # bandes finales
    trend = None
    for i, c in enumerate(candles):
        if a[i] is None:
            continue
        hl2 = (c["high"] + c["low"]) / 2.0
        basic_up = hl2 + mult * a[i]
        basic_dn = hl2 - mult * a[i]
        prev_close = candles[i - 1]["close"] if i > 0 else c["close"]
        if up is None:
            up, dn, trend = basic_up, basic_dn, 1
        else:
            up = basic_up if (basic_up < up or prev_close > up) else up
            dn = basic_dn if (basic_dn > dn or prev_close < dn) else dn
            if trend == 1 and c["close"] < dn:
                trend = -1
            elif trend == -1 and c["close"] > up:
                trend = 1
        out[i] = trend
    return out

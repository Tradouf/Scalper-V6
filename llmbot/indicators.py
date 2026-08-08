"""Indicateurs techniques purs Python (pas de pandas)."""

from __future__ import annotations

from typing import List, Optional


def ema(values: List[float], length: int) -> List[float]:
    out: List[float] = []
    k = 2.0 / (length + 1)
    prev: Optional[float] = None
    for v in values:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(closes: List[float], length: int = 14) -> List[float]:
    out = [50.0] * len(closes)
    avg_gain = avg_loss = 0.0
    alpha = 1.0 / length
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        if i <= length:
            avg_gain += gain / length
            avg_loss += loss / length
        else:
            avg_gain = avg_gain * (1 - alpha) + gain * alpha
            avg_loss = avg_loss * (1 - alpha) + loss * alpha
        if avg_loss <= 0:
            out[i] = 100.0 if avg_gain > 0 else 50.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def atr(candles: List[dict], length: int = 14) -> List[float]:
    out = [0.0] * len(candles)
    prev_atr: Optional[float] = None
    alpha = 1.0 / length
    for i, c in enumerate(candles):
        if i == 0:
            tr = c["high"] - c["low"]
        else:
            pc = candles[i - 1]["close"]
            tr = max(c["high"] - c["low"], abs(c["high"] - pc), abs(c["low"] - pc))
        prev_atr = tr if prev_atr is None else prev_atr * (1 - alpha) + tr * alpha
        out[i] = prev_atr
    return out


def macd_hist(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> List[float]:
    ema_f = ema(closes, fast)
    ema_s = ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_f, ema_s)]
    signal_line = ema(macd_line, signal)
    return [m - s for m, s in zip(macd_line, signal_line)]


def compute_snapshot(candles: List[dict]) -> dict:
    """Résumé technique pour le scanner et le LLM."""
    if len(candles) < 30:
        return {}
    closes = [c["close"] for c in candles]
    price = closes[-1]
    atr_v = atr(candles, 14)
    rsi_v = rsi(closes, 14)
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    hist = macd_hist(closes)
    atr_pct = atr_v[-1] / price if price else 0.0

    # S/R simples : min/max 20 dernières bougies
    recent = candles[-20:]
    support = min(c["low"] for c in recent)
    resistance = max(c["high"] for c in recent)

    trend = "bull" if ema20[-1] > ema50[-1] and price > ema50[-1] else (
        "bear" if ema20[-1] < ema50[-1] and price < ema50[-1] else "range"
    )

    return {
        "price": price,
        "atr": atr_v[-1],
        "atr_pct": round(atr_pct, 6),
        "rsi": round(rsi_v[-1], 2),
        "macd_hist": round(hist[-1], 8),
        "macd_hist_prev": round(hist[-2], 8) if len(hist) > 1 else 0.0,
        "ema20": ema20[-1],
        "ema50": ema50[-1],
        "trend": trend,
        "support": support,
        "resistance": resistance,
        "dist_support_pct": round((price - support) / price, 4) if price else 0,
        "dist_resistance_pct": round((resistance - price) / price, 4) if price else 0,
    }
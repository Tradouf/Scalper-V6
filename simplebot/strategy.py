"""
Stratégie SimpleBot — EMA cross + filtre RSI, TP/SL en multiples d'ATR.

Pur Python (pas de pandas) : la même fonction de signal sert au backtest
et au live, garantissant zéro divergence entre les deux.

Une bougie est un dict {"ts", "open", "high", "low", "close", "volume"}.
Signal : +1 = ouvrir long, -1 = ouvrir short, 0 = rien.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, asdict
from typing import List, Optional

RSI_LEN = 14
RSI_LONG_MAX = 75.0   # pas de long si déjà suracheté
RSI_SHORT_MIN = 25.0  # pas de short si déjà survendu


@dataclass(frozen=True)
class StrategyParams:
    ema_fast: int = 12
    ema_slow: int = 50
    atr_len: int = 14
    tp_atr: float = 2.5   # take-profit = entry ± tp_atr × ATR
    sl_atr: float = 1.5   # stop-loss   = entry ∓ sl_atr × ATR

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StrategyParams":
        return cls(
            ema_fast=int(d["ema_fast"]),
            ema_slow=int(d["ema_slow"]),
            atr_len=int(d.get("atr_len", 14)),
            tp_atr=float(d["tp_atr"]),
            sl_atr=float(d["sl_atr"]),
        )

    @property
    def warmup_bars(self) -> int:
        return max(self.ema_slow, self.atr_len, RSI_LEN) + 5


def param_grid() -> List[StrategyParams]:
    """Grille explorée par l'optimiseur (72 combinaisons)."""
    grid = []
    for fast, slow in itertools.product((9, 12, 21), (26, 50, 100)):
        if slow < fast * 2:
            continue
        for tp in (1.5, 2.5, 3.5):
            for sl in (1.0, 1.5, 2.0):
                grid.append(StrategyParams(ema_fast=fast, ema_slow=slow, tp_atr=tp, sl_atr=sl))
    return grid


# ── Indicateurs ──────────────────────────────────────────────────────────────

def ema(values: List[float], length: int) -> List[float]:
    out = []
    k = 2.0 / (length + 1)
    prev: Optional[float] = None
    for v in values:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(closes: List[float], length: int = RSI_LEN) -> List[float]:
    """RSI de Wilder (lissage exponentiel alpha=1/length)."""
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


def atr(candles: List[dict], length: int) -> List[float]:
    """ATR de Wilder."""
    out = [0.0] * len(candles)
    prev_atr: Optional[float] = None
    alpha = 1.0 / length
    for i, c in enumerate(candles):
        if i == 0:
            tr = c["high"] - c["low"]
        else:
            prev_close = candles[i - 1]["close"]
            tr = max(
                c["high"] - c["low"],
                abs(c["high"] - prev_close),
                abs(c["low"] - prev_close),
            )
        prev_atr = tr if prev_atr is None else prev_atr * (1 - alpha) + tr * alpha
        out[i] = prev_atr
    return out


# ── Signaux ──────────────────────────────────────────────────────────────────

def compute_signals(candles: List[dict], params: StrategyParams) -> List[int]:
    """
    +1 à la bougie où l'EMA rapide croise l'EMA lente à la hausse (RSI < 75),
    -1 sur croisement baissier (RSI > 25), 0 sinon. Rien pendant le warmup.
    """
    n = len(candles)
    signals = [0] * n
    if n < params.warmup_bars + 1:
        return signals

    closes = [c["close"] for c in candles]
    ema_f = ema(closes, params.ema_fast)
    ema_s = ema(closes, params.ema_slow)
    rsi_v = rsi(closes)

    for i in range(params.warmup_bars, n):
        crossed_up = ema_f[i - 1] <= ema_s[i - 1] and ema_f[i] > ema_s[i]
        crossed_dn = ema_f[i - 1] >= ema_s[i - 1] and ema_f[i] < ema_s[i]
        if crossed_up and rsi_v[i] < RSI_LONG_MAX:
            signals[i] = 1
        elif crossed_dn and rsi_v[i] > RSI_SHORT_MIN:
            signals[i] = -1
    return signals


def latest_signal(candles: List[dict], params: StrategyParams) -> dict:
    """
    Signal de la DERNIÈRE bougie de la liste (le live ne passe que des bougies
    clôturées). Retourne aussi l'ATR courant pour dimensionner TP/SL.
    """
    if len(candles) < params.warmup_bars + 1:
        return {"signal": 0, "atr": 0.0, "close": 0.0, "ts": None}
    signals = compute_signals(candles, params)
    atr_v = atr(candles, params.atr_len)
    last = candles[-1]
    return {
        "signal": signals[-1],
        "atr": atr_v[-1],
        "close": last["close"],
        "ts": last.get("ts"),
    }

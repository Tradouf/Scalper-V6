"""
Espace de stratégies MinuteLab.

Une stratégie = un déclencheur d'entrée (trigger) × un filtre de tendance
optionnel × une longueur de moyenne pour la sortie PnL/MA. Les signaux sont
évalués à la CLÔTURE de chaque bougie 1 m : +1 entrée long, -1 entrée short,
0 rien. La sortie de position n'est pas un signal : elle est gérée par le
backtester / le moteur live (gain croise sous sa moyenne).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from minutelab.indicators import ema, rsi, sma, stochastic, supertrend


@dataclass(frozen=True)
class Strat:
    trigger: str                 # rsi_revert | rsi_momo | stoch_cross | supertrend_flip | ema_cross | sma_cross
    tparams: tuple               # paramètres du trigger (voir _TRIGGERS)
    filt: str = "none"           # none | supertrend | ema_side
    fparams: tuple = field(default=())
    exit_ma_bars: int = 3        # approximation 1 m de la MA de sortie (live : 5 s)

    @property
    def name(self) -> str:
        t = f"{self.trigger}{list(self.tparams)}"
        f = "" if self.filt == "none" else f" + {self.filt}{list(self.fparams)}"
        return f"{t}{f} | exitMA={self.exit_ma_bars}"


def _cross_up(a_prev, a_now, b_prev, b_now) -> bool:
    if None in (a_prev, a_now, b_prev, b_now):
        return False
    return a_prev <= b_prev and a_now > b_now


def _cross_dn(a_prev, a_now, b_prev, b_now) -> bool:
    if None in (a_prev, a_now, b_prev, b_now):
        return False
    return a_prev >= b_prev and a_now < b_now


def compute_signals(candles: List[dict], s: Strat) -> List[int]:
    """Signaux d'entrée par bougie (évalués à la clôture)."""
    n = len(candles)
    closes = [c["close"] for c in candles]
    sig = [0] * n

    if s.trigger == "rsi_revert":
        p, os_, ob = s.tparams
        r = rsi(closes, p)
        for i in range(1, n):
            if r[i - 1] is None or r[i] is None:
                continue
            if r[i - 1] < os_ <= r[i]:
                sig[i] = 1
            elif r[i - 1] > ob >= r[i]:
                sig[i] = -1

    elif s.trigger == "rsi_momo":
        p, band = s.tparams
        r = rsi(closes, p)
        for i in range(1, n):
            if _cross_up(r[i - 1], r[i], 50 + band, 50 + band):
                sig[i] = 1
            elif _cross_dn(r[i - 1], r[i], 50 - band, 50 - band):
                sig[i] = -1

    elif s.trigger == "stoch_cross":
        k_len, d_len, low, high = s.tparams
        k, d = stochastic(candles, k_len, d_len)
        for i in range(1, n):
            if k[i - 1] is None or d[i - 1] is None:
                continue
            if _cross_up(k[i - 1], k[i], d[i - 1], d[i]) and min(k[i - 1], d[i - 1]) < low:
                sig[i] = 1
            elif _cross_dn(k[i - 1], k[i], d[i - 1], d[i]) and max(k[i - 1], d[i - 1]) > high:
                sig[i] = -1

    elif s.trigger == "supertrend_flip":
        p, mult = s.tparams
        st = supertrend(candles, p, mult)
        for i in range(1, n):
            if st[i - 1] is None or st[i] is None:
                continue
            if st[i - 1] == -1 and st[i] == 1:
                sig[i] = 1
            elif st[i - 1] == 1 and st[i] == -1:
                sig[i] = -1

    elif s.trigger in ("ema_cross", "sma_cross"):
        f_len, s_len = s.tparams
        ma = ema if s.trigger == "ema_cross" else sma
        fast, slow = ma(closes, f_len), ma(closes, s_len)
        for i in range(1, n):
            if _cross_up(fast[i - 1], fast[i], slow[i - 1], slow[i]):
                sig[i] = 1
            elif _cross_dn(fast[i - 1], fast[i], slow[i - 1], slow[i]):
                sig[i] = -1

    else:
        raise ValueError(f"trigger inconnu: {s.trigger}")

    return _apply_filter(candles, closes, sig, s)


def _apply_filter(candles, closes, sig: List[int], s: Strat) -> List[int]:
    if s.filt == "none":
        return sig
    if s.filt == "supertrend":
        p, mult = s.fparams
        st = supertrend(candles, p, mult)
        return [x if x != 0 and st[i] == x else 0 for i, x in enumerate(sig)]
    if s.filt == "ema_side":
        (p,) = s.fparams
        e = ema(closes, p)
        out = []
        for i, x in enumerate(sig):
            if x == 0 or e[i] is None:
                out.append(0)
            else:
                side = 1 if closes[i] > e[i] else -1
                out.append(x if side == x else 0)
        return out
    raise ValueError(f"filtre inconnu: {s.filt}")


# --- Grille de recherche ---------------------------------------------------

_TRIGGERS = (
    [("rsi_revert", (p, os_, ob)) for p in (7, 14) for os_, ob in ((30, 70), (25, 75), (20, 80))]
    + [("rsi_momo", (p, b)) for p in (7, 14) for b in (0, 5)]
    + [("stoch_cross", (k, 3, lo, hi)) for k in (5, 9, 14) for lo, hi in ((20, 80), (25, 75))]
    + [("supertrend_flip", (p, m)) for p, m in ((7, 1.5), (10, 2.0), (10, 3.0), (14, 2.0))]
    + [("ema_cross", pair) for pair in ((3, 10), (5, 13), (5, 20), (9, 21))]
    + [("sma_cross", pair) for pair in ((5, 20), (10, 30))]
)

_FILTERS = [
    ("none", ()),
    ("supertrend", (10, 3.0)),
    ("ema_side", (50,)),
]

_EXIT_MA_BARS = (3, 5)


def build_grid() -> List[Strat]:
    """Toutes les combinaisons trigger × filtre × MA de sortie (~168)."""
    grid = []
    for trig, tp in _TRIGGERS:
        for filt, fp in _FILTERS:
            # Filtrer un flip de supertrend par un supertrend est redondant.
            if trig == "supertrend_flip" and filt == "supertrend":
                continue
            for xb in _EXIT_MA_BARS:
                grid.append(Strat(trig, tp, filt, fp, xb))
    return grid

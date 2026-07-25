"""
Sleeve C — Breakout Donchian 1h (SPEC §3C). Stratégie nouvelle, simple,
complémentaire des EMA : moins de trades, meilleur R:R en tendance forte.

  LONG  : close > plus-haut des `donchian_len` bougies PRÉCÉDENTES
          ET ATR(14) > SMA(ATR, 50)          (expansion de volatilité)
  SHORT : close < plus-bas des N précédentes ET expansion
  SL    : sl_atr × ATR(14) natif · TP : tp_atr × ATR(14) natif
  Time-exit : 48 bougies 1h (2 jours) si ni TP ni SL

Grille réduite (27 combinaisons), même walk-forward + filtre qualité que la
sleeve B. Le filtre d'expansion ATR est TOUJOURS actif (hors grille) : un
breakout sans volatilité est un faux départ statistique.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import List

from simplebot.strategy import atr

from superbot.indicators import sma
from superbot.sleeves.base import ExitPolicy, Sleeve

TIME_EXIT_BARS = 48
ATR_EXPANSION_SMA = 50


@dataclass(frozen=True)
class BreakoutParams:
    donchian_len: int = 20
    sl_atr: float = 1.5
    tp_atr: float = 3.0


class BreakoutSleeve(Sleeve):

    name = "breakout"
    timeframes = ("1h",)      # 1h uniquement (SPEC §8 étape 3)
    optimizable = True

    def grid(self) -> List[BreakoutParams]:
        out = []
        for dlen in (15, 20, 30):
            for sl in (1.0, 1.5, 2.0):
                for tp in (2.5, 3.0, 4.0):
                    out.append(BreakoutParams(donchian_len=dlen, sl_atr=sl, tp_atr=tp))
        return out

    def signals(self, candles: List[dict], params: BreakoutParams) -> List[int]:
        n = len(candles)
        sig = [0] * n
        dlen = params.donchian_len
        atr_v = atr(candles, 14)
        atr_sma = sma(atr_v, ATR_EXPANSION_SMA)
        for i in range(max(dlen, ATR_EXPANSION_SMA) + 1, n):
            if atr_v[i] <= atr_sma[i]:          # pas d'expansion → pas de breakout
                continue
            window = candles[i - dlen:i]        # bougies PRÉCÉDENTES (i exclue)
            hh = max(c["high"] for c in window)
            ll = min(c["low"] for c in window)
            close = candles[i]["close"]
            if close > hh:
                sig[i] = 1
            elif close < ll:
                sig[i] = -1
        return sig

    def exit_policy(self, params: BreakoutParams) -> ExitPolicy:
        return ExitPolicy(tp_atr=params.tp_atr, sl_atr=params.sl_atr,
                          atr_len=14, time_exit_bars=TIME_EXIT_BARS)

    def params_to_dict(self, params: BreakoutParams) -> dict:
        return asdict(params)

    def params_from_dict(self, d: dict) -> BreakoutParams:
        return BreakoutParams(
            donchian_len=int(d["donchian_len"]),
            sl_atr=float(d["sl_atr"]),
            tp_atr=float(d["tp_atr"]),
        )

    def warmup_bars(self, params: BreakoutParams) -> int:
        return max(params.donchian_len, ATR_EXPANSION_SMA) + 5

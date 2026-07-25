"""
Sleeve A — Momentum 4h (SPEC §3A). Params FIGÉS, jamais optimisés.

La seule stratégie du repo validée out-of-sample sur 833 j × 31 symboles
(t-stat cluster-robuste 2.9 — voir simplebot/momentum.py, patron repris) :

  ROC(12 bougies 4h = 48 h) > +2 % → LONG ; < -2 % → SHORT (suivre le mouvement)
  PAS de take-profit — les TP amputent les gros gagnants et tuent l'edge
  SL 2×ATR(14) natif · time-exit 72 bougies (12 j) · signal opposé → flip

Corrections issues du paper simplebot (WR ~19 % en juillet) — filtres LIVE :
  - pas de LONG si funding horaire > +MOMENTUM_FUNDING_GATE (on paierait la
    foule ~0.29 %/12 j, mesuré) ; pas de SHORT si funding < -gate ;
  - pas d'entrée si spread > MAX_SPREAD_PCT ;
  - cap MAX_OPEN_PER_SLEEVE["momentum"] (6) appliqué par l'orchestrateur.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import List

from superbot import config
from superbot.sleeves.base import ExitPolicy, Sleeve


@dataclass(frozen=True)
class MomentumParams:
    roc_bars: int = config.MOMENTUM_ROC_BARS
    thr: float = config.MOMENTUM_THR
    sl_atr: float = config.MOMENTUM_SL_ATR
    time_exit_bars: int = config.MOMENTUM_TIME_EXIT_BARS


class MomentumSleeve(Sleeve):

    name = "momentum"
    timeframes = ("4h",)
    optimizable = False        # SPEC §8 étape 4 : pas d'optimisation

    def grid(self) -> List[MomentumParams]:
        return [MomentumParams()]

    def signals(self, candles: List[dict], params: MomentumParams) -> List[int]:
        """+1/-1 tant que le ROC(roc_bars) dépasse ±thr — l'entrée ne se fait
        qu'à plat ou en flip (géré par le moteur), le signal peut donc rester
        actif plusieurs bougies sans double ordre."""
        n = len(candles)
        sig = [0] * n
        closes = [c["close"] for c in candles]
        for i in range(params.roc_bars + 1, n):
            past = closes[i - params.roc_bars]
            if past <= 0:
                continue
            roc = closes[i] / past - 1.0
            if roc > params.thr:
                sig[i] = 1
            elif roc < -params.thr:
                sig[i] = -1
        return sig

    def exit_policy(self, params: MomentumParams) -> ExitPolicy:
        return ExitPolicy(tp_atr=None,                      # PAS de TP — figé
                          sl_atr=params.sl_atr,
                          atr_len=14,
                          time_exit_bars=params.time_exit_bars)

    def params_to_dict(self, params: MomentumParams) -> dict:
        return asdict(params)

    def params_from_dict(self, d: dict) -> MomentumParams:
        return MomentumParams(
            roc_bars=int(d.get("roc_bars", config.MOMENTUM_ROC_BARS)),
            thr=float(d.get("thr", config.MOMENTUM_THR)),
            sl_atr=float(d.get("sl_atr", config.MOMENTUM_SL_ATR)),
            time_exit_bars=int(d.get("time_exit_bars", config.MOMENTUM_TIME_EXIT_BARS)),
        )

    def warmup_bars(self, params: MomentumParams) -> int:
        return max(params.roc_bars, 14) + 5

    def allow_live_entry(self, signal: int, context: dict) -> tuple:
        """Filtres live SPEC §3A : funding contre nous + spread trop large."""
        funding = context.get("funding_hourly")
        if funding is not None:
            if signal == 1 and funding > config.MOMENTUM_FUNDING_GATE:
                return False, "funding_gate_long"
            if signal == -1 and funding < -config.MOMENTUM_FUNDING_GATE:
                return False, "funding_gate_short"
        spread = context.get("spread_pct")
        if spread is not None and spread > config.MAX_SPREAD_PCT:
            return False, "spread_too_wide"
        return True, "ok"

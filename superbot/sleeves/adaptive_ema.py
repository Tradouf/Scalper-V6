"""
Sleeve B — Adaptive EMA (SPEC §3B).

Signaux identiques à simplebot/strategy.py (croisement EMA fast/slow + filtre
RSI 75/25), avec DEUX différences structurantes :

  1. **trend_ema = 200 FIGÉ, hors grille** : le filtre directionnel améliore
     l'edge (+23 % mesuré à params figés) mais le mettre dans la grille dégrade
     la sélection walk-forward (leçon R&D simplebot 07/2026 — le « 1er du rang
     train » attrape des sets filtrés chanceux). Ici il s'applique à TOUTES les
     combinaisons, l'optimiseur ne peut pas le désactiver.

  2. **Multi-timeframe** : la sleeve déclare 15m ET 1h ; l'optimiseur teste les
     deux et retient le meilleur par symbole (jamais < 15m, 4h réservé momentum).

Toute la logique indicateur vient de simplebot.strategy (import direct).
"""

from __future__ import annotations

import itertools
from typing import List

from simplebot.strategy import StrategyParams, compute_signals

from superbot import config
from superbot.sleeves.base import ExitPolicy, Sleeve


class AdaptiveEMASleeve(Sleeve):

    name = "adaptive_ema"
    timeframes = ("15m", "1h")
    optimizable = True

    def grid(self) -> List[StrategyParams]:
        """120 combinaisons (SPEC §3B), trend_ema=200 forcé sur chacune."""
        out = []
        for fast, slow in itertools.product((9, 12, 21), (26, 50, 100)):
            if slow < fast * 2:
                continue
            for tp in (1.5, 2.5, 3.5):
                for sl in (1.0, 1.5, 2.0, 3.0, 4.0):
                    out.append(StrategyParams(
                        ema_fast=fast, ema_slow=slow, tp_atr=tp, sl_atr=sl,
                        trend_ema=config.TREND_EMA_FIXED,
                    ))
        return out

    def signals(self, candles: List[dict], params: StrategyParams) -> List[int]:
        return compute_signals(candles, params)

    def exit_policy(self, params: StrategyParams) -> ExitPolicy:
        return ExitPolicy(tp_atr=params.tp_atr, sl_atr=params.sl_atr,
                          atr_len=params.atr_len)

    def params_to_dict(self, params: StrategyParams) -> dict:
        return params.to_dict()

    def params_from_dict(self, d: dict) -> StrategyParams:
        p = StrategyParams.from_dict(d)
        # garde-fou : le filtre directionnel ne peut pas être désactivé par un
        # best_params.json trafiqué/ancien
        if p.trend_ema != config.TREND_EMA_FIXED:
            p = StrategyParams(ema_fast=p.ema_fast, ema_slow=p.ema_slow,
                               atr_len=p.atr_len, tp_atr=p.tp_atr, sl_atr=p.sl_atr,
                               trend_ema=config.TREND_EMA_FIXED)
        return p

    def warmup_bars(self, params: StrategyParams) -> int:
        # warmup_bars de StrategyParams inclut déjà max(ema_slow, trend_ema)+5
        return params.warmup_bars

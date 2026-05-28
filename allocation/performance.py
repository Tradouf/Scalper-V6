"""
Performance scoring par stratégie — module léger qui consomme les Fill et
produit un multiplicateur de performance borné pour l'allocateur.

Algorithme :
  1. Pour chaque fill, on ventile (PnL net - fee) par stratégie et par jour.
  2. Pour chaque stratégie, on calcule le PnL net journalier cumulé puis la
     série de rendements journaliers en % d'un notional de référence.
  3. Score brut = EWMA(rendements / vol_estimée) avec demi-vie configurable.
  4. Score final = sigmoid-bounded vers [mult_min, mult_max].

Limites MVP :
  - On approxime le notional de référence à 100 USD (constant). Le score est
    donc une mesure relative entre stratégies, pas un Sharpe absolu.
  - Pas de fenêtre de warmup obligatoire : tant qu'on n'a pas de fills, le
    score est neutre (1.0).

Le scorer est stateful : à instancier une fois, mis à jour via on_fill().
"""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from typing import Dict, Optional

from core.types import Fill


class PerformanceScorer:
    """Calcule un multiplicateur de performance par strategy_id ∈ [mult_min, mult_max].

    Sans fills → score neutre 1.0.
    Avec assez d'historique → EWMA du rendement / vol → mappé sur [mult_min, mult_max].
    """

    NEUTRAL_SCORE = 1.0
    REFERENCE_NOTIONAL = 100.0  # USD : base de normalisation

    def __init__(
        self,
        mult_min: float = 0.3,
        mult_max: float = 1.5,
        halflife_days: float = 45.0,
        min_days_for_score: int = 7,
    ) -> None:
        if mult_min >= mult_max:
            raise ValueError("mult_min doit être < mult_max")
        self._mult_min = mult_min
        self._mult_max = mult_max
        self._halflife_days = halflife_days
        self._min_days = min_days_for_score
        # state : strategy_id → {date → daily_net_pnl}
        self._daily_pnl: Dict[str, Dict[dt.date, float]] = defaultdict(lambda: defaultdict(float))

    # ─── Input ───────────────────────────────────────────────────────────────

    def on_fill(self, fill: Fill) -> None:
        if fill.strategy_id is None:
            return
        # closedPnl approximé : pour le MVP, on prend (notional × sign) - fee.
        # En réalité HL calcule un closedPnl propre via realized PnL = (exit-entry)*qty.
        # Le backtester P5 alimentera avec le bon closedPnl par fill.
        # Ici on accumule juste le contributing PnL (fee toujours négatif).
        net = -fill.fee  # fees sont toujours un coût net
        # Pour le scoring MVP, on suppose que l'attribution PnL réelle viendra
        # via un canal séparé (fill.closedPnl_signed). Pour l'instant, on
        # met seulement les fees.
        day = fill.timestamp.date() if isinstance(fill.timestamp, dt.datetime) else dt.date.today()
        self._daily_pnl[fill.strategy_id][day] += net

    def record_realized_pnl(self, strategy_id: str, day: dt.date, pnl: float) -> None:
        """Voie alternative : alimentation directe du PnL journalier (utilisé
        par le backtester ou par un attributeur externe).
        """
        self._daily_pnl[strategy_id][day] += pnl

    # ─── Output ──────────────────────────────────────────────────────────────

    def scores(self) -> Dict[str, float]:
        """Renvoie {strategy_id → multiplier ∈ [mult_min, mult_max]}.
        Stratégies sans historique suffisant : score neutre 1.0.
        """
        out: Dict[str, float] = {}
        for strat_id, daily in self._daily_pnl.items():
            out[strat_id] = self._score_one(strat_id)
        return out

    def _score_one(self, strat_id: str) -> float:
        daily = self._daily_pnl.get(strat_id, {})
        if len(daily) < self._min_days:
            return self.NEUTRAL_SCORE
        # Trier par date
        sorted_days = sorted(daily.keys())
        pnls = [daily[d] for d in sorted_days]
        # Rendements en %
        rets = [p / self.REFERENCE_NOTIONAL for p in pnls]
        if not rets:
            return self.NEUTRAL_SCORE
        # EWMA du rendement
        alpha = 1.0 - math.exp(-math.log(2.0) / self._halflife_days)
        ewma_ret = 0.0
        ewma_sq = 0.0  # E[r²] pour estimer la vol
        for r in rets:
            ewma_ret = alpha * r + (1.0 - alpha) * ewma_ret
            ewma_sq = alpha * (r * r) + (1.0 - alpha) * ewma_sq
        # Sharpe-like : ret / sqrt(var)
        var = max(ewma_sq - ewma_ret ** 2, 1e-9)
        sharpe_like = ewma_ret / math.sqrt(var)
        # Mapping vers [mult_min, mult_max] via sigmoid
        # sharpe ∈ [-3, +3] typique → on mappe avec slope douce
        sig = 1.0 / (1.0 + math.exp(-sharpe_like))  # ∈ (0, 1)
        score = self._mult_min + sig * (self._mult_max - self._mult_min)
        return float(score)

    # ─── Debug / monitoring ──────────────────────────────────────────────────

    def daily_pnl(self, strategy_id: str) -> Dict[dt.date, float]:
        return dict(self._daily_pnl.get(strategy_id, {}))

    def cumulative_pnl(self, strategy_id: str) -> float:
        return sum(self._daily_pnl.get(strategy_id, {}).values())

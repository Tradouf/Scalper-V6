"""
RuleBasedAllocator — combine régime + signaux + perf scores → TargetPortfolio.

Pipeline (cf. spec §6.2) :
  1. Poids "doux" base par stratégie : base_i = Σ_r P(r) × B[r][i]
  2. Multiplicateur de performance borné : mult_i = clamp(perf, MIN, MAX)
     raw_i = base_i × mult_i
  3. Normalisation : weight_i = raw_i / Σ_j raw_j
  4. Pour chaque signal directionnel (target_notional > 0) :
        contrib_i = weight_i × target_notional × confidence × sign(direction)
  5. Aggrégation par actif : target_notional[asset] = Σ contrib_i
  6. Construction TargetPortfolio (gross, net, contributing_strategies).

NB MVP : la vol-targeting (étape 4 de la spec) est mise en suspens — on n'a
pas encore d'historique de vol par stratégie. Elle sera réintégrée en P5 quand
le backtester aura tourné et fournira les vols réalisées.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict

from core.config import AllocationConfig
from core.interfaces import Portfolio
from core.types import (
    Regime,
    RegimeState,
    Signal,
    TargetPortfolio,
    TargetPosition,
)


class RuleBasedAllocator:
    """Allocateur règle-based avec matrice B et multiplicateur de perf borné."""

    def __init__(self, allocation_config: AllocationConfig) -> None:
        self._cfg = allocation_config

    def allocate(
        self,
        signals: list[Signal],
        regime: RegimeState,
        current_portfolio: Portfolio,
        perf_scores: Dict[str, float],
    ) -> TargetPortfolio:
        # ─── Étape 1-3 : poids par stratégie ─────────────────────────────────
        weights = self._compute_strategy_weights(regime, perf_scores)

        # ─── Étape 4-5 : agrégation par actif ────────────────────────────────
        # contrib_i = weight_i × target_notional × confidence × sign(direction)
        # On agrège par asset en gardant le détail par stratégie pour attribution.
        by_asset: Dict[str, Dict] = defaultdict(
            lambda: {"notional": 0.0, "contribs": {}}
        )
        for sig in signals:
            if sig.target_notional <= 0:
                # CLOSE ou HOLD : pas de contribution à l'allocation positive.
                # Note : un CLOSE est un signal explicite pour fermer une
                # position existante. Le risk manager / exec gérera la
                # transition vers 0 lors du reconcile.
                continue
            w = weights.get(sig.strategy_id, 0.0)
            if w <= 0:
                continue
            contrib = w * sig.target_notional * sig.confidence * float(sig.direction)
            by_asset[sig.asset]["notional"] += contrib
            prev = by_asset[sig.asset]["contribs"].get(sig.strategy_id, 0.0)
            by_asset[sig.asset]["contribs"][sig.strategy_id] = prev + contrib

        # ─── Étape 6 : TargetPortfolio ───────────────────────────────────────
        positions = [
            TargetPosition(
                asset=asset,
                target_notional=float(data["notional"]),
                contributing_strategies={k: float(v) for k, v in data["contribs"].items()},
            )
            for asset, data in by_asset.items()
        ]
        gross = sum(abs(p.target_notional) for p in positions)
        net = sum(p.target_notional for p in positions)

        return TargetPortfolio(
            timestamp=regime.timestamp,
            positions=positions,
            gross_exposure=gross,
            net_exposure=net,
        )

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _compute_strategy_weights(
        self, regime: RegimeState, perf_scores: Dict[str, float]
    ) -> Dict[str, float]:
        """Étapes 1-3 du pipeline : base × mult, normalisation."""
        # 1. base_i = Σ_r P(r) × B[r][i]
        # On itère sur les stratégies présentes dans la matrice B.
        strategy_ids = set()
        for d in self._cfg.base_weights.values():
            strategy_ids.update(d.keys())

        base: Dict[str, float] = {}
        for strat_id in strategy_ids:
            base[strat_id] = sum(
                regime.probabilities.get(r, 0.0) * self._cfg.base_weights[r].get(strat_id, 0.0)
                for r in Regime
            )

        # 2. raw_i = base_i × clamp(perf, mult_min, mult_max)
        raw: Dict[str, float] = {}
        for strat_id, b in base.items():
            mult = perf_scores.get(strat_id, 1.0)
            mult = max(self._cfg.mult_min, min(self._cfg.mult_max, mult))
            raw[strat_id] = b * mult

        # 3. Normalisation
        total = sum(raw.values())
        if total <= 1e-12:
            # Tous les raw_i nulls → poids uniformes (dégénérescence)
            n = max(len(raw), 1)
            return {k: 1.0 / n for k in raw}
        return {k: v / total for k, v in raw.items()}

    # Accès debug : permet de récupérer les poids effectifs pour le dashboard.
    def get_weights(
        self, regime: RegimeState, perf_scores: Dict[str, float]
    ) -> Dict[str, float]:
        return self._compute_strategy_weights(regime, perf_scores)

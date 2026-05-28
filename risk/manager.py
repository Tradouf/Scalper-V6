"""
RiskManager — projette TargetPortfolio sur les contraintes dures.

Contraintes :
  - cap gross : Σ |notional| ≤ max_gross_exposure_pct × equity
  - cap par actif : |notional[asset]| ≤ max_per_asset_pct × equity
  - kill switch DD : drawdown courant > kill_switch_dd_pct → portfolio vide
  - daily loss limit : daily_pnl_pct < -daily_loss_limit_pct → portfolio vide

Stratégie de projection :
  1. Si kill_switch → renvoyer un TargetPortfolio vide (toutes positions = 0).
  2. Sinon, appliquer caps par actif (scale-down les notionnaux qui dépassent).
  3. Si gross dépasse max_gross après cap par actif, scaler tout par le facteur
     gross_max / gross_courant (proportionnel).

L'attribution par stratégie est conservée : si on scale x0.8, on scale chaque
contributing_strategies du même facteur.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Dict

from core.config import RiskConfig
from core.types import TargetPortfolio, TargetPosition
from risk.state import RiskStateImpl

logger = logging.getLogger("v7.risk")


class RiskManager:
    """Implémente le Protocol RiskManager."""

    def __init__(self, cfg: RiskConfig) -> None:
        self._cfg = cfg

    # ─── API publique ────────────────────────────────────────────────────────

    def kill_switch_triggered(self, state: RiskStateImpl) -> bool:
        if state.current_drawdown >= self._cfg.kill_switch_dd_pct:
            return True
        if state.daily_pnl_pct <= -self._cfg.daily_loss_limit_pct:
            return True
        return False

    def project(self, target: TargetPortfolio, state: RiskStateImpl) -> TargetPortfolio:
        # 1. Kill-switch → vide
        if self.kill_switch_triggered(state):
            logger.critical(
                "RISK kill-switch déclenché : DD=%.2f%% daily_pnl=%.2f%% → flat portfolio",
                state.current_drawdown * 100, state.daily_pnl_pct * 100,
            )
            return TargetPortfolio(
                timestamp=target.timestamp,
                positions=[],
                gross_exposure=0.0,
                net_exposure=0.0,
            )

        equity = max(state.equity, 1e-9)
        cap_per_asset = self._cfg.max_per_asset_pct * equity
        cap_gross = self._cfg.max_gross_exposure_pct * equity

        # 2. Caps par actif
        capped_positions: list[TargetPosition] = []
        for pos in target.positions:
            n = pos.target_notional
            cap_sign = 1.0
            if abs(n) > cap_per_asset:
                cap_sign = cap_per_asset / abs(n)
                logger.info(
                    "RISK cap actif %s : %.2f → %.2f (max %.2f = %.0f%% equity)",
                    pos.asset, n, n * cap_sign, cap_per_asset,
                    self._cfg.max_per_asset_pct * 100,
                )
            scaled_contribs = {k: v * cap_sign for k, v in pos.contributing_strategies.items()}
            capped_positions.append(replace(
                pos,
                target_notional=n * cap_sign,
                contributing_strategies=scaled_contribs,
            ))

        gross_after_asset_cap = sum(abs(p.target_notional) for p in capped_positions)

        # 3. Cap gross global
        if gross_after_asset_cap > cap_gross > 0:
            global_scale = cap_gross / gross_after_asset_cap
            logger.info(
                "RISK cap gross : %.2f → %.2f (max %.2f = %.0f%% equity, scale=%.3f)",
                gross_after_asset_cap, gross_after_asset_cap * global_scale,
                cap_gross, self._cfg.max_gross_exposure_pct * 100, global_scale,
            )
            capped_positions = [
                replace(
                    p,
                    target_notional=p.target_notional * global_scale,
                    contributing_strategies={k: v * global_scale for k, v in p.contributing_strategies.items()},
                )
                for p in capped_positions
            ]

        gross = sum(abs(p.target_notional) for p in capped_positions)
        net = sum(p.target_notional for p in capped_positions)

        return TargetPortfolio(
            timestamp=target.timestamp,
            positions=capped_positions,
            gross_exposure=gross,
            net_exposure=net,
        )

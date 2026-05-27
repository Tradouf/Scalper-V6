"""
GridStrategy — wrapper StrategyAgent autour de GridEngine.

Le grid est event-driven (FSM avec limit orders persistants), pas signal-driven
classique. Cette classe l'adapte à l'interface StrategyAgent du V7 :

  - strategy_id = "grid"
  - generate_signals(market) :
      Pour chaque symbole avec un grid actif, retourne un Signal qui décrit
      l'exposition nette courante (= somme signée des fills non-TPés). Sert
      surtout à l'attribution (l'allocateur sait combien le grid "réclame").
      Pour les symboles SANS grid actif mais éligibles (régime range, dans la
      watchlist), retourne un Signal "candidat" (target_notional 0, confidence
      basée sur le budget reçu) pour signaler l'intention.
  - on_fill(fill) :
      Mise à jour de la FSM grid (cosmétique car la FSM se met à jour aussi
      via on_tick / open_oids). Stocke pour traçabilité.

L'allocateur ne contrôle PAS le grid via Signal.target_notional. Il lui
accorde un budget via set_budget(symbol, usdc). Le grid l'utilise pour
activer/désactiver ses ladders.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from core.types import Fill, MarketSnapshot, Signal
from strategies.grid_engine import GridEngine

logger = logging.getLogger("v7.strategy.grid")


class GridStrategy:
    """StrategyAgent qui pilote un GridEngine."""

    def __init__(
        self,
        grid_engine: GridEngine,
        symbols: list[str],
        strategy_id: str = "grid",
        default_horizon_bars: int = 24,
    ) -> None:
        self._engine = grid_engine
        self._symbols = list(symbols)
        self._strategy_id = strategy_id
        self._horizon_bars = default_horizon_bars
        # Budgets alloués par symbole (USD). Si > activation_threshold_usdc,
        # le grid s'auto-active sur ce symbole au prochain tick. Sinon il
        # se désactive si actif.
        self._budgets: dict[str, float] = {sym: 0.0 for sym in symbols}
        # Position nette tracking (signée). Mise à jour via on_fill.
        self._net_exposure: dict[str, float] = {sym: 0.0 for sym in symbols}
        self._fills_received: list[Fill] = []

    # ─── StrategyAgent contract ───────────────────────────────────────────────

    @property
    def strategy_id(self) -> str:
        return self._strategy_id

    def generate_signals(self, market: MarketSnapshot) -> list[Signal]:
        """Pour chaque symbole de la whitelist, émet un Signal.

        Sémantique du Signal pour le grid :
          - direction = sign(exposition nette courante) (0 si flat)
          - target_notional = |exposition nette courante| (USD)
          - confidence = budget alloué / notional max théorique (proxy)
          - expected_edge_bps = 30 (estimation heuristique, à ajuster avec
            data après backtest P5)
        """
        signals: list[Signal] = []
        for sym in self._symbols:
            if sym not in market.prices:
                continue
            mark = float(market.prices[sym])
            if mark <= 0:
                continue
            net = self._net_exposure.get(sym, 0.0)
            budget = self._budgets.get(sym, 0.0)
            direction = 0.0
            if abs(net) > 1e-9:
                direction = 1.0 if net > 0 else -1.0
            # Confiance : proportion du budget vs un cap (10× notional par level)
            try:
                max_budget = float(self._engine._cfg.notional_per_level_usdc) * 10.0
            except Exception:
                max_budget = 300.0
            confidence = min(1.0, max(0.0, budget / max(max_budget, 1.0)))

            signals.append(
                Signal(
                    strategy_id=self._strategy_id,
                    asset=sym,
                    direction=direction,
                    target_notional=abs(net),
                    expected_edge_bps=30.0,
                    confidence=confidence,
                    stop_price=None,  # grid n'a pas de SL directionnel
                    horizon_bars=self._horizon_bars,
                    timestamp=market.timestamp,
                )
            )
        return signals

    def on_fill(self, fill: Fill) -> None:
        """Met à jour le tracking de l'exposition nette."""
        self._fills_received.append(fill)
        if fill.asset in self._net_exposure:
            self._net_exposure[fill.asset] += fill.notional

    # ─── Interface side-band (allocateur → grid) ──────────────────────────────

    def set_budget(self, symbol: str, budget_usdc: float) -> None:
        """L'allocateur configure le budget grid par symbole.

        Si budget > activation_threshold_usdc, le grid s'auto-active sur ce
        symbole au prochain tick on_tick (sous condition de régime range +
        prix disponible). Sinon, désactivation soft.

        Cette interface est side-band : non couverte par le Protocol
        StrategyAgent (qui est minimal). C'est l'allocateur (composition
        root) qui la connaît pour le cas spécial Grid.
        """
        self._budgets[symbol] = float(budget_usdc)

    def get_budget(self, symbol: str) -> float:
        return self._budgets.get(symbol, 0.0)

    def get_net_exposure(self, symbol: str) -> float:
        return self._net_exposure.get(symbol, 0.0)

    # ─── Accès au moteur (pour le composition root + tests) ───────────────────

    @property
    def engine(self) -> GridEngine:
        return self._engine

    def active_symbols(self) -> list[str]:
        return self._engine.active_symbols()

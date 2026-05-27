"""
MockStrategy — implémentation minimale du Protocol StrategyAgent.

Sert à :
  - vérifier que le Protocol est cohérent (pas de méthode oubliée).
  - permettre des tests unitaires de l'allocateur sans stratégie réelle.

Comportement : produit un Signal LONG fixe sur le premier symbole du market
snapshot, avec confiance et target_notional configurables au constructeur.
"""
from __future__ import annotations

from typing import Optional

from core.types import Fill, MarketSnapshot, Signal


class MockStrategy:
    """Implémentation triviale de StrategyAgent (validée via Protocol)."""

    def __init__(
        self,
        strategy_id: str = "mock",
        target_notional: float = 100.0,
        direction: float = 1.0,
        confidence: float = 0.6,
        expected_edge_bps: float = 20.0,
        horizon_bars: int = 4,
        stop_price: Optional[float] = None,
    ) -> None:
        self._strategy_id = strategy_id
        self._target_notional = target_notional
        self._direction = direction
        self._confidence = confidence
        self._expected_edge_bps = expected_edge_bps
        self._horizon_bars = horizon_bars
        self._stop_price = stop_price
        self.fills_received: list[Fill] = []

    @property
    def strategy_id(self) -> str:
        return self._strategy_id

    def generate_signals(self, market: MarketSnapshot) -> list[Signal]:
        if not market.candles:
            return []
        asset = next(iter(market.candles.keys()))
        return [
            Signal(
                strategy_id=self._strategy_id,
                asset=asset,
                direction=self._direction,
                target_notional=self._target_notional,
                expected_edge_bps=self._expected_edge_bps,
                confidence=self._confidence,
                stop_price=self._stop_price,
                horizon_bars=self._horizon_bars,
                timestamp=market.timestamp,
            )
        ]

    def on_fill(self, fill: Fill) -> None:
        self.fills_received.append(fill)

"""
Portfolio concret — vue agrégée du portefeuille (positions + equity).

Implémente le Protocol Portfolio (core/interfaces.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


@dataclass
class PortfolioImpl:
    """Implémentation concrète du Protocol Portfolio."""

    _positions: Dict[str, float] = field(default_factory=dict)  # asset → notional signé
    _equity: float = 0.0

    @property
    def positions(self) -> Dict[str, float]:
        return dict(self._positions)

    @property
    def equity(self) -> float:
        return self._equity

    # ─── API mutation ────────────────────────────────────────────────────────

    def set_position(self, asset: str, notional: float) -> None:
        if abs(notional) < 1e-9:
            self._positions.pop(asset, None)
        else:
            self._positions[asset] = notional

    def adjust_position(self, asset: str, delta: float) -> None:
        new = self._positions.get(asset, 0.0) + delta
        self.set_position(asset, new)

    def set_equity(self, value: float) -> None:
        self._equity = float(value)

    def adjust_equity(self, delta: float) -> None:
        self._equity += float(delta)

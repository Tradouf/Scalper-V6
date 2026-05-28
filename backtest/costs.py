"""
Modèle de coûts pour le backtester.

Hyperliquid (perp) :
  - maker fee ~ +0.015% (rebate négligeable selon tier ; modélisé à 0.015%)
  - taker fee ~ +0.045%
  - slippage market : proportionnel à √(notional / depth_proxy) — pour le MVP
    on prend une constante 2 bps × (1 + market_volatility_factor).
  - funding : payé/reçu toutes les 8h selon le funding rate × notional.

Référence : https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CostModel:
    """Modèle de coûts paramétrable."""

    maker_fee_bps: float = 1.5    # 0.015 %
    taker_fee_bps: float = 4.5    # 0.045 %
    slippage_bps: float = 2.0     # 2 bps slippage market par défaut

    def fee(self, notional: float, is_taker: bool = True) -> float:
        """Frais en USD pour un fill de |notional| USD."""
        bps = self.taker_fee_bps if is_taker else self.maker_fee_bps
        return abs(notional) * bps / 10_000.0

    def slippage(self, notional: float) -> float:
        """Coût de slippage en USD. Toujours pénalité (≥ 0)."""
        return abs(notional) * self.slippage_bps / 10_000.0

    def funding_cost(self, notional_signed: float, funding_rate: float, hours: float = 1.0) -> float:
        """Coût de funding pour une heure (ou fraction).

        Convention HL : funding_rate s'applique toutes les 8h. On scale linéairement.
        Pour une position LONG (notional > 0) avec funding positif → cost > 0
        (le long paie). Inversement pour short ou funding négatif.
        """
        return notional_signed * funding_rate * (hours / 8.0)

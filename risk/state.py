"""
RiskState — état risque courant utilisé par RiskManager pour ses décisions.

Implémente le Protocol RiskState (core/interfaces.py).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass
class RiskStateImpl:
    """Concrétisation simple du Protocol RiskState."""

    equity: float
    current_drawdown: float = 0.0       # [0, 1] ex: 0.05 = -5%
    daily_pnl_pct: float = 0.0          # depuis 00:00 UTC (positif ou négatif)
    peak_equity_today: float = 0.0
    timestamp: dt.datetime = None

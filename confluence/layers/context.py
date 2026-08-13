"""
`LayerContext` — tout ce qu'une couche a besoin de savoir en plus de ses
bougies, injecté explicitement.

Ce type existe pour une seule raison : rendre l'absence d'I/O possible. Une
couche qui irait chercher elle-même le funding, l'heure ou l'état du biais
serait intestable et non rejouable ; ici tout arrive par le contexte, y compris
`now_ms`. En backtest, `now_ms` est le temps simulé ; en live, l'horloge. Le
code des couches ne fait pas la différence — c'est le but.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from confluence.state import BiasState
from confluence.types import Bias, Regime, RiskLevel, Side, utc

Candle = Dict[str, float]


@dataclass(frozen=True)
class LayerContext:
    now_ms: int

    # Séries déjà filtrées « clôturées » par l'orchestrateur (§3).
    candles: Dict[str, List[Candle]] = field(default_factory=dict)

    # Funding HORAIRE courant (positif = les longs paient), tel que le rend
    # `simplebot.data.fetch_funding_rates`. None = donnée indisponible.
    funding_hourly: Optional[float] = None

    # Verdict du MacroRegimeAgent (§2). UNKNOWN par défaut : aucun veto, mais
    # aucun feu vert non plus — un défaut « NORMAL » aurait fait passer une
    # absence de données pour une confirmation.
    macro_risk: RiskLevel = RiskLevel.UNKNOWN

    # État persistant lu par l'orchestrateur, injecté ici en lecture seule.
    bias_state: BiasState = field(default_factory=BiasState)

    # Renseignés au fil de la descente 1d → 1h → 15m : la couche 15m a besoin
    # du régime et de la direction établis par la couche 1h.
    bias: Bias = Bias.FLAT
    regime: Optional[Regime] = None
    direction: Optional[Side] = None
    atr_1h: Optional[float] = None

    # Carnet d'ordres, couche 1m uniquement.
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None

    @property
    def now(self) -> datetime:
        return utc(self.now_ms)

    def series(self, timeframe: str) -> List[Candle]:
        return self.candles.get(timeframe, [])

    def with_(self, **changes) -> "LayerContext":
        """Copie modifiée — le contexte est figé pour qu'une couche ne puisse
        pas contaminer la suivante par effet de bord."""
        from dataclasses import replace

        return replace(self, **changes)


__all__ = ["Candle", "LayerContext"]

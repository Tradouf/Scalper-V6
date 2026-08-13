"""
StrategyRouter — SPEC §1.

```
RegimeLayer 1h ──> TREND  ──> moteur de tendance actif, GridAgent OFF
               ──> RANGE  ──> GridAgent actif, moteur de tendance OFF
               ──> CHOP   ──> les deux OFF
```

**Les deux ne sont jamais actifs simultanément.** C'est l'invariant central du
routage, et il est vérifié par `Route.is_coherent()` plutôt que laissé à la
lecture.

**État de la branche TREND.** Le ConfluenceAgent — moteur de tendance prévu par
la spec — a été REJETÉ par le §9 (registre, entrée n°1) et porte un
`DEPLOY_BLOCKED` exécutable. En conséquence, et conformément à l'arbitrage pris
avec le propriétaire du projet :

* la branche TREND **n'ouvre aucune position** ;
* elle sert uniquement à **recevoir** l'inventaire transféré par le handoff
  §6.1, qui vit ensuite sous le `TrailingStopAgent` — de l'infrastructure
  validée, et non le signal rejeté.

Autrement dit, le routeur ne « réactive » rien : il empêche la grille de tourner
hors range, et il donne un toit à une position héritée.

**Asymétrie des transitions (§1)** : entrer en RANGE demande `confirm_bars_1h`
bougies 1h consécutives ; sortir vers TREND est immédiat. C'est délibéré —
attendre une confirmation pour ARRÊTER une grille reviendrait à la laisser
tourner pendant la cassure, c'est-à-dire au moment exact où elle perd le plus.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger("sdm.grid.router")


class Engine(Enum):
    NONE = "none"
    GRID = "grid"
    TREND = "trend"


@dataclass(frozen=True)
class Route:
    engine: Engine
    reason: str
    regime: Optional[str] = None
    confirm_count: int = 0
    data: Dict[str, Any] = field(default_factory=dict)

    @property
    def grid_active(self) -> bool:
        return self.engine is Engine.GRID

    @property
    def trend_active(self) -> bool:
        return self.engine is Engine.TREND

    def is_coherent(self) -> bool:
        """Invariant §1 : jamais les deux moteurs en même temps."""
        return not (self.grid_active and self.trend_active)

    def as_log(self) -> Dict[str, Any]:
        return {"engine": self.engine.value, "reason": self.reason,
                "regime": self.regime, "confirm_count": self.confirm_count,
                **self.data}


@dataclass
class RouterState:
    """Persistable : un restart ne doit pas réinitialiser le compteur de
    confirmation, sinon la grille se redéploie trois bougies trop tôt."""

    range_streak: int = 0
    last_bar_ts: int = 0
    current_engine: str = Engine.NONE.value

    def to_json(self) -> Dict[str, Any]:
        return {"range_streak": self.range_streak, "last_bar_ts": self.last_bar_ts,
                "current_engine": self.current_engine}

    @classmethod
    def from_json(cls, raw: Dict[str, Any]) -> "RouterState":
        return cls(range_streak=int(raw.get("range_streak", 0)),
                   last_bar_ts=int(raw.get("last_bar_ts", 0)),
                   current_engine=str(raw.get("current_engine", Engine.NONE.value)))


class StrategyRouter:
    def __init__(self, confirm_bars_1h: int = 3,
                 trend_engine_available: bool = False) -> None:
        """`trend_engine_available=False` par défaut : le moteur de tendance est
        rejeté (entrée n°1 du registre). Le passer à True exigerait un nouveau
        verdict §9 favorable."""
        self.confirm_bars_1h = max(1, confirm_bars_1h)
        self.trend_engine_available = trend_engine_available

    def route(self, state: RouterState, *, regime: Optional[str], bar_ts: int,
              macro_extreme: bool = False,
              has_handoff_position: bool = False) -> Route:
        """Décide du moteur actif pour cette bougie 1h.

        Idempotent sur `bar_ts` : rejouer la même bougie ne fait pas avancer le
        compteur de confirmation — sans quoi un redémarrage en cours d'heure
        avancerait la confirmation d'un cran gratuit.
        """
        data = {"macro_extreme": macro_extreme,
                "has_handoff_position": has_handoff_position}

        if bar_ts > state.last_bar_ts:
            if regime == "range":
                state.range_streak += 1
            else:
                state.range_streak = 0
            state.last_bar_ts = bar_ts

        if macro_extreme:
            # §1 : veto macro ⇒ GridAgent OFF, flatten si position ouverte.
            state.range_streak = 0
            state.current_engine = Engine.NONE.value
            return Route(Engine.NONE, "veto macro EXTREME: tout moteur OFF (§1)",
                         regime, state.range_streak, data)

        if regime == "trend":
            state.current_engine = Engine.TREND.value
            if has_handoff_position:
                return Route(Engine.TREND,
                             "TREND: position héritée du handoff §6.1 gérée par le "
                             "TrailingStopAgent — aucune nouvelle entrée",
                             regime, state.range_streak, data)
            if not self.trend_engine_available:
                return Route(Engine.NONE,
                             "TREND: aucun moteur d'entrée disponible — le "
                             "ConfluenceAgent est REJETÉ (registre n°1)",
                             regime, state.range_streak, data)
            return Route(Engine.TREND, "TREND: moteur de tendance actif",
                         regime, state.range_streak, data)

        if regime == "range":
            if state.range_streak < self.confirm_bars_1h:
                state.current_engine = Engine.NONE.value
                return Route(Engine.NONE,
                             f"RANGE non confirmé: {state.range_streak}/"
                             f"{self.confirm_bars_1h} bougies 1h (§1)",
                             regime, state.range_streak, data)
            state.current_engine = Engine.GRID.value
            return Route(Engine.GRID, "RANGE confirmé: GridAgent actif",
                         regime, state.range_streak, data)

        # CHOP, ou régime indéterminé : les deux OFF.
        state.current_engine = Engine.NONE.value
        return Route(Engine.NONE, f"régime {regime or 'indéterminé'}: les deux moteurs OFF",
                     regime, state.range_streak, data)


__all__ = ["Engine", "Route", "RouterState", "StrategyRouter"]

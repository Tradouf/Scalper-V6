"""
TrailingStopAgent — SPEC §2 et §6.3.

Cité par la spec comme existant ; il n'existait pas dans le repo. Il reçoit la
position dès le fill et gère seul la sortie ; le ConfluenceAgent ne s'en occupe
plus (sauf invalidation de biais, §6.4).

Trailing ATR adaptatif en trois temps, tous exprimés en **R** (multiples du
risque initial `|entrée − stop initial|`), ce qui rend le comportement
indépendant de la volatilité du moment :

* avant `activate_at_r` : le stop initial du §6.2 ne bouge pas ;
* à partir de `breakeven_at_r` : le stop remonte au moins à l'entrée ;
* à partir de `tighten_at_r` : la distance de trailing se resserre de
  `k_trail` à `k_trail_tight`.

Deux invariants tenus par le code :

1. **Le stop ne recule jamais.** Un trailing qui se relâche quand le prix
   revient n'est plus un stop, c'est une espérance.
2. **Le déclenchement se juge sur les extrêmes de bougie, le déplacement sur
   les clôtures.** Trailer sur les mèches ferait sortir sur du bruit
   d'exécution ; ignorer les mèches pour le déclenchement ferait rater des
   stops réellement touchés — et gonflerait le backtest.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from confluence.config import TrailingConfig
from confluence.types import Side


@dataclass
class TrailingState:
    side: Side
    entry: float
    initial_stop: float
    stop: float
    peak: float                      # extrême favorable atteint (close)
    activated: bool = False

    @property
    def risk_unit(self) -> float:
        return abs(self.entry - self.initial_stop)

    def r_multiple(self, price: float) -> float:
        unit = self.risk_unit
        if unit <= 0:
            return 0.0
        return self.side.sign * (price - self.entry) / unit


class TrailingStopAgent:
    name = "trailing"

    def __init__(self, cfg: TrailingConfig) -> None:
        self.cfg = cfg

    def open(self, side: Side, entry: float, initial_stop: float) -> TrailingState:
        return TrailingState(side=side, entry=entry, initial_stop=initial_stop,
                             stop=initial_stop, peak=entry)

    def update(self, state: TrailingState, close: float, atr_1h: float) -> TrailingState:
        """Fait avancer le trailing d'une bougie clôturée. Pur.

        `atr_1h` est réévalué à chaque barre : c'est ce qui rend le trailing
        « adaptatif » — il s'élargit quand le marché s'agite, au lieu de se
        faire arracher par une expansion de volatilité.
        """
        if state.side.sign * (close - state.peak) > 0:
            state.peak = close

        r = state.r_multiple(close)
        if r >= self.cfg.activate_at_r:
            state.activated = True

        candidates = [state.stop]

        if r >= self.cfg.breakeven_at_r:
            candidates.append(state.entry)

        if state.activated and atr_1h > 0:
            k = self.cfg.k_trail_tight if r >= self.cfg.tighten_at_r else self.cfg.k_trail
            candidates.append(state.peak - state.side.sign * k * atr_1h)

        # Le stop ne peut que se resserrer : max pour un long, min pour un short.
        state.stop = max(candidates) if state.side is Side.LONG else min(candidates)
        return state

    def hit(self, state: TrailingState, low: float, high: float) -> Optional[float]:
        """Prix de sortie si le stop a été touché dans la bougie, sinon None.

        Rend le prix du STOP, pas le prix de clôture : la modélisation du
        slippage appartient au backtest (§9.1), pas à l'agent — sinon les deux
        se superposeraient sans qu'on sache lequel compte.
        """
        if state.side is Side.LONG and low <= state.stop:
            return state.stop
        if state.side is Side.SHORT and high >= state.stop:
            return state.stop
        return None


__all__ = ["TrailingState", "TrailingStopAgent"]

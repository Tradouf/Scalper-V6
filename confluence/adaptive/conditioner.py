"""
RegimeConditioner — étage 1a, adaptation temps réel déterministe. SPEC §12.3.

Module le set actif selon le percentile de volatilité ATR 1h, par interpolation
linéaire entre deux bornes portées par le `ParameterSet` lui-même :

| Paramètre       | vol percentile ≤ 30 | vol percentile ≥ 70 |
|-----------------|---------------------|---------------------|
| `k_stop`        | borne basse (1,2)   | borne haute (2,0)   |
| `edge_multiple` | durci (7)           | assoupli (4)        |
| `risk_pct`      | nominal             | réduit (×0,7)       |

**Fonction pure.** Mêmes entrées ⇒ mêmes sorties, aucune I/O, aucune horloge.
C'est la condition posée par le §12.3 pour que le backtest du §9 tourne avec le
conditioner ACTIF plutôt que sur paramètres figés — sans quoi on validerait une
stratégie et on en exécuterait une autre.

**Pourquoi il n'y a pas de boucle de rétroaction.** Le percentile vient de la
couche 1h (§4.2), et les trois paramètres conditionnés appartiennent tous à la
section `risk`, qui n'est lue qu'en aval — au moment du filtre d'edge, du stop
et du sizing. Le conditionnement ne peut donc pas modifier l'entrée dont il
dépend. Cette propriété n'est pas un hasard heureux : `assert_no_feedback()`
la vérifie, et le jour où l'on voudra conditionner un seuil d'ADX, elle
refusera.
"""

from __future__ import annotations

import logging
from typing import Dict, Mapping, Optional, Tuple

logger = logging.getLogger("sdm.confluence.adaptive.conditioner")

# Sections dont la lecture est POSTÉRIEURE au calcul du percentile ATR par la
# couche 1h. Seuls ces préfixes peuvent être conditionnés (cf. docstring).
CONDITIONABLE_PREFIXES = ("risk.", "trailing.")

# Paramètres entiers par nature : les interpoler puis les tronquer donnerait un
# palier invisible plutôt qu'une rampe. On les arrondit explicitement.
INTEGER_PARAMS = ("risk.max_trades_per_day", "risk.max_leverage")


class ConditionerError(ValueError):
    """Bornes de conditionnement incohérentes ou paramètre non conditionnable."""


class RegimeConditioner:
    def __init__(self, vol_percentile_low: float = 30.0,
                 vol_percentile_high: float = 70.0) -> None:
        if vol_percentile_low >= vol_percentile_high:
            raise ConditionerError(
                "vol_percentile_low doit être < vol_percentile_high "
                "(sinon l'interpolation s'inverse et durcit le risque quand le "
                "marché se calme)")
        self.low = float(vol_percentile_low)
        self.high = float(vol_percentile_high)

    def weight(self, vol_percentile: float) -> float:
        """Position dans la rampe, dans [0, 1]. 0 = calme, 1 = agité.

        Clampée aux deux bouts : au-delà du 90e percentile, on ne veut pas
        extrapoler un `k_stop` que personne n'a validé. Les bornes du set sont
        des bornes, pas une pente à prolonger.
        """
        if vol_percentile <= self.low:
            return 0.0
        if vol_percentile >= self.high:
            return 1.0
        return (vol_percentile - self.low) / (self.high - self.low)

    def condition(self, params: Mapping[str, float],
                  vol_percentile: Optional[float],
                  bounds: Optional[Mapping[str, Tuple[float, float]]] = None,
                  ) -> Dict[str, float]:
        """`condition(params, vol_percentile) -> params` du §12.3.

        `vol_percentile is None` (percentile non calculable) rend les paramètres
        INCHANGÉS. Choisir une extrémité par défaut reviendrait à laisser une
        donnée manquante durcir ou relâcher le risque en silence.
        """
        out = dict(params)
        if not bounds:
            return out
        if vol_percentile is None:
            logger.debug("conditioner: percentile indisponible — paramètres inchangés")
            return out

        w = self.weight(float(vol_percentile))
        for path, (at_low, at_high) in bounds.items():
            if not path.startswith(CONDITIONABLE_PREFIXES):
                raise ConditionerError(
                    f"{path} n'est pas conditionnable : seules les sections "
                    f"{CONDITIONABLE_PREFIXES} sont lues APRÈS le calcul du "
                    f"percentile ATR, les autres créeraient une boucle (§12.3)")
            value = float(at_low) + w * (float(at_high) - float(at_low))
            out[path] = round(value) if path in INTEGER_PARAMS else value
        return out

    @staticmethod
    def assert_no_feedback(bounds: Mapping[str, Tuple[float, float]]) -> None:
        """Vérifie qu'aucune borne ne porte sur un paramètre lu AVANT le
        percentile. Appelé à l'enregistrement d'un set, pour que l'erreur sorte
        au moment de la promotion et non trois semaines plus tard en live."""
        bad = [p for p in bounds if not p.startswith(CONDITIONABLE_PREFIXES)]
        if bad:
            raise ConditionerError(
                f"bornes de conditionnement interdites sur {bad}: ces paramètres "
                f"alimentent le calcul dont dépend le conditionnement (§12.3)")


__all__ = ["CONDITIONABLE_PREFIXES", "ConditionerError", "RegimeConditioner"]

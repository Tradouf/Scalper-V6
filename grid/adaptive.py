"""
Intégration APM du GridAgent — SPEC §8.

Les paramètres de grille vivent dans les `ParameterSet` du registre et sont
validés par le même pipeline §12.4 que ceux du ConfluenceAgent. Rien de
spécifique n'est introduit : c'est le même registre, le même conditionnement,
les mêmes plafonds durs.

**La règle qui structure ce fichier** : le conditionnement ne doit JAMAIS
toucher un paramètre qui alimente la détection de régime amont. Le §8 le dit
explicitement — « les seuils ADX/percentile du §2 restent figés — même règle que
pour le ConfluenceAgent ». Toute la section `activation` est donc interdite au
conditionnement, et `assert_no_grid_feedback()` le vérifie à l'enregistrement
plutôt qu'en production.

La raison est la même que pour le candidat n°1, et elle vaut d'être répétée : si
le percentile de volatilité pouvait modifier le seuil qui décide du régime, le
système choisirait le régime dans lequel il préfère se trouver. Une boucle de ce
genre ne se manifeste pas par une erreur, mais par des résultats de backtest
irréproductibles.

**Ce que les postures peuvent et ne peuvent pas** (§8) :

* `defensive` peut réduire l'exposition, ou interdire le déploiement ;
* aucune posture ne peut élargir `max_grid_loss_pct` ni désactiver le §6.1.

Ces deux interdictions sont appliquées en dernier, sur le résultat, par
`apply_grid_caps()` — comme les plafonds durs du §12.5.
"""

from __future__ import annotations

import logging
from typing import Dict, Mapping, Optional, Tuple

from confluence.adaptive.conditioner import RegimeConditioner
from grid.config import GridConfig, GridConfigError

logger = logging.getLogger("sdm.grid.adaptive")

# Sections conditionnables : lues APRÈS le calcul du régime, donc sans boucle.
GRID_CONDITIONABLE_PREFIXES = ("build.", "exits.")

# Sections INTERDITES au conditionnement : elles alimentent la décision de
# régime dont dépend le conditionnement lui-même (§8).
GRID_FROZEN_PREFIXES = ("activation.",)

# Paramètres que nulle posture ne peut relever (§8).
GRID_HARD_CAPS = ("build.max_grid_loss_pct",)


class GridFeedbackError(GridConfigError):
    """Une borne de conditionnement porte sur un paramètre amont du régime."""


def assert_no_grid_feedback(bounds: Mapping[str, Tuple[float, float]]) -> None:
    """Vérifie qu'aucune borne ne crée de boucle de rétroaction (§8).

    Appelé à l'enregistrement d'un ParameterSet : l'erreur sort au moment de la
    promotion, pas trois semaines plus tard en production.
    """
    bad = [p for p in bounds
           if p.startswith(GRID_FROZEN_PREFIXES)
           or not p.startswith(GRID_CONDITIONABLE_PREFIXES)]
    if bad:
        raise GridFeedbackError(
            f"bornes de conditionnement interdites sur {sorted(bad)} : ces paramètres "
            f"alimentent la détection de régime du §2, dont dépend le conditionnement "
            f"lui-même — les seuils ADX et de percentile restent figés (§8)")


# Bornes par défaut, à porter dans les ParameterSet du registre.
# `k_step` monte avec la volatilité : vol plus haute ⇒ pas plus large, donc
# moins de cycles mais chacun plus rentable. `max_net_exposure_frac` descend :
# on porte moins d'inventaire quand le marché s'agite.
DEFAULT_GRID_BOUNDS: Dict[str, Dict[str, Tuple[float, float]]] = {
    "defensive": {"build.k_step": (0.35, 0.60),
                  "build.max_net_exposure_frac": (0.40, 0.25)},
    "neutral": {"build.k_step": (0.30, 0.50),
                "build.max_net_exposure_frac": (0.60, 0.40)},
    "aggressive": {"build.k_step": (0.25, 0.45),
                   "build.max_net_exposure_frac": (0.75, 0.55)},
}


def condition_grid(cfg: GridConfig, vol_percentile: Optional[float],
                   bounds: Optional[Mapping[str, Tuple[float, float]]] = None,
                   conditioner: Optional[RegimeConditioner] = None,
                   ) -> Tuple[GridConfig, list]:
    """Applique le conditionnement §12.3 aux paramètres de grille.

    Rend `(config, notes)`. Ne lève jamais sur un paramètre inapplicable : une
    valeur refusée laisse la config de base en place et produit une note. Le
    GridAgent doit toujours obtenir une configuration valide, comme le §12.8
    l'exige pour le ConfluenceAgent.
    """
    notes: list = []
    if not bounds:
        return cfg, notes
    assert_no_grid_feedback(bounds)

    if vol_percentile is None:
        notes.append("percentile de volatilité indisponible — paramètres inchangés")
        return cfg, notes

    cond = conditioner or RegimeConditioner(30.0, 70.0)
    weight = cond.weight(float(vol_percentile))

    out = cfg
    for path, (at_low, at_high) in bounds.items():
        value = float(at_low) + weight * (float(at_high) - float(at_low))
        try:
            out = out.replace_path(path, value)
        except GridConfigError as exc:
            notes.append(f"paramètre {path} ignoré ({exc})")
            logger.error("APM grille: %s=%r inapplicable — %s", path, value, exc)
    return apply_grid_caps(out, cfg, notes)


def apply_grid_caps(conditioned: GridConfig, nominal: GridConfig,
                    notes: list) -> Tuple[GridConfig, list]:
    """Plafonds durs du §8, appliqués EN DERNIER sur le résultat.

    Aucune posture ne peut élargir `max_grid_loss_pct` ni désactiver la sortie de
    cassure. On le vérifie plutôt que de le supposer : une borne saisie à
    l'envers dans un ParameterSet produirait une grille qui accepte de perdre
    davantage quand le marché s'agite — exactement l'inverse de l'intention.
    """
    out = conditioned
    for path in GRID_HARD_CAPS:
        current = out.get_path(path)
        ceiling = nominal.get_path(path)
        if current > ceiling:
            notes.append(f"{path} conditionné à {current:.4f} au-dessus du nominal "
                         f"{ceiling:.4f} — plafonné (§8)")
            out = out.replace_path(path, ceiling)

    if not out.exits.breakout_handoff and nominal.exits.breakout_handoff:
        # Désactiver le handoff est permis (c'est le résultat possible de l'A/B
        # §9.5) ; désactiver la SORTIE de cassure ne l'est pas — et il n'existe
        # aucun paramètre pour le faire, ce que ce commentaire documente.
        notes.append("breakout_handoff désactivé — la sortie §6.1 reste active")
    return out, notes


def posture_allows_deployment(posture: str, degraded: bool) -> Tuple[bool, str]:
    """§8 : `defensive` peut interdire le déploiement. Aucune posture ne peut
    l'imposer si l'APM tourne dégradé."""
    if degraded:
        return False, ("APM dégradé (set de repli jamais validé) — pas de déploiement "
                       "de grille")
    return True, f"posture {posture}: déploiement autorisé"


def grid_params_for_registry(cfg: GridConfig) -> Dict[str, float]:
    """Paramètres de grille à porter dans un `ParameterSet` (§8).

    Volontairement restreint aux paramètres que le §8 cite : le registre n'a pas
    vocation à devenir une copie du YAML, et chaque paramètre supplémentaire est
    une dimension de plus dans l'espace d'optimisation — donc une chance de plus
    de trouver du bruit.
    """
    return {
        "build.k_step": cfg.build.k_step,
        "build.grid_edge_multiple": cfg.build.grid_edge_multiple,
        "build.max_grid_loss_pct": cfg.build.max_grid_loss_pct,
        "build.max_levels": float(cfg.build.max_levels),
        "build.range_lookback_bars_15m": float(cfg.build.range_lookback_bars_15m),
        "build.max_net_exposure_frac": cfg.build.max_net_exposure_frac,
    }


__all__ = ["DEFAULT_GRID_BOUNDS", "GRID_CONDITIONABLE_PREFIXES", "GRID_FROZEN_PREFIXES",
           "GRID_HARD_CAPS", "GridFeedbackError", "apply_grid_caps", "assert_no_grid_feedback",
           "condition_grid", "grid_params_for_registry", "posture_allows_deployment"]

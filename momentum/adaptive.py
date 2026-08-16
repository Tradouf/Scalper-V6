"""
Intégration APM du MomentumAgent — SPEC §8, volontairement **minimale**.

Une seule chose est conditionnable : `gross_exposure_frac`, et uniquement à la
BAISSE. Tout le reste — `lookback_d`, `skip_d`, `n_legs`, l'univers, la
fréquence de rebalancement — est hors de portée.

**Pourquoi cette frontière est plus stricte que pour les candidats précédents.**
Chez le GridAgent, l'interdiction visait une boucle de rétroaction : un
paramètre conditionné ne devait pas influencer la détection de régime dont
dépendait le conditionnement. Ici il n'y a aucune boucle à craindre — le
MomentumAgent n'a pas de détection de régime (c'est délibéré, §0 des
dépendances). L'interdiction a un autre fondement, plus fondamental :

    un signal dont les paramètres bougent n'est plus le signal qu'on a gelé.

L'entrée n°3 du registre enregistre une hypothèse précise : « le classement des
rendements sur 21 jours, en excluant les 2 derniers, prédit-il les rendements
relatifs futurs ? ». Un lookback qui s'adapterait au marché testerait une autre
hypothèse — non enregistrée, non gelée, non couverte par le seuil de Bonferroni.
Le hash cesserait de décrire ce qui tourne.

C'est aussi pourquoi le §10 range explicitement l'optimisation du lookback hors
périmètre : « la sensibilité §9.3 le sonde, elle ne le choisit pas ».
"""

from __future__ import annotations

import logging
from typing import Dict, List, Mapping, Tuple

from momentum.config import (
    CONDITIONABLE,
    MomentumConfig,
    SignalConditioningError,
    assert_no_signal_conditioning,
)

logger = logging.getLogger("sdm.momentum.adaptive")

# §8 : la posture `defensive` peut réduire l'exposition. Les facteurs sont des
# MULTIPLICATEURS de `gross_exposure_frac`, jamais des valeurs absolues — ainsi
# une posture ne peut pas contourner le nominal du set validé.
POSTURE_GROSS_FACTOR: Dict[str, float] = {
    "defensive": 0.5,
    "neutral": 1.0,
    "aggressive": 1.0,      # AUCUNE augmentation : le §8 ne l'autorise pas
}


def apply_posture(cfg: MomentumConfig, posture: str,
                  degraded: bool = False) -> Tuple[MomentumConfig, List[str]]:
    """Applique une posture. Rend `(config, notes)`.

    `aggressive` ne relève RIEN. Le §8 permet à `defensive` de réduire
    l'exposition, et se tait sur l'augmentation — un silence qui se lit comme une
    interdiction, pas comme une permission. Le tirage du §9 se fait à gross
    100 %, donc toute valeur supérieure sortirait de ce qui a été validé.
    """
    notes: List[str] = []
    factor = POSTURE_GROSS_FACTOR.get(posture)
    if factor is None:
        notes.append(f"posture inconnue {posture!r} — traitée comme neutral")
        factor = 1.0

    if degraded:
        notes.append("APM dégradé (set jamais validé) — exposition réduite de moitié")
        factor = min(factor, 0.5)

    if factor >= 1.0:
        return cfg, notes

    nominal = cfg.portfolio.gross_exposure_frac
    reduced = nominal * factor
    notes.append(f"posture {posture}: gross_exposure_frac {nominal:.2f} → {reduced:.2f}")
    return cfg.replace_path("portfolio.gross_exposure_frac", reduced), notes


def validate_conditioning_plan(paths: Mapping[str, float]) -> None:
    """Vérifie qu'un plan de conditionnement ne touche que l'autorisé (§8).

    Appelé avant d'enregistrer un ParameterSet, comme
    `assert_no_grid_feedback()` chez le candidat n°2 : l'erreur doit sortir à la
    promotion, pas trois semaines plus tard en production.
    """
    assert_no_signal_conditioning(list(paths))
    for path, value in paths.items():
        if path == "portfolio.gross_exposure_frac" and value > 1.0:
            raise SignalConditioningError(
                f"{path}={value} : le §8 permet de RÉDUIRE l'exposition, pas de "
                f"l'augmenter. Le tirage §9 s'est fait à gross 100 % — au-delà, "
                f"on exécute autre chose que ce qui a été validé")


def params_for_registry(cfg: MomentumConfig) -> Dict[str, float]:
    """Paramètres à porter dans un ParameterSet.

    Restreint à ce que le §8 autorise à bouger. Le signal n'y figure pas : ce
    n'est pas un oubli, c'est le sens de la section — le registre ne doit pas
    même offrir la possibilité de le régler.
    """
    return {"portfolio.gross_exposure_frac": cfg.portfolio.gross_exposure_frac}


def conditionable_paths() -> Tuple[str, ...]:
    return CONDITIONABLE


__all__ = ["POSTURE_GROSS_FACTOR", "apply_posture", "conditionable_paths",
           "params_for_registry", "validate_conditioning_plan"]

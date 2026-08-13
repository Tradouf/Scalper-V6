"""
AdaptiveParameterManager — seuils adaptatifs à deux étages. SPEC §12.

Propriétaire exclusif des valeurs effectives des paramètres du §7. Le
ConfluenceAgent ne lit plus le YAML directement : il interroge l'APM à chaque
cycle d'évaluation.

**Principe cardinal (§12)** : aucun LLM ne produit jamais de valeur numérique.
Les nombres viennent exclusivement de l'optimisation statistique (étage 1). Le
LLM (étage 2) ne fait que CHOISIR entre trois jeux de paramètres déjà validés,
et ce choix est lui-même borné par un ratchet asymétrique et un shadow mode.

Ce partage n'est pas de la prudence rituelle : un LLM qui produit un `k_stop`
produit un nombre que rien n'a validé sur des données, et qui passera donc au
travers du §9 — le seul dispositif du projet capable de dire non.

    étage 1a  RegimeConditioner    déterministe, temps réel, backtestable
    étage 1b  WalkForwardOptimizer déterministe, mensuel, hors chemin critique
    étage 2   PostureSelector      LLM, quotidien, choix ternaire borné
"""

from confluence.adaptive.conditioner import RegimeConditioner  # noqa: F401
from confluence.adaptive.manager import AdaptiveParameterManager  # noqa: F401
from confluence.adaptive.optimizer import (  # noqa: F401
    OptimizerReport,
    WalkForwardOptimizer,
)
from confluence.adaptive.posture import (  # noqa: F401
    LLMBackend,
    Posture,
    PostureAdvice,
    PostureSelector,
)
from confluence.adaptive.registry import (  # noqa: F401
    FALLBACK_NEUTRAL,
    ParameterSet,
    ParamRegistry,
    RegistryError,
)

__all__ = [
    "AdaptiveParameterManager", "FALLBACK_NEUTRAL", "LLMBackend",
    "OptimizerReport", "ParamRegistry", "ParameterSet", "Posture",
    "PostureAdvice", "PostureSelector", "RegimeConditioner", "RegistryError",
    "WalkForwardOptimizer",
]

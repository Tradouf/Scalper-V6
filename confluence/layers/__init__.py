"""
Les quatre couches — SPEC §4.

Chaque couche est un **filtre veto** : elle ne peut que refuser ou laisser
passer. Aucune ne peut décider seule d'entrer. C'est le principe cardinal du
§1 (« le défaut est l'inaction ») traduit en types : `evaluate()` rend un
`LayerVerdict`, jamais un ordre.

Chaque `evaluate(candles, context)` est **pure** (§8) : mêmes entrées ⇒ même
sortie, aucune I/O, aucune horloge lue en douce. Le `now_ms` vient toujours du
`LayerContext`. C'est ce qui permet au backtest §9 d'exécuter exactement le
même code que le live, et aux tests de rejouer l'historique barre par barre.
"""

from confluence.layers.bias import BiasLayer          # noqa: F401
from confluence.layers.context import LayerContext    # noqa: F401
from confluence.layers.execution import (             # noqa: F401
    ExecutionLayer,
    ExecutionOutcome,
    ExecutionPlan,
)
from confluence.layers.regime import RegimeLayer      # noqa: F401
from confluence.layers.timing import TimingLayer      # noqa: F401

__all__ = [
    "BiasLayer", "ExecutionLayer", "ExecutionOutcome", "ExecutionPlan",
    "LayerContext", "RegimeLayer", "TimingLayer",
]

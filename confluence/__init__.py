"""
ConfluenceAgent — confluence multi-timeframe déterministe (SPEC V8).

Quatre horizons, chacun filtre à veto : 1d donne le biais, 1h le régime, 15m le
timing, 1m l'exécution seule. Toute couche non alignée ⇒ pas de trade ; le
défaut est l'inaction.

Différence avec `agents/multi_tf.py` (confluence H1/M15/M1 déjà en prod V7) :
là-bas chaque strate interroge un LLM, donc ni reproductible ni backtestable.
Ici tout est déterministe et sans I/O dans la décision — c'est ce qui rend le
protocole de validation §9 (walk-forward + sensibilité + placebo) exécutable.

Rien de ce module ne doit toucher le mainnet avant que `confluence/run.py
validate` n'ait rendu un verdict conforme aux critères d'acceptation §9.4.
"""

from confluence.types import (  # noqa: F401 — API publique
    Bias,
    ConfluenceSignal,
    LayerVerdict,
    Regime,
    Side,
)

__all__ = ["Bias", "ConfluenceSignal", "LayerVerdict", "Regime", "Side"]

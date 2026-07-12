"""
Interface Sleeve — le contrat que chaque stratégie SuperBot implémente.

Une sleeve fournit :
  - sa grille de paramètres (vide si params figés, ex. momentum) ;
  - ses signaux (+1 / -1 / 0 par bougie, causaux) ;
  - sa politique de sortie (TP/SL en ATR, time-exit éventuel, TP présent ou non).

Le backtester unifié (superbot/backtester.py) et — en Phase 2 — l'orchestrateur
ne connaissent QUE ce contrat : ajouter une sleeve n'exige de toucher ni au
moteur ni au walk-forward.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class ExitPolicy:
    """Sorties d'une position pour une combinaison de paramètres donnée."""
    tp_atr: Optional[float]        # None = PAS de take-profit (ex. momentum)
    sl_atr: float                  # SL natif obligatoire pour toutes les sleeves
    atr_len: int = 14
    time_exit_bars: Optional[int] = None   # None = pas de time-exit


class Sleeve(ABC):
    """Contrat minimal d'une stratégie."""

    #: nom court, sert de clé partout (best_params, allocations, dashboards)
    name: str = "base"
    #: timeframes que l'optimiseur doit tester pour cette sleeve
    timeframes: tuple = ()
    #: True si la grille est optimisée par walk-forward (False = params figés)
    optimizable: bool = True

    @abstractmethod
    def grid(self) -> List[object]:
        """Liste des jeux de paramètres à explorer (objets opaques pour le
        moteur ; la sleeve sait les interpréter)."""

    @abstractmethod
    def signals(self, candles: List[dict], params: object) -> List[int]:
        """Signaux causaux par bougie : +1 ouvrir long, -1 ouvrir short, 0 rien."""

    @abstractmethod
    def exit_policy(self, params: object) -> ExitPolicy:
        """Politique de sortie associée à ce jeu de paramètres."""

    @abstractmethod
    def params_to_dict(self, params: object) -> dict:
        """Sérialisation JSON des paramètres pour best_params.json."""

    @abstractmethod
    def params_from_dict(self, d: dict) -> object:
        """Désérialisation depuis best_params.json (rechargement live)."""

    def warmup_bars(self, params: object) -> int:
        """Bougies à ignorer en début de fenêtre (indicateurs non convergés)."""
        return 50

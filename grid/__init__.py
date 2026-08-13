"""
GridAgent — grille Long/Short maker sur BTC-PERP. SPEC GridAgent.

Candidat n°2 du registre des hypothèses (`hypotheses/REGISTRY.md`), enregistré
avant tout tirage, seuil placebo α = 0,025 (Bonferroni, n = 2).

**Le cadrage honnête du §0, qui gouverne tout le reste.** Une grille est une
stratégie SHORT-VOLATILITÉ : elle encaisse de petits gains fréquents en range
et subit sa perte maximale au moment exact où le prix sort du range. Il
n'existe pas de grille gagnante par construction. L'edge, s'il existe, vient de
trois choses et uniquement de trois :

1. ne tourner qu'en régime RANGE — **le filtre de régime EST la stratégie**, la
   grille n'est que l'exécution ;
2. un espacement qui couvre largement les frais maker, sans quoi la grille est
   une machine à payer l'exchange ;
3. une sortie non négociable quand le range casse — la quasi-totalité des pertes
   des grilles vient d'un inventaire conservé hors range.

Le module est donc écrit de façon que ces trois points soient les plus durs à
contourner : l'activation refuse par défaut (§2), l'espacement a un plancher de
frais infranchissable (§3.2), et la cassure (§6.1) est le seul chemin du système
— avec le flatten de régime — où un ordre au marché est autorisé.

**Comptabilité (§7)** : la seule métrique de décision est `net_mtm_pnl`. Le PnL
réalisé d'une grille est positif par construction ; le mettre en avant est un
instrument d'auto-illusion, et c'est précisément ainsi que des grilles
« gagnantes » perdent de l'argent.
"""

from grid.accounting import GridAccounting, SessionPnL  # noqa: F401
from grid.build import GridPlan, build_grid, detect_range  # noqa: F401
from grid.config import GridConfig, load  # noqa: F401
from grid.router import Route, StrategyRouter  # noqa: F401
from grid.types import (  # noqa: F401
    ActivationVerdict,
    Fill,
    GridLevel,
    HandoffPlan,
    StopReason,
)

__all__ = [
    "ActivationVerdict", "Fill", "GridAccounting", "GridConfig", "GridLevel",
    "GridPlan", "HandoffPlan", "Route", "SessionPnL", "StopReason",
    "StrategyRouter", "build_grid", "detect_range", "load",
]

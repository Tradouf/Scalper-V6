"""
Interfaces V7 — Protocols pour le couplage faible entre couches.

Chaque module concret (ex: regime/detector.py, strategies/grid.py) implémente
le Protocol correspondant. Aucun module n'importe une classe concrète d'un
autre module sauf cas explicite (composition root dans main.py).
"""
from __future__ import annotations

from typing import Protocol

from core.types import (
    Fill,
    MarketSnapshot,
    RegimeState,
    Signal,
    TargetPortfolio,
)


# Placeholders typés pour des concepts qui auront leur propre fichier en P4-P6.
# On les déclare ici pour que les Protocols restent type-checkables sans cycle
# d'imports. Implémentations concrètes en P3-P6.


class Portfolio(Protocol):
    """Vue agrégée du portefeuille courant (positions live + cash)."""

    @property
    def positions(self) -> dict[str, float]:  # asset → notional signé
        ...

    @property
    def equity(self) -> float:
        ...


class RiskState(Protocol):
    """État courant pour les décisions de Risk Manager (drawdown, etc)."""

    @property
    def current_drawdown(self) -> float:  # [0, 1] (ex: 0.05 = -5%)
        ...

    @property
    def equity(self) -> float:
        ...


class Order(Protocol):
    """Ordre prêt à être soumis à l'exchange."""

    @property
    def asset(self) -> str: ...
    @property
    def side(self) -> str: ...  # 'buy' | 'sell'
    @property
    def qty(self) -> float: ...
    @property
    def order_type(self) -> str: ...  # 'market' | 'limit'
    @property
    def price(self) -> float | None: ...  # None pour market
    @property
    def reduce_only(self) -> bool: ...
    @property
    def strategy_id(self) -> str | None: ...  # pour attribution


# ─── Protocols cœur ──────────────────────────────────────────────────────────


class RegimeDetector(Protocol):
    """Détecte le régime de marché à partir du MarketSnapshot.

    Doit garantir : aucun look-ahead (test no-leak en P1).
    """

    def detect(self, market: MarketSnapshot) -> RegimeState: ...


class StrategyAgent(Protocol):
    """Une stratégie de trading. Produit des Signal sans connaître le régime
    ni les poids d'allocation (régime-agnostique).

    on_fill() est appelé quand un fill attribué à cette stratégie est confirmé,
    pour mettre à jour les stops / l'état interne.
    """

    @property
    def strategy_id(self) -> str: ...

    def generate_signals(self, market: MarketSnapshot) -> list[Signal]: ...

    def on_fill(self, fill: Fill) -> None: ...


class Allocator(Protocol):
    """Combine signaux + régime + perf scores → TargetPortfolio.

    perf_scores : map strategy_id → multiplicateur borné [MULT_MIN, MULT_MAX].
    """

    def allocate(
        self,
        signals: list[Signal],
        regime: RegimeState,
        current_portfolio: Portfolio,
        perf_scores: dict[str, float],
    ) -> TargetPortfolio: ...


class RiskManager(Protocol):
    """Projette le portefeuille cible sur les contraintes dures (caps, DD).
    Peut renvoyer un portfolio cible modifié (réduit) ou vide (kill switch).
    """

    def project(
        self,
        target: TargetPortfolio,
        state: RiskState,
    ) -> TargetPortfolio: ...

    def kill_switch_triggered(self, state: RiskState) -> bool: ...


class ExecutionEngine(Protocol):
    """Traduit un TargetPortfolio en ordres à soumettre. Applique la bande de
    non-trade (pas d'ordre si l'écart est inférieur au seuil).
    """

    def reconcile(
        self,
        target: TargetPortfolio,
        current: Portfolio,
    ) -> list[Order]: ...

    def submit(self, orders: list[Order]) -> list[Fill]: ...


# ─── Bus de fills (publish-subscribe interne) ────────────────────────────────


class FillBus(Protocol):
    """Distribue les fills aux abonnés (stratégies, allocation.performance,
    monitoring). Pas de garantie d'ordre cross-souscripteurs."""

    def publish(self, fill: Fill) -> None: ...

    def subscribe(self, callback) -> None: ...  # callback: Callable[[Fill], None]

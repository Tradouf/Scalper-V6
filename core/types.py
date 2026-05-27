"""
Contrats de données V7 — dataclasses immuables échangées entre couches.

Aucune logique métier ici. Tout objet créé est valide par construction (validation
en __post_init__ pour les invariants critiques : somme des probas, bornes,
direction signée).
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ─── Régimes de marché ───────────────────────────────────────────────────────


class Regime(Enum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    HIGH_VOL = "high_vol"  # stress / crise


@dataclass(frozen=True)
class RegimeState:
    """Sortie du détecteur de régime à l'instant t.

    probabilities doit sommer à 1.0 (±1e-6 tolérance).
    label = argmax pour le logging ; les consommateurs (allocateur) doivent
    utiliser probabilities pour les calculs (régime doux).
    """

    timestamp: dt.datetime
    probabilities: dict[Regime, float]
    label: Regime
    confidence: float  # ex. proba du label dominant

    def __post_init__(self) -> None:
        # Validation : toutes les Regimes présentes, sum ≈ 1, valeurs [0, 1]
        missing = set(Regime) - set(self.probabilities.keys())
        if missing:
            raise ValueError(f"RegimeState : probabilités manquantes pour {missing}")
        for r, p in self.probabilities.items():
            if not (0.0 <= p <= 1.0 + 1e-9):
                raise ValueError(f"RegimeState : proba {r}={p} hors [0,1]")
        s = sum(self.probabilities.values())
        if not math.isclose(s, 1.0, abs_tol=1e-6):
            raise ValueError(f"RegimeState : somme probas = {s}, attendu 1.0")
        if not (0.0 <= self.confidence <= 1.0 + 1e-9):
            raise ValueError(f"RegimeState : confidence={self.confidence} hors [0,1]")


# ─── Signaux de stratégie ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Signal:
    """Sortie d'un StrategyAgent à l'instant t, AVANT allocation.

    direction         : conviction signée dans [-1, +1] (LONG > 0, SHORT < 0)
    target_notional   : taille souhaitée AVANT allocation, en USD (toujours ≥ 0)
    expected_edge_bps : edge attendu NET des coûts estimés (bps)
    confidence        : [0, 1]
    stop_price        : niveau de stop suggéré (None si non applicable)
    horizon_bars      : durée de détention attendue (en bars du timeframe stratégie)
    """

    strategy_id: str
    asset: str
    direction: float
    target_notional: float
    expected_edge_bps: float
    confidence: float
    stop_price: Optional[float]
    horizon_bars: int
    timestamp: dt.datetime

    def __post_init__(self) -> None:
        if not (-1.0 - 1e-9 <= self.direction <= 1.0 + 1e-9):
            raise ValueError(f"Signal direction={self.direction} hors [-1,1]")
        if self.target_notional < 0:
            raise ValueError(f"Signal target_notional={self.target_notional} négatif")
        if not (0.0 <= self.confidence <= 1.0 + 1e-9):
            raise ValueError(f"Signal confidence={self.confidence} hors [0,1]")
        if self.horizon_bars < 0:
            raise ValueError(f"Signal horizon_bars={self.horizon_bars} négatif")
        if not self.strategy_id:
            raise ValueError("Signal strategy_id vide")
        if not self.asset:
            raise ValueError("Signal asset vide")


# ─── Portefeuille cible (sortie allocateur) ──────────────────────────────────


@dataclass(frozen=True)
class TargetPosition:
    """Position cible par actif après allocation.

    target_notional : SIGNÉ (USD). Positif=long, négatif=short, 0=flat.
    contributing_strategies : map strategy_id → notional contribué (signé).
        La somme |·| n'est pas forcément égale à |target_notional| à cause des
        compensations possibles (long de strat A + short de strat B sur même
        asset → net plus petit). Sert à l'attribution PnL.
    """

    asset: str
    target_notional: float
    contributing_strategies: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.asset:
            raise ValueError("TargetPosition asset vide")


@dataclass(frozen=True)
class TargetPortfolio:
    """Portefeuille cible produit par l'allocateur. Sera projeté par RiskManager
    puis traduit en ordres par ExecutionEngine."""

    timestamp: dt.datetime
    positions: list[TargetPosition]
    gross_exposure: float  # Σ |notional|
    net_exposure: float    # Σ notional (signé)

    def __post_init__(self) -> None:
        if self.gross_exposure < -1e-9:
            raise ValueError(f"TargetPortfolio gross_exposure négatif: {self.gross_exposure}")


# ─── Fills (retour exchange, alimente attribution) ───────────────────────────


@dataclass(frozen=True)
class Fill:
    """Un fill exécuté côté exchange. Alimente :
      - allocation.performance (scoring perf par stratégie)
      - strategies (mise à jour stops, état interne)
      - risk (suivi exposition réelle)
    """

    order_id: str
    asset: str
    notional: float        # SIGNÉ (USD)
    price: float
    fee: float             # ≥ 0
    strategy_id: Optional[str]  # None si non attribué (cleanup, ext intervention)
    timestamp: dt.datetime

    def __post_init__(self) -> None:
        if not self.order_id:
            raise ValueError("Fill order_id vide")
        if not self.asset:
            raise ValueError("Fill asset vide")
        if self.fee < 0:
            raise ValueError(f"Fill fee négative: {self.fee}")
        if self.price <= 0:
            raise ValueError(f"Fill price non strictement positif: {self.price}")


# ─── Snapshot marché (entrée du détecteur de régime + stratégies) ────────────


@dataclass(frozen=True)
class Candle:
    """Une bougie OHLCV."""

    ts_open: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class MarketSnapshot:
    """Vue du marché à l'instant t pour TOUS les actifs suivis.

    candles : map asset → liste de Candle (dernières N bars, ordre chrono ascendant).
              Le timeframe est implicite (1h par défaut côté V7).
    funding_rates : map asset → funding rate courant (taux/8h sur HL).
    prices : map asset → mark price spot/perp courant.
    """

    timestamp: dt.datetime
    candles: dict[str, list[Candle]]
    prices: dict[str, float]
    funding_rates: dict[str, float] = field(default_factory=dict)

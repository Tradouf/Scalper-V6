"""
Statistiques pour mean reversion : z-score rolling + half-life d'Ornstein-Uhlenbeck.

Pas de dépendance statsmodels (volontairement) — on se contente d'estimer θ par
régression OLS sur (X_{t-1}, ΔX_t). Si la half-life est négative ou très grande,
la série est non-stationnaire (= équivalent fonctionnel d'un ADF p-value élevé).
"""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np


def zscore(prices: Sequence[float], window: int = 50) -> Optional[float]:
    """Z-score du dernier prix sur la fenêtre.

    Returns None si pas assez de données ou std nulle (= série dégénérée).
    """
    if prices is None:
        return None
    arr = np.asarray(prices, dtype=float)
    if arr.size < window:
        return None
    seg = arr[-window:]
    mu = float(seg.mean())
    sd = float(seg.std(ddof=1))
    if sd <= 1e-12:
        return None
    return float((arr[-1] - mu) / sd)


def half_life(prices: Sequence[float]) -> Optional[float]:
    """Half-life du retour à la moyenne (Ornstein-Uhlenbeck).

    Pour un processus dX_t = θ(μ − X_t)dt + σdW_t,
    half-life = −ln(2) / β où β est le coefficient OLS de ΔX_t sur X_{t-1}.

    Returns:
        - None si données insuffisantes ou régression dégénérée
        - valeur positive si série mean-reverting (en nombre de périodes)
        - valeur négative si série explosive (= non-stationnaire, à rejeter)
    """
    if prices is None:
        return None
    arr = np.asarray(prices, dtype=float)
    if arr.size < 10:
        return None

    lag = arr[:-1]
    delta = arr[1:] - lag
    if lag.size < 5:
        return None

    # OLS sur Δx = β·x_{t-1} + α  → β = cov(lag, delta) / var(lag)
    var_lag = float(np.var(lag, ddof=1))
    if var_lag <= 1e-12:
        return None
    beta = float(np.cov(lag, delta, ddof=1)[0, 1] / var_lag)

    if abs(beta) <= 1e-12:
        return None
    # β positif = série explosive (anti mean-reverting) → half-life négative.
    return float(-np.log(2.0) / beta)


def is_mean_reverting(prices: Sequence[float], min_hl: float = 5.0, max_hl: float = 48.0) -> bool:
    """Heuristique : la série est exploitable en mean-reversion si la half-life
    est dans une fenêtre raisonnable [min_hl, max_hl] périodes."""
    hl = half_life(prices)
    if hl is None or hl <= 0:
        return False
    return min_hl <= hl <= max_hl


def rolling_mean_std(prices: Sequence[float], window: int = 50) -> tuple[Optional[float], Optional[float]]:
    """Moyenne et écart-type rolling de la fenêtre. None si pas assez de données."""
    arr = np.asarray(prices, dtype=float)
    if arr.size < window:
        return None, None
    seg = arr[-window:]
    return float(seg.mean()), float(seg.std(ddof=1))

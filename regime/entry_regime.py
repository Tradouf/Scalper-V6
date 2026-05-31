"""
Entry regime classifier — port V6 Fix 9 (PROTOTYPE).

Helper utilitaire de classification du régime à l'entrée d'un trade, basé
sur la pente des N dernières bougies AVANT l'entrée (pas de look-ahead).

Sémantique :
  "trend"  → pente alignée avec le side ET |pente| > REGIME_TREND_SLOPE_MIN
             Stratégie : trail large, pas de TP fixe (laisser courir).
  "range"  → tout le reste (pente faible OU mal alignée avec le side).
             Stratégie : TP/SL statiques, AUCUN trail.
  "off"    → flag REGIME_GATED_TRAIL désactivé → comportement historique
             (les stratégies trail comme avant). C'est le défaut.

Validation côté V6 : backtest backtest_regime_trail.py (71/177 trades, 5m),
stable sur tout le sweep de seuil 0.001→0.015. Levier principal : NE PAS
trailer en range. Optimum arm 1.5% / drop 1.0%.

V7 : ne PAS activer (REGIME_GATED_TRAIL=True) avant d'avoir rejoué le
backtest sur le 1m loggé par data/ohlc_1m/ (accumulation depuis 30/05).
"""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np


def slope_pct(closes: Sequence[float], n: int = 12) -> Optional[float]:
    """Pente normalisée des N dernières closes : (close[-1] - close[-n]) / close[-n].
    Retourne None si moins de N bougies."""
    arr = np.asarray(closes, dtype=float)
    if arr.size < n or n < 2:
        return None
    base = arr[-n]
    if base <= 0:
        return None
    return float((arr[-1] - base) / base)


def classify_entry_regime(
    closes_pre_entry: Sequence[float],
    side: str,
    slope_min: float = 0.003,
    bars: int = 12,
    enabled: bool = False,
) -> str:
    """Retourne "off" | "range" | "trend".

    Args:
      closes_pre_entry : closes des bougies AVANT l'entrée (pas inclure le bar
                         d'entrée pour éviter le look-ahead).
      side : "buy" ou "sell" (direction de la position).
      slope_min : magnitude minimale de pente pour qualifier "trend"
                  (REGIME_TREND_SLOPE_MIN, défaut 0.003 = 0.3%).
      bars : nombre de bougies pour le calcul de pente (défaut 12).
      enabled : si False → retourne "off" (comportement historique).

    Sémantique side : "buy" attend une pente >0, "sell" une pente <0. Une
    pente du mauvais côté = non-aligned = "range" (le trail large n'aide pas).
    """
    if not enabled:
        return "off"
    s = slope_pct(closes_pre_entry, n=bars)
    if s is None:
        return "range"  # données insuffisantes : safe par défaut
    aligned = (side.lower() == "buy" and s > 0) or (side.lower() == "sell" and s < 0)
    if aligned and abs(s) > slope_min:
        return "trend"
    return "range"

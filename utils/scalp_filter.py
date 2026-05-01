"""
Filtre pré-LLM — élimine les symboles non éligibles au scalping +1%
SANS appel LLM. Ultra-rapide (<1ms). Appelé avant tout LLM.
"""
from __future__ import annotations
import logging
from typing import Dict, Optional, Tuple

from config.settings import (
    SCALP_MIN_ATR_PCT,
    SCALP_MIN_SR_DIST,
    MAX_SPREAD_PCT,
)

logger = logging.getLogger("sdm.scalp_filter")


def scalp_eligible(
    technical: Dict,
    ob: Dict,
    side: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Vérifie si un symbole est éligible au scalping +1%.

    Args:
        technical : résultat de AgentTechnical.analyze() — contient
                    indicators{price, atr, key_levels{support, resistance}}
        ob        : résultat de AgentOrderbook.analyze()
        side      : "buy" | "sell" | None (si None, vérifie dans les 2 sens)

    Returns:
        (True, "") si éligible
        (False, "raison") si non éligible
    """
    ind = technical.get("indicators", {})
    if not ind:
        return False, "pas d'indicateurs techniques"

    price = ind.get("price", 0)
    if price <= 0:
        return False, "prix invalide"

    # 1. Volatilité : ATR% ≥ seuil
    atr = ind.get("atr", 0)
    atr_pct = atr / price if price > 0 else 0
    if atr_pct < SCALP_MIN_ATR_PCT:
        return False, f"ATR% trop faible ({atr_pct:.4f} < {SCALP_MIN_ATR_PCT})"

    # 2. Distance S/R ≥ seuil dans le sens du trade
    key_levels = technical.get("key_levels", {})
    support    = key_levels.get("support", 0)
    resistance = key_levels.get("resistance", 0)

    if side in ("buy", None) and resistance > 0:
        dist_res = (resistance - price) / price
        if dist_res < SCALP_MIN_SR_DIST:
            if side == "buy":
                return False, f"résistance trop proche pour LONG ({dist_res:.4f} < {SCALP_MIN_SR_DIST})"

    if side in ("sell", None) and support > 0:
        dist_sup = (price - support) / price
        if dist_sup < SCALP_MIN_SR_DIST:
            if side == "sell":
                return False, f"support trop proche pour SHORT ({dist_sup:.4f} < {SCALP_MIN_SR_DIST})"

    # 3. Spread carnet
    spread_pct = ob.get("spread_pct", 999)
    if spread_pct > MAX_SPREAD_PCT:
        return False, f"spread trop large ({spread_pct:.5f} > {MAX_SPREAD_PCT})"

    # 4. Liquidité carnet
    if not ob.get("is_liquid_enough", False):
        return False, "liquidité carnet insuffisante"

    return True, ""


def compute_consensus(bull: Dict, bear: Dict) -> Tuple[float, str]:
    """
    Calcule un score de consensus directionnel à partir des outputs Bull/Bear.

    Returns:
        (score 0.0-1.0, direction "buy"|"sell"|"wait")
    """
    bull_conf = bull.get("confidence", 0.0)
    bear_conf = bear.get("confidence", 0.0)
    bull_timing = bull.get("entry_timing", "wait")
    bear_risk   = bear.get("risk_level", "high")

    # Score net Bull
    net = bull_conf - bear_conf

    # Malus si bear dit high risk
    if bear_risk == "high":
        net -= 0.15
    elif bear_risk == "low":
        net += 0.10

    # Bonus si bull dit "now"
    if bull_timing == "now":
        net += 0.05

    if net >= 0.20:
        return min(bull_conf, 0.95), "buy"
    elif net <= -0.20:
        return min(bear_conf, 0.95), "sell"
    else:
        return abs(net), "wait"

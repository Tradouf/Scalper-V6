"""Filtre quantitatif pré-LLM — score 0-100, zéro appel LLM."""

from __future__ import annotations

import logging
from typing import Dict, List, Tuple

from llmbot import config
from llmbot.indicators import compute_snapshot

logger = logging.getLogger("sdm.llmbot.scanner")

MIN_ATR_PCT = 0.006
MIN_SR_DIST = 0.012
MAX_SPREAD_PCT = 0.001


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def score_setup(tech: dict, ob: dict, side_hint: str) -> Tuple[float, str, str]:
    """
    Score un setup directionnel. Retourne (score, direction, raison).
    direction : 'long' | 'short' | 'none'
    """
    if not tech:
        return 0.0, "none", "pas_de_donnees"

    price = tech.get("price", 0)
    atr_pct = tech.get("atr_pct", 0)
    if atr_pct < MIN_ATR_PCT:
        return 0.0, "none", f"atr_faible_{atr_pct:.4f}"

    spread = ob.get("spread_pct", 999) / 100.0 if ob.get("spread_pct", 0) > 1 else ob.get("spread_pct", 999)
    if spread > MAX_SPREAD_PCT:
        return 0.0, "none", f"spread_{spread:.5f}"

    if not ob.get("is_liquid_enough", True) and ob:
        return 0.0, "none", "illiquide"

    rsi = tech.get("rsi", 50)
    macd_h = tech.get("macd_hist", 0)
    macd_prev = tech.get("macd_hist_prev", 0)
    trend = tech.get("trend", "range")
    imb = ob.get("bid_ask_imbalance", 0)

    long_score = 0.0
    short_score = 0.0

    # Tendance
    if trend == "bull":
        long_score += 25
    elif trend == "bear":
        short_score += 25
    else:
        long_score += 10
        short_score += 10

    # RSI
    if 40 <= rsi <= 65:
        long_score += 15
    if 35 <= rsi <= 60:
        short_score += 10
    if rsi > 75:
        long_score -= 20
    if rsi < 25:
        short_score -= 20

    # MACD momentum
    if macd_h > 0 and macd_h > macd_prev:
        long_score += 20
    if macd_h < 0 and macd_h < macd_prev:
        short_score += 20

    # Distance S/R
    if tech.get("dist_resistance_pct", 0) >= MIN_SR_DIST:
        long_score += 15
    else:
        long_score -= 15
    if tech.get("dist_support_pct", 0) >= MIN_SR_DIST:
        short_score += 15
    else:
        short_score -= 15

    # Orderbook imbalance
    if imb > 0.08:
        long_score += 10
    if imb < -0.08:
        short_score += 10

    if side_hint == "long":
        short_score *= 0.5
    elif side_hint == "short":
        long_score *= 0.5

    if long_score >= short_score and long_score >= config.MIN_QUANT_SCORE:
        return _clamp(long_score), "long", "setup_long"
    if short_score > long_score and short_score >= config.MIN_QUANT_SCORE:
        return _clamp(short_score), "short", "setup_short"
    best = max(long_score, short_score)
    return _clamp(best), "none", f"score_insuffisant_{best:.0f}"


def scan_symbol(candles: List[dict], ob: dict, side_hint: str = "any") -> dict:
    tech = compute_snapshot(candles)
    score, direction, reason = score_setup(tech, ob, side_hint)
    return {
        "technical": tech,
        "orderbook": ob,
        "quant_score": round(score, 1),
        "direction": direction,
        "reason": reason,
        "eligible": direction in ("long", "short") and score >= config.MIN_QUANT_SCORE,
    }


def rank_candidates(candidates: List[dict]) -> List[dict]:
    return sorted(
        [c for c in candidates if c.get("eligible")],
        key=lambda x: x.get("quant_score", 0),
        reverse=True,
    )[: config.MAX_LLM_TRADES_PER_CYCLE]
"""Agent trader LLM — 1 seul appel par setup candidat."""

from __future__ import annotations

import logging
from typing import Dict, Optional

from llmbot import config
from llmbot import llm

logger = logging.getLogger("sdm.llmbot.trader")

SYSTEM = """Tu es un trader algo professionnel sur Hyperliquid perps.
Tu reçois un setup PRÉ-FILTRÉ par score quantitatif (déjà éligible).
Décide si tu ENTRES ou tu ATTENDS.

Réponds UNIQUEMENT en JSON :
{
  "action": "ENTER_LONG" | "ENTER_SHORT" | "WAIT",
  "confidence": 0.0-1.0,
  "reason": "phrase courte",
  "tp_roe_pct": 0.02-0.05,
  "sl_roe_pct": 0.01-0.02
}

Règles :
- ENTER seulement si tu vois une convergence technique + contexte favorable.
- WAIT si doute, surachat/survente extrême, ou R:R insuffisant.
- tp_roe_pct >= 2 × sl_roe_pct (ratio 2:1 minimum).
- Sois sélectif : mieux vaut WAIT qu'un mauvais trade."""


def decide(
    symbol: str,
    scan: dict,
    news: dict,
    open_positions: int,
) -> Optional[Dict]:
    tech = scan.get("technical", {})
    ob = scan.get("orderbook", {})
    direction = scan.get("direction", "none")
    quant_score = scan.get("quant_score", 0)

    if direction == "long" and news.get("block_longs"):
        return {"action": "WAIT", "confidence": 0.9, "reason": "news_block_longs", "tp_roe_pct": 0.03, "sl_roe_pct": 0.015}
    if direction == "short" and news.get("block_shorts"):
        return {"action": "WAIT", "confidence": 0.9, "reason": "news_block_shorts", "tp_roe_pct": 0.03, "sl_roe_pct": 0.015}

    user = f"""Symbole: {symbol}
Setup quant: score={quant_score} direction={direction}
Prix: {tech.get('price')} | ATR%: {tech.get('atr_pct')} | RSI: {tech.get('rsi')}
Trend: {tech.get('trend')} | MACD hist: {tech.get('macd_hist')}
Support dist: {tech.get('dist_support_pct')} | Resistance dist: {tech.get('dist_resistance_pct')}
Orderbook imbalance: {ob.get('bid_ask_imbalance')} | spread: {ob.get('spread_pct')}
News macro: {news.get('sentiment')} (conf {news.get('confidence')}) — {news.get('summary', '')[:120]}
Positions ouvertes: {open_positions}/{config.MAX_OPEN_POSITIONS}
Levier cible: {config.LEVERAGE}x

Le setup quant suggère {direction.upper()}. Valides-tu l'entrée ?"""

    parsed = llm.chat_json(SYSTEM, user, model=config.MODEL_TRADER)
    if not parsed:
        logger.warning("%s: LLM indisponible → WAIT", symbol)
        return {"action": "WAIT", "confidence": 0.0, "reason": "llm_down", "tp_roe_pct": config.TP_ROE_PCT, "sl_roe_pct": config.SL_ROE_PCT}

    action = str(parsed.get("action", "WAIT")).upper()
    if action not in ("ENTER_LONG", "ENTER_SHORT", "WAIT"):
        action = "WAIT"
    conf = float(parsed.get("confidence", 0) or 0)
    tp = float(parsed.get("tp_roe_pct", config.TP_ROE_PCT) or config.TP_ROE_PCT)
    sl = float(parsed.get("sl_roe_pct", config.SL_ROE_PCT) or config.SL_ROE_PCT)
    sl = max(0.01, min(sl, 0.025))
    tp = max(sl * 2.0, min(tp, 0.06))

    # Cohérence direction
    if action == "ENTER_LONG" and direction != "long":
        action = "WAIT"
    if action == "ENTER_SHORT" and direction != "short":
        action = "WAIT"

    result = {
        "action": action,
        "confidence": conf,
        "reason": str(parsed.get("reason", ""))[:200],
        "tp_roe_pct": tp,
        "sl_roe_pct": sl,
    }
    logger.info(
        "%s LLM → %s conf=%.2f quant=%.0f — %s",
        symbol, action, conf, quant_score, result["reason"][:60],
    )
    return result
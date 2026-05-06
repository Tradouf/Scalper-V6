"""
AgentMomentum (AgentBull) — détecte la direction et la force du signal directionnel.

V6.1 : prompt court et focalisé, retourne signal + confidence directement exploitables
par le consensus. Pas d'arguments texte inutiles.
"""

from __future__ import annotations
from typing import Dict, Optional

from .base_agent import BaseAgent
from memory.shared_memory import SharedMemory


class AgentMomentum(BaseAgent):
    def __init__(self, memory: SharedMemory):
        super().__init__("bull", memory)

    def analyze(self, symbol: str, regime: Optional[Dict], technical: Dict) -> Dict:
        regime = regime or {}
        trend   = regime.get("trend", "unknown")
        vol     = regime.get("volatility", "unknown")
        risk    = regime.get("risk_level", "medium")

        slope   = technical.get("slope", "flat")
        micro   = technical.get("micro_trend", "unknown")
        rsi     = technical.get("rsi", 50)
        macd_h  = technical.get("macd_hist", 0)
        vwap    = technical.get("vwap_signal", 0)

        mkt     = self.memory.get_analysis("MARKET")
        news_s  = mkt.get("news", {}).get("overall_sentiment", "neutral")
        whale_s = mkt.get("whales", {}).get("sentiment", "neutral")

        system = (
            "Tu es un agent de détection de momentum pour un scalper crypto haute fréquence. "
            "Analyse les données et retourne UNIQUEMENT un JSON valide, sans texte autour."
        )

        user = (
            f"Symbole: {symbol}\n"
            f"Régime: trend={trend} vol={vol} risk={risk}\n"
            f"Technique: slope={slope} micro={micro} RSI={rsi:.0f} MACD_hist={macd_h:.4f} VWAP={vwap:.4f}\n"
            f"Contexte: news={news_s} whales={whale_s}\n\n"
            "Question: Y a-t-il un signal d'entrée directionnel clair ?\n"
            'JSON: {"signal":"buy"|"sell"|"wait","confidence":0.0-1.0,"reason":"max 8 mots"}'
        )

        # Distinguer LLM down (None) d'une vraie réponse pour éviter qu'un timeout
        # se traduise en "wait conf=0.00" indistinguable d'un signal neutre légitime.
        llm_resp = self._llm(system, user, temperature=0.2, max_tokens=120)
        if llm_resp is None:
            result = {"signal": "wait", "confidence": 0.0, "reason": "llm_down",
                      "llm_status": "down"}
        else:
            result = self._parse_json(llm_resp)
            if not result or "signal" not in result:
                result = {"signal": "wait", "confidence": 0.0, "reason": "parse error"}

        signal = str(result.get("signal", "wait") or "wait").lower()
        if signal not in ("buy", "sell", "wait"):
            signal = "wait"

        conf = max(0.0, min(1.0, float(result.get("confidence", 0) or 0)))

        out = {
            "signal":     signal,
            "confidence": conf,
            "reason":     str(result.get("reason", "") or ""),
            # compat consensus legacy
            "risk_level": "medium",
        }

        self.memory.update_debate(symbol, "bull", out)
        return out

    # Compat API V4 — non utilisé par le pipeline V6
    def argue(self, symbol: str) -> Dict:
        return self.analyze(symbol, None, {})

"""
AgentBull — défend les arguments HAUSSIERS pour un symbole.
Adapté pour l'interface main_v5: méthode analyze(symbol, regime, tech)
qui renvoie au minimum {confidence, risk_level, ...}.
"""

from __future__ import annotations
from typing import Dict, Optional

from .base_agent import BaseAgent
from memory.shared_memory import SharedMemory


class AgentBull(BaseAgent):
    def __init__(self, memory: SharedMemory):
        super().__init__("bull", memory)

    # API V4 historique: débat dialectique
    def argue(self, symbol: str) -> Dict:
        analysis = self.memory.get_analysis(symbol)
        technical = analysis.get("technical", {})
        news = self.memory.get_analysis("MARKET").get("news", {})
        whales = self.memory.get_analysis("MARKET").get("whales", {})
        regime = self.memory.get_regime()
        trades = self.memory.get_recent_trades(5)

        system = (
            "Tu es un analyste Buy-Side OPTIMISTE dans une salle des marchés. "
            "Ton rôle est de défendre les arguments HAUSSIERS pour le trade. "
            "Sois précis, quantifié, convaincant. Réponds en JSON."
        )

        user = (
            f"Symbole: {symbol}\n"
            f"Analyse technique: {technical}\n"
            f"Sentiment news: {news.get('overall_sentiment','N/A')} | "
            f"Fear/Greed: {news.get('fear_greed','N/A')}\n"
            f"Signal whales: {whales.get('sentiment','N/A')}\n"
            f"Régime marché: {regime}\n"
            f"Trades récents: {trades}\n\n"
            "Construis les 3 meilleurs arguments HAUSSIERS. JSON:\n"
            '{"arguments":["arg1","arg2","arg3"],'
            '"confidence":0.0-1.0,'
            '"upside_target":0.0,'
            '"entry_timing":"now"|"pullback"|"breakout"}'
        )

        result = self._parse_json(
            self._llm(system, user, temperature=0.3, max_tokens=400)
        )
        if not result:
            result = {
                "arguments": [],
                "confidence": 0.3,
                "upside_target": 0.0,
                "entry_timing": "wait",
            }

        self.memory.update_debate(symbol, "bull", result)
        self._send_message(
            "bear", f"BULL {symbol}: conf={result.get('confidence', 0):.2f}"
        )
        return result

    # Nouvelle API utilisée par main_v5
    def analyze(self, symbol: str, regime: Optional[Dict], technical: Dict) -> Dict:
        """
        Adaptation simple: on réutilise argue() et on normalise la sortie
        au format attendu par _consensus(): {confidence, risk_level, ...}.
        """
        res = self.argue(symbol)
        conf = float(res.get("confidence", 0) or 0)

        # risk_level côté bull n'est pas central, on renvoie "medium"
        out = {
            "confidence": max(0.0, min(1.0, conf)),
            "risk_level": "medium",
            "raw": res,
        }
        return out

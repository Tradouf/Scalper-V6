"""
AgentBear — défend les arguments BAISSIERS pour un symbole.
Adapté pour l'interface main_v5: méthode analyze(symbol, regime, tech)
qui renvoie au minimum {risk_level, confidence, ...}.
"""

from __future__ import annotations
from typing import Dict, Optional

from .base_agent import BaseAgent
from memory.shared_memory import SharedMemory


class AgentBear(BaseAgent):
    def __init__(self, memory: SharedMemory):
        super().__init__("bear", memory)

    # API V4 historique: débat dialectique
    def argue(self, symbol: str) -> Dict:
        analysis = self.memory.get_analysis(symbol)
        technical = analysis.get("technical", {})
        news = self.memory.get_analysis("MARKET").get("news", {})
        whales = self.memory.get_analysis("MARKET").get("whales", {})
        bull_args = self.memory.get_debate(symbol).get("bull", {})
        regime = self.memory.get_regime()

        system = (
            "Tu es un analyste Buy-Side SCEPTIQUE dans une salle des marchés. "
            "Ton rôle est de défendre les arguments BAISSIERS et identifier les RISQUES. "
            "Tu dois contrer les arguments du Bull. Réponds en JSON."
        )

        user = (
            f"Symbole: {symbol}\n"
            f"Analyse technique: {technical}\n"
            f"Sentiment news: {news.get('overall_sentiment','N/A')}\n"
            f"Signal whales: {whales.get('sentiment','N/A')}\n"
            f"Régime marché: {regime}\n"
            f"Arguments Bull à contrer: {bull_args.get('arguments',[])}\n\n"
            "Construis les 3 meilleurs arguments BAISSIERS/RISQUES. JSON:\n"
            '{"arguments":["arg1","arg2","arg3"],'
            '"risk_level":"low"|"medium"|"high",'
            '"downside_target":0.0,'
            '"stop_loss_suggestion":0.0}'
        )

        result = self._parse_json(
            self._llm(system, user, temperature=0.3, max_tokens=400)
        )
        if not result:
            result = {
                "arguments": [],
                "risk_level": "medium",
                "downside_target": 0.0,
                "stop_loss_suggestion": 0.0,
            }

        self.memory.update_debate(symbol, "bear", result)
        self._send_message("trader", f"BEAR {symbol}: risk={result.get('risk_level')}")
        return result

    # Nouvelle API utilisée par main_v5
    def analyze(self, symbol: str, regime: Optional[Dict], technical: Dict) -> Dict:
        """
        Adaptation simple: on réutilise argue() et on normalise la sortie
        au format attendu par _consensus(): {risk_level, confidence, ...}.
        """
        res = self.argue(symbol)
        risk_level = str(res.get("risk_level", "medium") or "medium").lower()
        # on dérive une pseudo-confiance bear à partir du downside_target
        downside = float(res.get("downside_target", 0) or 0)
        conf = max(0.0, min(1.0, abs(downside) / 10.0))  # échelle grossière

        out = {
            "risk_level": risk_level,
            "confidence": conf,
            "raw": res,
        }
        return out

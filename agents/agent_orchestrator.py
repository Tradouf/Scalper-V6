"""
AgentOrchestrator (CEO/CIO) — coordonne tous les agents.
[V5.8] enrich_regime_with_features() : regime deterministe via features slope/markov/hmm/vol.
Trend/volatility = deterministe. Directive = LLM uniquement.
"""
from __future__ import annotations
import logging
from typing import Dict, List, Optional
from .base_agent import BaseAgent
from memory.shared_memory import SharedMemory

logger = logging.getLogger("sdm.orchestrator")


class AgentOrchestrator(BaseAgent):

    def __init__(self, memory: SharedMemory):
        super().__init__("orchestrator", memory)

    def enrich_regime_with_features(self, symbol_features: Dict[str, Dict]) -> Dict:
        if not symbol_features:
            return {"trend": "range", "volatility": "medium", "risk": "medium", "persistence": 0.0}
        slope_votes = []
        vol_votes = []
        persistence_scores = []
        markov_votes = []
        for sym, feat in symbol_features.items():
            slope_votes.append(str(feat.get("slope_alignment", "mixed")))
            vol_votes.append(str(feat.get("vol_state", "neutral")))
            persistence_scores.append(float(feat.get("regime_persistence_score", 0.0) or 0.0))
            markov_votes.append(str(feat.get("markov_state", "range")))
        bullish     = slope_votes.count("bullish") + markov_votes.count("trend_up")
        bearish     = slope_votes.count("bearish") + markov_votes.count("trend_down")
        compression = vol_votes.count("compression")
        expansion   = vol_votes.count("expansion")
        persistence = sum(persistence_scores) / max(1, len(persistence_scores))
        trend      = "bull" if bullish > bearish else ("bear" if bearish > bullish else "range")
        volatility = "high" if expansion > compression else ("low" if compression > expansion else "medium")
        risk       = "low" if persistence >= 0.70 and trend != "range" else ("high" if volatility == "high" else "medium")
        return {"trend": trend, "volatility": volatility, "risk": risk, "persistence": persistence}

    def assess_regime(self, market_data: Dict) -> Dict:
        news   = self.memory.get_analysis("MARKET").get("news", {})
        whales = self.memory.get_analysis("MARKET").get("whales", {})

        deterministic_regime = {"trend": "range", "volatility": "medium", "risk": "medium", "persistence": 0.0}
        try:
            if hasattr(self.memory, "get_features"):
                all_features = self.memory.get_features() or {}
                if all_features:
                    deterministic_regime = self.enrich_regime_with_features(all_features)
        except Exception as e:
            logger.warning("enrich_regime_with_features error: %r", e)

        try:
            n_syms = len(self.memory.get_features() or {}) if hasattr(self.memory, "get_features") else 0
        except Exception:
            n_syms = 0

        logger.info(
            "REGIME FEATURES trend=%s vol=%s risk=%s persistence=%.2f nsymbols=%d",
            deterministic_regime.get("trend"), deterministic_regime.get("volatility"),
            deterministic_regime.get("risk"), float(deterministic_regime.get("persistence", 0.0) or 0.0), n_syms,
        )

        system = (
            "Tu es le CIO d une salle des marches crypto sur Hyperliquid futures perpetuels. "
            "REGLES ABSOLUES : "
            "1. Ne jamais ordonner l arret total du trading. "
            "2. La directive doit etre UNE instruction concrete pour les agents. "
            "3. risk_appetite=low en bear/high-vol, medium en range, high en bull/low-vol. "
            "Reponds UNIQUEMENT en JSON valide."
        )
        user = (
            f"Donnees marche global:\n{market_data}\n\n"
            f"Sentiment news: {news.get('overall_sentiment','N/A')} | Fear/Greed: {news.get('fear_greed','N/A')}\n"
            f"Sentiment whales: {whales.get('sentiment','N/A')}\n"
            f"Regime deterministe: trend={deterministic_regime.get('trend')} vol={deterministic_regime.get('volatility')} "
            f"risk={deterministic_regime.get('risk')} persistence={deterministic_regime.get('persistence',0.0):.2f}\n"
            f"Messages agents:\n"
            + "\n".join([f"  [{m['from']}]: {m['content']}"
                          for m in self.memory.get_messages_for("orchestrator")[-5:]])
            + "\n\nDonne uniquement la directive strategique. JSON:\n"
            '{"directive":"instruction courte","active_strategies":["trend"],"max_positions":3}'
        )

        llm_result = self._parse_json(self._llm(system, user, temperature=0.1, max_tokens=200))
        if not llm_result:
            llm_result = {"directive": "prudence", "active_strategies": ["trend"], "max_positions": 3}

        result = {
            "trend": deterministic_regime.get("trend", "range"),
            "volatility": deterministic_regime.get("volatility", "medium"),
            "risk": deterministic_regime.get("risk", "medium"),
            "risk_appetite": deterministic_regime.get("risk", "medium"),
            "active_strategies": llm_result.get("active_strategies", ["trend"]),
            "max_positions": int(llm_result.get("max_positions", 3) or 3),
            "directive": llm_result.get("directive", "prudence"),
        }

        self.memory.update_regime(result.get("trend", "range"), result.get("volatility", "medium"))
        directive = result.get("directive", "")
        if directive:
            self._send_message("all", f"DIRECTIVE CEO: {directive}")
        self.logger.info("Regime: trend=%s volatility=%s risk=%s | %s",
                         result.get("trend"), result.get("volatility"), result.get("risk_appetite"), directive[:60])
        return result

    def decide_entry(self, symbol: str, tech: Dict, bull: Dict, bear: Dict, regime: Dict,
                     news_sentiment: Optional[str] = None, open_positions: Optional[Dict] = None) -> Dict:
        indicators = tech.get("indicators", {}) if isinstance(tech, dict) else {}
        tech_sig   = tech.get("signal", "wait")
        tech_conf  = tech.get("confidence", 0)
        if open_positions:
            lines = [f"  {sym}: {pos.get('side','?')} entry={pos.get('entry',0):.4f}" for sym, pos in open_positions.items()]
            positions_info = "Positions ouvertes :\n" + "\n".join(lines)
        else:
            positions_info = "Aucune position ouverte."

        system = (
            "Tu es le trader senior d une salle des marches crypto sur Hyperliquid (futures perpetuels, levier 5x). "
            "Tu recois les rapports de plusieurs agents specialises et tu prends la DECISION FINALE d entree. "
            "PRINCIPES CLES :\n"
            "- Si les agents se contredisent fortement, la reponse correcte est souvent WAIT.\n"
            "- En cas de doute reel : WAIT.\n"
            "Reponds UNIQUEMENT en JSON valide sur une ligne, sans markdown."
        )
        user = (
            f"SYMBOLE : {symbol}\n\n"
            f"REGIME : trend={regime.get('trend','?')} volatility={regime.get('volatility','?')} "
            f"risk_appetite={regime.get('risk_appetite', regime.get('risk','?'))}\n"
            f"directive CEO: {regime.get('directive','aucune')}\n\n"
            f"AGENT BULL : confiance={bull.get('confidence',0):.2f} | {str(bull.get('reason', bull.get('analysis','N/A')))[:200]}\n\n"
            f"AGENT BEAR : risk_level={bear.get('risk_level','?')} | {str(bear.get('reason', bear.get('analysis','N/A')))[:200]}\n\n"
            f"AGENT TECHNIQUE : signal={tech_sig} conf={tech_conf:.2f} | "
            f"RSI={indicators.get('rsi','?')} vol_ratio={indicators.get('vol_ratio', indicators.get('volratio','?'))}\n"
            f"detail: {str(tech.get('reason', tech.get('analysis','N/A')))[:200]}\n\n"
            f"SENTIMENT NEWS : {news_sentiment or 'N/A'}\n\n"
            f"{positions_info}\n\n"
            '{"side":"buy"|"sell"|"wait","confidence":0.00,"reason":"max 120 chars"}'
        )

        raw = self._llm(system, user, temperature=0.15, max_tokens=150)
        result = self._parse_json(raw)
        if not result or "side" not in result:
            self.logger.warning("decide_entry %s: LLM invalide (%r) -> WAIT", symbol, raw)
            return {"side": "wait", "confidence": 0.0, "reason": "reponse LLM invalide"}

        side = str(result.get("side", "wait")).lower()
        if side not in {"buy", "sell", "wait"}:
            side = "wait"
        conf   = max(0.0, min(1.0, float(result.get("confidence", 0) or 0)))
        reason = str(result.get("reason", ""))[:200]
        self.logger.info("decide_entry %s -> side=%s conf=%.2f | %s", symbol, side, conf, reason)
        return {"side": side, "confidence": conf, "reason": reason}

    def arbitrate(self, symbol: str, trader_decision: Dict, risk_approved: bool, risk_reason: str) -> Dict:
        if not risk_approved:
            self.logger.info("Arbitrage %s: RISK VETO - %s", symbol, risk_reason)
            return {"action": "cancel", "reason": f"Risk veto: {risk_reason}"}
        confidence = trader_decision.get("confidence", 0)
        conviction = trader_decision.get("conviction", "low")
        if confidence < 0.60:
            return {"action": "cancel", "reason": f"Confiance insuffisante: {confidence:.2f}"}
        if conviction == "low" and confidence < 0.70:
            return {"action": "cancel", "reason": "Conviction trop faible"}
        return {"action": "execute", "reason": f"Approuve: conf={confidence:.2f} conv={conviction}"}

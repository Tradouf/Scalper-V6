"""
AgentMemoire (Reflective Agent) — analyse les performances passées
et envoie des recommandations d'amélioration aux autres agents.
Feedback loop permanent.
"""
from __future__ import annotations
import logging
from typing import Dict
from .base_agent import BaseAgent
from memory.shared_memory import SharedMemory

logger = logging.getLogger("sdm.memory_agent")

class AgentMemoire(BaseAgent):

    def __init__(self, memory: SharedMemory):
        super().__init__("memory_agent", memory)

    def reflect(self) -> Dict:
        """
        Analyse les 20 derniers trades, identifie les patterns d'erreur,
        et envoie des recommandations à l'orchestrateur et au trader.
        """
        trades = self.memory.get_recent_trades(20)
        if len(trades) < 3:
            return {"status": "not_enough_data"}

        # Stats rapides
        wins     = [t for t in trades if t.get("pnl",0) > 0]
        losses   = [t for t in trades if t.get("pnl",0) <= 0]
        winrate  = len(wins) / len(trades)
        avg_win  = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
        pf       = abs(avg_win * len(wins)) / abs(avg_loss * len(losses)) if losses and avg_loss != 0 else 999

        system = (
            "Tu es un analyste de performance dans une salle des marchés. "
            "Tu identifies les patterns d'erreur et d'amélioration sur les trades passés. "
            "Réponds en JSON."
        )
        user = (
            f"Historique des {len(trades)} derniers trades:\n"
            + "\n".join([f"  {t.get('symbol','?')} {t.get('side','?').upper()} "
                         f"PnL={t.get('pnl',0)*100:+.1f}%" for t in trades])
            + f"\n\nStats: WR={winrate*100:.0f}% | AvgWin={avg_win*100:+.1f}% | "
              f"AvgLoss={avg_loss*100:+.1f}% | PF={pf:.2f}\n\n"
            "Identifie les patterns et donne des recommandations. JSON:\n"
            '{"patterns":["pattern1","pattern2"],'
            '"recommendations":["rec1","rec2"],'
            '"symbols_to_avoid":[],'
            '"symbols_to_favor":[],'
            '"strategy_adjustment":"description"}'
        )

        result = self._parse_json(self._llm(system, user, temperature=0.2, max_tokens=500))
        if not result:
            return {"status": "parse_error", "winrate": winrate, "profit_factor": pf}

        result["winrate"]       = round(winrate, 3)
        result["profit_factor"] = round(pf, 2)
        result["trades_analyzed"] = len(trades)

        # Envoie les recommandations à l'orchestrateur et au trader
        recs = result.get("recommendations", [])
        if recs:
            msg = "FEEDBACK: " + " | ".join(recs[:3])
            self._send_message("orchestrator", msg)
            self._send_message("trader", msg)

        # Symboles à éviter → alerte risk
        for sym in result.get("symbols_to_avoid", []):
            self.memory.add_alert(f"AgentMémoire recommande d'éviter {sym}")

        self.logger.info("Réflexion: WR=%.0f%% PF=%.2f | %d patterns identifiés",
                        winrate*100, pf, len(result.get("patterns",[])))
        return result

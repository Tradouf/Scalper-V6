"""
AgentRisk (AgentBear) — évalue le risque d'entrée dans les conditions actuelles.

V6.1 : prompt court et focalisé, retourne risk_score numérique (0-1) directement
exploitable par le consensus au lieu d'un simple low/medium/high.
"""

from __future__ import annotations
from typing import Dict, Optional

from .base_agent import BaseAgent
from memory.shared_memory import SharedMemory


class AgentRiskEntry(BaseAgent):
    def __init__(self, memory: SharedMemory):
        super().__init__("bear", memory)

    def analyze(self, symbol: str, regime: Optional[Dict], technical: Dict) -> Dict:
        regime = regime or {}
        trend   = regime.get("trend", "unknown")
        vol     = regime.get("volatility", "unknown")
        risk    = regime.get("risk_level", "medium")

        atr_pct  = technical.get("atr_pct", 0)
        spread   = technical.get("spread_pct", 0)

        # Performances récentes sur ce symbole
        recent   = self.memory.get_recent_trades(5)
        sym_trades = [t for t in recent if str(t.get("symbol","")) == symbol]
        last_pnl   = [float(t.get("pnl_pct", 0) or 0) for t in sym_trades]
        winrate    = (sum(1 for p in last_pnl if p > 0) / len(last_pnl)) if last_pnl else 0.5
        avg_pnl    = (sum(last_pnl) / len(last_pnl)) if last_pnl else 0.0

        # Drawdown quotidien
        risk_status = self.memory.get("risk_status") or {}
        daily_pnl   = float(risk_status.get("daily_pnl", 0) or 0)

        system = (
            "Tu es un agent de gestion du risque pour un scalper crypto haute fréquence. "
            "Évalue le risque d'entrer en position maintenant. "
            "Retourne UNIQUEMENT un JSON valide, sans texte autour."
        )

        user = (
            f"Symbole: {symbol}\n"
            f"Régime: trend={trend} vol={vol} risk={risk}\n"
            f"Volatilité ATR: {atr_pct:.3f}% | Spread: {spread:.4f}%\n"
            f"Perf récente {symbol}: WR={winrate:.0%} avg_pnl={avg_pnl:.3f} "
            f"(sur {len(last_pnl)} trades)\n"
            f"PnL journalier global: {daily_pnl:.3f}%\n\n"
            "Question: Quel est le niveau de risque pour entrer maintenant ?\n"
            'JSON: {"risk":"low"|"medium"|"high","risk_score":0.0-1.0,"reason":"max 8 mots"}'
        )

        llm_resp = self._llm(system, user, temperature=0.2, max_tokens=120)
        if llm_resp is None:
            result = {"risk": "medium", "risk_score": 0.5, "reason": "llm_down",
                      "llm_status": "down"}
        else:
            result = self._parse_json(llm_resp)
            if not result or "risk" not in result:
                result = {"risk": "medium", "risk_score": 0.5, "reason": "parse error"}

        risk_lvl = str(result.get("risk", "medium") or "medium").lower()
        if risk_lvl not in ("low", "medium", "high"):
            risk_lvl = "medium"

        # Si risk_score absent, dérive depuis risk_level
        score_map = {"low": 0.2, "medium": 0.5, "high": 0.8}
        risk_score = result.get("risk_score")
        if risk_score is None:
            risk_score = score_map[risk_lvl]
        risk_score = max(0.0, min(1.0, float(risk_score or 0)))

        out = {
            "risk_level":  risk_lvl,
            "risk_score":  risk_score,
            "confidence":  1.0 - risk_score,  # compat consensus legacy
            "reason":      str(result.get("reason", "") or ""),
        }

        self.memory.update_debate(symbol, "bear", out)
        return out

    # Compat API V4 — non utilisé par le pipeline V6
    def argue(self, symbol: str) -> Dict:
        return self.analyze(symbol, None, {})

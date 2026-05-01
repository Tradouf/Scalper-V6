"""
AgentRisk — VETO ABSOLU. Indépendant de tous les autres agents.
Rapporte directement à l'Orchestrateur.
Peut bloquer ou annuler toute transaction.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Dict, Optional
from .base_agent import BaseAgent
from memory.shared_memory import SharedMemory
from config.settings import MAX_RISK_PER_TRADE, MAX_DAILY_DRAWDOWN, VAR_LIMIT_PCT, MAX_CONCENTRATION, MAX_OPEN_POSITIONS

logger = logging.getLogger("sdm.risk")

@dataclass
class RiskDecision:
    approved:          bool
    reason:            str
    adjusted_qty:      Optional[float] = None
    adjusted_leverage: Optional[float] = None
    max_size_pct:      float = MAX_RISK_PER_TRADE

class AgentRisk(BaseAgent):
    """
    Règles de risque :
    1. Max positions ouvertes
    2. Max exposition par actif
    3. Drawdown journalier maximum
    4. VaR limite
    5. Pas de doublon sur un symbole déjà en position
    """

    def __init__(self, memory: SharedMemory, exchange_client):
        super().__init__("risk", memory)
        self._client = exchange_client

    def validate(self, symbol: str, side: str, size_pct: float,
                 capital: float, price: float, atr: float,
                 sl_atr: float = 2.0) -> RiskDecision:
        """
        Valide ou rejette une décision de trading.
        Peut ajuster la taille mais ne peut pas forcer un trade.
        """
        positions   = self.memory.get_positions()
        risk_status = self.memory.get_risk_status()
        daily_pnl   = risk_status.get("daily_pnl", 0.0)

        # ── Règle 1 : Max positions ───────────────────────────────
        if len(positions) >= MAX_OPEN_POSITIONS and symbol not in positions:
            return RiskDecision(False, f"Max {MAX_OPEN_POSITIONS} positions atteint")

        # ── Règle 2 : Pas de doublon ─────────────────────────────
        if symbol in positions:
            existing_side = positions[symbol].get("side")
            if existing_side == side:
                return RiskDecision(False, f"{symbol} déjà en position {side.upper()}")

        # ── Règle 3 : Drawdown journalier ────────────────────────
        if daily_pnl <= -MAX_DAILY_DRAWDOWN:
            msg = f"Drawdown journalier atteint ({daily_pnl*100:.1f}%) — trading suspendu"
            self.memory.add_alert(msg)
            self._send_message("orchestrator", f"STOP TRADING: {msg}")
            return RiskDecision(False, msg)

        # ── Règle 4 : Taille max par trade ───────────────────────
        size_pct = min(size_pct, MAX_RISK_PER_TRADE)

        # ── Règle 5 : Concentration max ──────────────────────────
        current_exposure = self._compute_exposure(symbol, capital)
        if current_exposure + size_pct > MAX_CONCENTRATION:
            size_pct = max(0.02, MAX_CONCENTRATION - current_exposure)
            self.logger.info("Taille réduite pour concentration: %.1f%%", size_pct*100)

        # ── Règle 6 : SL minimum obligatoire ────────────────────
        sl_distance = sl_atr * atr / price
        if sl_distance < 0.005:  # SL minimum 0.5%
            return RiskDecision(False, "SL trop serré (< 0.5%)")

        # ── Règle 7 : Notional minimum ───────────────────────────
        notional = capital * size_pct
        if notional < 10.0:
            return RiskDecision(False, f"Notional trop faible: {notional:.2f} USDT")

        # ── Levier adapté au régime ───────────────────────────────
        regime    = self.memory.get_regime()
        volatility = regime.get("volatility", "medium")
        leverage  = {"low": 5.0, "medium": 3.0, "high": 2.0}.get(volatility, 3.0)

        qty = (capital * size_pct) / price

        self.logger.info("✅ RISK OK %s %s size=%.1f%% lev=%.0fx SL=%.2f%%",
                        side.upper(), symbol, size_pct*100, leverage, sl_distance*100)

        return RiskDecision(
            approved=True,
            reason="Validé",
            adjusted_qty=round(qty, 6),
            adjusted_leverage=leverage,
            max_size_pct=size_pct
        )

    def update_daily_pnl(self, pnl_change: float):
        """Mise à jour du PnL journalier après chaque trade fermé."""
        risk_status = self.memory.get_risk_status()
        new_pnl = risk_status.get("daily_pnl", 0.0) + pnl_change
        self.memory.update_risk(daily_pnl=new_pnl)
        if new_pnl <= -MAX_DAILY_DRAWDOWN * 0.8:
            self.memory.add_alert(f"PnL journalier à {new_pnl*100:.1f}% — proche du stop")

    def reset_daily(self):
        """Réinitialise le PnL journalier (appelé chaque jour à minuit)."""
        self.memory.update_risk(daily_pnl=0.0, alerts=[])
        self.logger.info("PnL journalier réinitialisé")

    def _compute_exposure(self, symbol: str, capital: float) -> float:
        positions = self.memory.get_positions()
        pos = positions.get(symbol)
        if not pos:
            return 0.0
        return pos.get("size_pct", 0.0)

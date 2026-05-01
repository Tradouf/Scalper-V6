"""
RiskManagerAgent — Agent de gestion des risques.
Calcule les métriques de risque et prend des décisions de trading.
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass
from typing import List, Optional, Dict, Any
from exchanges.base import Balance, Position, ExchangeClient

logger = logging.getLogger("sdm.risk_manager")

@dataclass
class RiskDecision:
    approved: bool
    reason: str
    max_size_pct: Optional[float] = None
    adjusted_qty: Optional[float] = None
    adjusted_leverage: Optional[float] = None

class RiskManagerAgent:
    """
    Agent responsable de la gestion des risques.
    - Calcule l'exposition totale
    - Vérifie les limites de capital
    - Prend des décisions d'approbation/rejet des trades
    """

    def __init__(self, exchange_client: ExchangeClient):
        self._client = exchange_client
        self._daily_pnl: float = 0.0
        self._daily_reset_time: float = time.time()
        self._max_daily_drawdown: float = 0.05

    def _get_capital(self, balances: List[Balance]) -> float:
        """Calcule le capital total disponible."""
        total = 0.0
        for bal in balances:
            if bal.asset == "USDT" or bal.asset == "USD":
                total += bal.free + bal.locked
        return total

    def _calc_total_exposure(self, positions: List[Position]) -> float:
        """Calcule l'exposition totale en USDT."""
        exposure = 0.0
        for pos in positions:
            if pos.symbol.endswith("USDT"):
                exposure += abs(pos.size * pos.entry_price)
        return exposure

    def _maybe_reset_daily(self) -> None:
        """Réinitialise les métriques journalières si un nouveau jour commence."""
        now = time.time()
        if now - self._daily_reset_time > 86400:  # 24 heures
            self._daily_pnl = 0.0
            self._daily_reset_time = now
            logger.info("Métriques journalières réinitialisées")

    def evaluate_trade(
        self,
        symbol: str,
        side: str,
        size_pct: float,
        price: float,
        atr: float,
        max_risk_per_trade: float = 0.02,
        max_daily_drawdown: float = 0.05,
        max_concentration: float = 0.3,
        max_open_positions: int = 5,
        sl_atr: float = 2.0
    ) -> RiskDecision:
        """
        Évalue un trade potentiel et retourne une décision de risque.
        
        Args:
            symbol: Symbole du trade (ex: "BTCUSDT")
            side: "buy" ou "sell"
            size_pct: Pourcentage du capital à risquer (0.01 = 1%)
            price: Prix d'entrée estimé
            atr: Average True Range pour calcul du SL
            max_risk_per_trade: Risque max par trade (défaut: 2%)
            max_daily_drawdown: Drawdown journalier max (défaut: 5%)
            max_concentration: Concentration max par actif (défaut: 30%)
            max_open_positions: Nombre max de positions ouvertes (défaut: 5)
            sl_atr: Multiplicateur ATR pour le stop-loss (défaut: 2.0)
            
        Returns:
            RiskDecision: Décision d'approbation avec ajustements éventuels
        """
        self._maybe_reset_daily()
        
        # Récupérer les données actuelles
        try:
            balances = self._client.get_balances()
            positions = self._client.get_positions()
        except Exception as e:
            logger.error(f"Erreur lors de la récupération des données: {e}")
            return RiskDecision(False, f"Erreur de données: {e}")
        
        capital = self._get_capital(balances)
        total_exposure = self._calc_total_exposure(positions)
        
        # Règle 1: Vérifier le drawdown journalier
        if self._daily_pnl <= -max_daily_drawdown * capital:
            msg = f"Drawdown journalier atteint: {self._daily_pnl:.2f} USDT"
            logger.warning(msg)
            return RiskDecision(False, msg)
        
        # Règle 2: Vérifier le nombre de positions ouvertes
        if len(positions) >= max_open_positions:
            # Vérifier si on a déjà une position sur ce symbole
            existing_symbols = [p.symbol for p in positions]
            if symbol not in existing_symbols:
                msg = f"Maximum de {max_open_positions} positions atteint"
                logger.warning(msg)
                return RiskDecision(False, msg)
        
        # Règle 3: Vérifier la concentration par actif
        current_symbol_exposure = 0.0
        for pos in positions:
            if pos.symbol == symbol:
                current_symbol_exposure = abs(pos.size * pos.entry_price)
                break
        
        new_exposure = capital * size_pct
        total_symbol_exposure = current_symbol_exposure + new_exposure
        
        if total_symbol_exposure > capital * max_concentration:
            # Ajuster la taille pour respecter la limite de concentration
            max_allowed = max(0.0, capital * max_concentration - current_symbol_exposure)
            if max_allowed <= 0:
                msg = f"Concentration max atteinte pour {symbol}"
                logger.warning(msg)
                return RiskDecision(False, msg)
            
            # Réduire la taille à la limite maximale
            size_pct = max_allowed / capital
            logger.info(f"Taille ajustée pour concentration: {size_pct*100:.1f}%")
        
        # Règle 4: Limiter le risque par trade
        size_pct = min(size_pct, max_risk_per_trade)
        
        # Règle 5: Vérifier le notional minimum
        notional = capital * size_pct
        if notional < 10.0:  # Minimum 10 USDT
            msg = f"Notional trop faible: {notional:.2f} USDT"
            logger.warning(msg)
            return RiskDecision(False, msg)
        
        # Règle 6: Vérifier le stop-loss minimum
        sl_distance = sl_atr * atr / price  # Utiliser le paramètre sl_atr
        if sl_distance < 0.005:  # Minimum 0.5%
            msg = f"Stop-loss trop serré: {sl_distance*100:.2f}%"
            logger.warning(msg)
            return RiskDecision(False, msg)
        
        # Calculer la quantité ajustée
        adjusted_qty = (capital * size_pct) / price
        
        # Déterminer le levier basé sur la volatilité
        # Note: Cette logique devrait être améliorée avec des données de marché réelles
        volatility_level = "medium"
        if atr / price > 0.03:
            volatility_level = "high"
        elif atr / price < 0.01:
            volatility_level = "low"
        
        leverage_map = {"low": 5.0, "medium": 3.0, "high": 2.0}
        adjusted_leverage = leverage_map.get(volatility_level, 3.0)
        
        logger.info(
            f"✅ Trade approuvé: {symbol} {side.upper()} "
            f"size={size_pct*100:.1f}% qty={adjusted_qty:.6f} "
            f"lev={adjusted_leverage:.1f}x SL={sl_distance*100:.2f}%"
        )
        
        return RiskDecision(
            approved=True,
            reason="Trade approuvé par le risk manager",
            max_size_pct=size_pct,
            adjusted_qty=adjusted_qty,
            adjusted_leverage=adjusted_leverage
        )
    
    def update_daily_pnl(self, pnl_change: float) -> None:
        """Met à jour le PnL journalier."""
        self._daily_pnl += pnl_change
        logger.info(f"PnL journalier mis à jour: {self._daily_pnl:.2f} USDT")
        
        # Alerter si proche du drawdown limite
        drawdown_limit = self._max_daily_drawdown * self._get_capital([])
        if self._daily_pnl <= -0.8 * drawdown_limit:
            logger.warning(f"PnL journalier proche de la limite: {self._daily_pnl:.2f} USDT")
    
    def get_risk_metrics(self) -> Dict[str, Any]:
        """Retourne les métriques de risque actuelles."""
        try:
            balances = self._client.get_balances()
            positions = self._client.get_positions()
            
            capital = self._get_capital(balances)
            exposure = self._calc_total_exposure(positions)
            exposure_pct = (exposure / capital) * 100 if capital > 0 else 0
            
            return {
                "capital_usdt": capital,
                "total_exposure_usdt": exposure,
                "exposure_percentage": exposure_pct,
                "daily_pnl_usdt": self._daily_pnl,
                "open_positions": len(positions),
                "positions": [
                    {
                        "symbol": p.symbol,
                        "side": "long" if p.size > 0 else "short",
                        "size": abs(p.size),
                        "entry_price": p.entry_price,
                        "current_price": p.current_price if hasattr(p, 'current_price') else p.entry_price,
                        "pnl": p.pnl if hasattr(p, 'pnl') else 0.0
                    }
                    for p in positions
                ]
            }
        except Exception as e:
            logger.error(f"Erreur lors du calcul des métriques: {e}")
            return {
                "capital_usdt": 0.0,
                "total_exposure_usdt": 0.0,
                "exposure_percentage": 0.0,
                "daily_pnl_usdt": self._daily_pnl,
                "open_positions": 0,
                "positions": [],
                "error": str(e)
            }

"""
StrategyMomentumAgent — Salle des Marchés
Stratégie momentum pur : RSI + Bollinger Bands + volume spike.

Génère des signaux même en tendance établie (pas besoin de croisement).
Complémentaire de StrategyTrendAgent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from exchanges.base import OrderRequest
from agents.market_scanner import MarketScore
from agents.strategy_trend import (
    TradeSignal, _rsi, _atr, _volume_spike,
    LEVERAGE, STOP_LOSS_ATR, TAKE_PROFIT_ATR,
)

logger = logging.getLogger("sdm.strategy_momentum")

RSI_PERIOD    = 14
BB_PERIOD     = 20
BB_STD        = 2.0
CANDLE_INTERVAL = "15m"
CANDLE_LIMIT    = 100


def _bollinger(prices: List[float], period: int = 20, num_std: float = 2.0):
    if len(prices) < period:
        return None, None, None
    window = prices[-period:]
    mean   = sum(window) / period
    std    = (sum((p - mean)**2 for p in window) / period) ** 0.5
    return mean, mean + num_std * std, mean - num_std * std


class StrategyMomentumAgent:
    """
    Signaux basés sur RSI + Bollinger Bands en 15 minutes.

    Long  : prix touche bande basse BB + RSI < 35 + volume spike
    Short : prix touche bande haute BB + RSI > 65 + volume spike
    """

    def __init__(self, exchange_client):
        self._client = exchange_client

    def analyze(self, markets: List[MarketScore]) -> List[TradeSignal]:
        signals = []
        for m in markets:
            try:
                signal = self._analyze_one(m)
                if signal:
                    signals.append(signal)
                    logger.info(
                        "Signal MOMENTUM %s %s @ %.4f conf=%.2f [%s]",
                        signal.side.upper(), signal.symbol,
                        signal.entry_price, signal.confidence, signal.reason,
                    )
            except Exception as e:
                logger.debug("Erreur momentum %s: %s", m.symbol, e)
        return signals

    def _analyze_one(self, market: MarketScore) -> Optional[TradeSignal]:
        symbol  = market.symbol
        candles = self._client._client.get_candles(
            symbol, interval=CANDLE_INTERVAL, limit=CANDLE_LIMIT
        )
        if len(candles) < BB_PERIOD + 5:
            return None

        closes     = [c["close"] for c in candles]
        price      = closes[-1]
        rsi        = _rsi(closes, RSI_PERIOD)
        atr        = _atr(candles, 14)
        vol_spike  = _volume_spike(candles)
        bb_mid, bb_upper, bb_lower = _bollinger(closes, BB_PERIOD, BB_STD)

        if atr <= 0 or bb_upper is None:
            return None

        # ── Signal LONG : survente ───────────────
        if price <= bb_lower and rsi < 35:
            confidence = 0.65
            if vol_spike:
                confidence += 0.15
            if market.whale_signal > 500_000:
                confidence += 0.10
            if rsi < 25:
                confidence += 0.10
            return TradeSignal(
                symbol      = symbol,
                side        = "buy",
                entry_price = price,
                stop_loss   = price - STOP_LOSS_ATR * atr,
                take_profit = price + TAKE_PROFIT_ATR * atr,
                confidence  = round(min(confidence, 1.0), 2),
                reason      = f"BB_lower RSI={rsi:.1f} vol_spike={vol_spike}",
            )

        # ── Signal SHORT : surachat ──────────────
        if price >= bb_upper and rsi > 65:
            confidence = 0.65
            if vol_spike:
                confidence += 0.15
            if market.whale_signal < -500_000:
                confidence += 0.10
            if rsi > 75:
                confidence += 0.10
            return TradeSignal(
                symbol      = symbol,
                side        = "sell",
                entry_price = price,
                stop_loss   = price + STOP_LOSS_ATR * atr,
                take_profit = price - TAKE_PROFIT_ATR * atr,
                confidence  = round(min(confidence, 1.0), 2),
                reason      = f"BB_upper RSI={rsi:.1f} vol_spike={vol_spike}",
            )

        return None

    def signals_to_orders(
        self,
        signals: List[TradeSignal],
        capital: float,
        min_confidence: float = 0.65,
    ) -> List[OrderRequest]:
        orders = []
        for s in signals:
            if s.confidence < min_confidence:
                continue
            risk_pct = 0.05 * s.confidence
            notional = capital * risk_pct
            qty      = notional / s.entry_price
            orders.append(OrderRequest(
                symbol     = s.symbol,
                side       = s.side,
                qty        = round(qty, 6),
                order_type = "limit",
                price      = round(s.entry_price, 4),
                leverage   = float(LEVERAGE),
            ))
        return orders

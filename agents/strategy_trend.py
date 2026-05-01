"""
StrategyTrendAgent — Salle des Marchés
Stratégie trend-following multi-timeframe.

Signaux générés à partir de :
- EMA 20 / EMA 50 (croisement = signal directionnel)
- RSI 14 (filtre de surachat/survente)
- ATR 14 (sizing adaptatif et stop-loss dynamique)
- Volume spike (confirmation du mouvement)

Retourne des OrderRequest prêts à être validés par le RiskManagerAgent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from exchanges.base import OrderRequest
from agents.market_scanner import MarketScore

logger = logging.getLogger("sdm.strategy_trend")


# ──────────────────────────────────────────────
# Paramètres stratégie
# ──────────────────────────────────────────────

EMA_FAST          = 20
EMA_SLOW          = 50
RSI_PERIOD        = 14
ATR_PERIOD        = 14
RSI_OVERSOLD      = 40    # seuil entrée long
RSI_OVERBOUGHT    = 60    # seuil entrée short
LEVERAGE          = 5     # levier fixe (Risk Manager peut réduire)
CANDLE_INTERVAL   = "1h"
CANDLE_LIMIT      = 100
STOP_LOSS_ATR     = 2.0   # stop = prix ± 2×ATR
TAKE_PROFIT_ATR   = 4.0   # TP = prix ± 4×ATR (RR = 2:1)


# ──────────────────────────────────────────────
# Signal retourné
# ──────────────────────────────────────────────

@dataclass
class TradeSignal:
    symbol: str
    side: str           # "buy" ou "sell"
    entry_price: float
    stop_loss: float
    take_profit: float
    confidence: float   # 0-1
    reason: str


# ──────────────────────────────────────────────
# Indicateurs techniques (calcul pur Python)
# ──────────────────────────────────────────────

def _ema(prices: List[float], period: int) -> List[float]:
    if len(prices) < period:
        return []
    k = 2.0 / (period + 1)
    result = [sum(prices[:period]) / period]
    for p in prices[period:]:
        result.append(p * k + result[-1] * (1 - k))
    return result


def _rsi(prices: List[float], period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i+1] - prices[i] for i in range(len(prices)-1)]
    gains  = [d if d > 0 else 0.0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0.0 for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(candles: List[dict], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        high  = candles[i]["high"]
        low   = candles[i]["low"]
        close = candles[i-1]["close"]
        trs.append(max(high - low, abs(high - close), abs(low - close)))
    return sum(trs[-period:]) / period


def _volume_spike(candles: List[dict], lookback: int = 20) -> bool:
    if len(candles) < lookback + 1:
        return False
    avg_vol = sum(c["vol"] for c in candles[-lookback-1:-1]) / lookback
    last_vol = candles[-1]["vol"]
    return last_vol > avg_vol * 1.5   # spike = volume 50% au-dessus de la moyenne


# ──────────────────────────────────────────────
# Agent
# ──────────────────────────────────────────────

class StrategyTrendAgent:
    """
    Génère des signaux de trade trend-following sur les marchés sélectionnés
    par le MarketScannerAgent.

    Usage:
        strategy = StrategyTrendAgent(exchange_client)
        signals  = strategy.analyze(top_markets)
        orders   = strategy.signals_to_orders(signals, capital=188.0)
    """

    def __init__(self, exchange_client):
        self._client = exchange_client

    # ──────────────────────────────────────────
    # Analyse
    # ──────────────────────────────────────────

    def analyze(self, markets: List[MarketScore]) -> List[TradeSignal]:
        """
        Analyse une liste de marchés et retourne les signaux détectés.
        """
        signals = []
        for m in markets:
            try:
                signal = self._analyze_one(m)
                if signal:
                    signals.append(signal)
                    logger.info(
                        "Signal %s %s @ %.4f  SL=%.4f  TP=%.4f  conf=%.2f  [%s]",
                        signal.side.upper(), signal.symbol,
                        signal.entry_price, signal.stop_loss,
                        signal.take_profit, signal.confidence,
                        signal.reason,
                    )
            except Exception as e:
                logger.debug("Erreur analyse %s: %s", m.symbol, e)
        return signals

    def _analyze_one(self, market: MarketScore) -> Optional[TradeSignal]:
        symbol  = market.symbol
        candles = self._client._client.get_candles(
            symbol, interval=CANDLE_INTERVAL, limit=CANDLE_LIMIT
        )
        if len(candles) < EMA_SLOW + 5:
            return None

        closes = [c["close"] for c in candles]
        price  = closes[-1]

        # Indicateurs
        ema_fast_series = _ema(closes, EMA_FAST)
        ema_slow_series = _ema(closes, EMA_SLOW)
        if len(ema_fast_series) < 2 or len(ema_slow_series) < 2:
            return None

        ema_fast_now  = ema_fast_series[-1]
        ema_fast_prev = ema_fast_series[-2]
        ema_slow_now  = ema_slow_series[-1]
        ema_slow_prev = ema_slow_series[-2]

        rsi        = _rsi(closes, RSI_PERIOD)
        atr        = _atr(candles, ATR_PERIOD)
        vol_spike  = _volume_spike(candles)

        if atr <= 0:
            return None

        # ── Signal LONG ──────────────────────────
        # EMA fast croise EMA slow vers le haut + RSI pas suracheté + volume spike
        cross_up = ema_fast_prev <= ema_slow_prev and ema_fast_now > ema_slow_now
        if cross_up and rsi < RSI_OVERBOUGHT:
            confidence = 0.6
            if vol_spike:
                confidence += 0.2
            if market.whale_signal > 1_000_000:
                confidence += 0.1
            if rsi < RSI_OVERSOLD:
                confidence += 0.1
            confidence = min(confidence, 1.0)

            return TradeSignal(
                symbol      = symbol,
                side        = "buy",
                entry_price = price,
                stop_loss   = price - STOP_LOSS_ATR * atr,
                take_profit = price + TAKE_PROFIT_ATR * atr,
                confidence  = round(confidence, 2),
                reason      = f"EMA{EMA_FAST}/EMA{EMA_SLOW} crossup RSI={rsi:.1f} ATR={atr:.4f}",
            )

        # ── Signal SHORT ─────────────────────────
        # EMA fast croise EMA slow vers le bas + RSI pas survendu
        cross_down = ema_fast_prev >= ema_slow_prev and ema_fast_now < ema_slow_now
        if cross_down and rsi > RSI_OVERSOLD:
            confidence = 0.6
            if vol_spike:
                confidence += 0.2
            if market.whale_signal < -1_000_000:
                confidence += 0.1
            if rsi > RSI_OVERBOUGHT:
                confidence += 0.1
            confidence = min(confidence, 1.0)

            return TradeSignal(
                symbol      = symbol,
                side        = "sell",
                entry_price = price,
                stop_loss   = price + STOP_LOSS_ATR * atr,
                take_profit = price - TAKE_PROFIT_ATR * atr,
                confidence  = round(confidence, 2),
                reason      = f"EMA{EMA_FAST}/EMA{EMA_SLOW} crossdown RSI={rsi:.1f} ATR={atr:.4f}",
            )

        return None

    # ──────────────────────────────────────────
    # Conversion signal → OrderRequest
    # ──────────────────────────────────────────

    def signals_to_orders(
        self,
        signals: List[TradeSignal],
        capital: float,
        min_confidence: float = 0.6,
    ) -> List[OrderRequest]:
        """
        Convertit les signaux en OrderRequest.
        Filtre par confidence minimale.
        """
        orders = []
        for s in signals:
            if s.confidence < min_confidence:
                logger.debug("Signal %s ignoré: confidence %.2f < %.2f", s.symbol, s.confidence, min_confidence)
                continue

            # Taille basée sur la confidence (plus confiant = plus gros)
            risk_pct = 0.05 * s.confidence   # max 5% du capital par trade
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

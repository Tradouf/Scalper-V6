"""
AgentTrader — Salle des Marches V5
Agent IA de trading basé sur LocalAI (Qwen2.5-7b).
Lit le débat Bull/Bear, le régime, les news et les whales depuis SharedMemory.

MODIFICATIONS V5.4:
- flip_position() : fermeture propre + annulation TP/SL orphelins + réouverture
- cancel_all_tpsl() : annule tous les ordres reduce_only d'un symbole
- wait_flat_and_clean() : attend que la position soit nulle et les ordres propres
- TP placé en MARKET (orderType={"trigger": {"isMarket": True}})
- log_flip_event() : trace chaque flip dans shared_memory pour le Learner

MODIFS 5.4.1 :
- cancel_all_tpsl() : ajoute la route trigger native (TP/SL Hyperliquid)
- wait_flat_and_clean() : timeout porté à 20s

MODIFS 5.5 :
- record_result() accepte le closed_pnl_usdc réel Hyperliquid
- pnl_pct priorise le PnL réalisé exchange si fourni
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import requests

from agents.market_scanner import MarketScore
from agents.strategy_trend import TradeSignal

logger = logging.getLogger("sdm.agent_trader")

LOCALAI_URL = "http://localhost:8080/v1/chat/completions"
LOCALAI_MODEL = "qwen2.5-7b-trader"
MEMORY_FILE = "trader_memory.json"
MAX_MEMORY = 20
CANDLE_INTERVAL = "1h"
CANDLE_LIMIT = 50


def _ema(prices, period):
    if len(prices) < period:
        return []
    k = 2.0 / (period + 1)
    result = [sum(prices[:period]) / period]
    for p in prices[period:]:
        result.append(p * k + result[-1] * (1 - k))
    return result


def _rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i + 1] - prices[i] for i in range(len(prices) - 1)]
    gains = [d if d > 0 else 0.0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0.0 for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def _atr(candles, period=14):
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, c = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - c), abs(l - c)))
    return sum(trs[-period:]) / period


def _bollinger(prices, period=20, nb_std=2.0):
    if len(prices) < period:
        return None, None, None
    recent = prices[-period:]
    mean = sum(recent) / period
    std = (sum((p - mean) ** 2 for p in recent) / period) ** 0.5
    return mean - nb_std * std, mean, mean + nb_std * std


def _macd(prices, fast=12, slow=26, signal=9):
    ema_fast = _ema(prices, fast)
    ema_slow = _ema(prices, slow)
    if not ema_fast or not ema_slow:
        return 0.0, 0.0
    min_len = min(len(ema_fast), len(ema_slow))
    macd_line = [
        ema_fast[-min_len:][i] - ema_slow[-min_len:][i] for i in range(min_len)
    ]
    sig_line = _ema(macd_line, signal)
    if not sig_line:
        return macd_line[-1], 0.0
    return macd_line[-1], sig_line[-1]


def _volume_ratio(candles, lookback=20):
    if len(candles) < lookback + 1:
        return 1.0
    avg = sum(c["vol"] for c in candles[-lookback - 1 : -1]) / lookback
    return candles[-1]["vol"] / avg if avg > 0 else 1.0


def find_sr_levels(closes: list, price: float, atr: float) -> dict:
    resistances, supports = [], []
    for i in range(2, len(closes) - 2):
        hi = closes[i] >= max(closes[i - 2], closes[i - 1], closes[i + 1], closes[i + 2])
        lo = closes[i] <= min(closes[i - 2], closes[i - 1], closes[i + 1], closes[i + 2])
        if hi:
            resistances.append(closes[i])
        if lo:
            supports.append(closes[i])

    rng = price * 0.10
    res_near = sorted([r for r in resistances if r > price and r < price + rng])[:3]
    sup_near = sorted([s for s in supports if s < price and s > price - rng], reverse=True)[:3]

    tp_long = res_near[0] if res_near else round(price + 4 * atr, 6)
    tp_short = sup_near[0] if sup_near else round(price - 4 * atr, 6)
    sl_long = sup_near[0] if sup_near else round(price - 2 * atr, 6)
    sl_short = res_near[0] if res_near else round(price + 2 * atr, 6)
    rr_long = round(abs(tp_long - price) / max(abs(price - sl_long), 0.0001), 2)
    rr_short = round(abs(tp_short - price) / max(abs(sl_short - price), 0.0001), 2)

    return {
        "resistances": " | ".join(f"{r:.4f}" for r in res_near) or "non détecté",
        "supports": " | ".join(f"{s:.4f}" for s in sup_near) or "non détecté",
        "tp_long": tp_long,
        "tp_short": tp_short,
        "sl_long": sl_long,
        "sl_short": sl_short,
        "rr_long": rr_long,
        "rr_short": rr_short,
    }


class AgentTrader:
    def __init__(self, memory, exchange_client):
        self._client = memory
        self._exchange = exchange_client
        self._memory = self._load_memory()

    def analyze(self, markets: List[MarketScore]) -> List[TradeSignal]:
        signals = []
        for market in markets:
            try:
                signal = self._analyze_one(market)
                if signal:
                    signals.append(signal)
                    logger.info(
                        "Agent: %s %s @ %.4f conf=%.2f | %s",
                        signal.side.upper(),
                        signal.symbol,
                        signal.entry_price,
                        signal.confidence,
                        signal.reason[:80],
                    )
            except Exception as e:
                logger.debug("Erreur agent %s: %s", market.symbol, e)
        return signals

    def decide(self, symbol: str) -> Optional[dict]:
        def _safe(fn):
            try:
                return fn()
            except Exception:
                return {}

        debate = _safe(lambda: self._client.get_debate(symbol))
        regime = _safe(lambda: self._client.get_regime())
        risk_st = _safe(lambda: self._client.get_risk_status())
        news = _safe(lambda: self._client.get_analysis("MARKET").get("news", {}))
        whales = _safe(lambda: self._client.get_analysis("MARKET").get("whales", {}))
        positions = _safe(lambda: self._client.get_positions())

        ms = MarketScore(
            symbol=symbol,
            score=50.0,
            price=0.0,
            volume_24h=0.0,
            spread_bps=0.0,
            funding_rate=0.0,
            momentum_pct=0.0,
            open_interest=0.0,
            whale_signal=0.0,
            whale_details=[{
                "debate": debate,
                "regime": regime,
                "news": news,
                "whales": whales,
                "daily_pnl": risk_st.get("daily_pnl", 0.0),
                "nb_pos": len(positions),
            }],
        )

        sig = self._analyze_one(ms)
        if sig is None:
            return None
        return {
            "side": sig.side,
            "confidence": sig.confidence,
            "sl_atr": 2.0,
            "tp_atr": 4.0,
            "size_pct": 0.08,
            "reason": sig.reason,
        }

    def review_position(self, symbol: str, pos: dict, price: float) -> dict:
        entry = pos.get("entry", price)
        side = pos.get("side", "buy")
        sl = pos.get("sl", 0)
        tp = pos.get("tp", 0)
        pnl = (price - entry) / entry if side == "buy" else (entry - price) / entry
        prompt = (
            "Tu es un trader crypto expert. Tu gères une position ouverte.\n\n"
            f"POSITION : {side.upper()} {symbol}\n"
            f" Entrée : {entry:.6f} USDT\n"
            f" Prix actuel: {price:.6f} USDT\n"
            f" PnL actuel : {pnl*100:+.2f}%\n"
            f" Stop-Loss : {sl:.6f}\n"
            f" Take-Profit: {tp:.6f}\n\n"
            "Décide : garder (hold), fermer (close), ajuster SL (adjust_sl) ou TP (adjust_tp) ?\n\n"
            'Réponds UNIQUEMENT en JSON :\n'
            '{"action": "hold", "new_sl": null, "new_tp": null, "reason": "..."}\n'
        )
        decision = self._call_llm(prompt, symbol)
        if decision is None:
            return {"action": "hold", "reason": "LLM indisponible"}
        return decision

    def record_result(
        self,
        symbol: str,
        side: str,
        entry: float,
        exit_price: float,
        reason: str,
        qty: float | None = None,
        leverage: float | None = None,
        closed_pnl_usdc: float | None = None,
        fee_usdc: float | None = None,
        source: str = "local",
    ) -> None:
        pnl_pct_local = (
            (exit_price - entry) / entry * 100 if side == "buy" else (entry - exit_price) / entry * 100
        )

        pnl_pct = pnl_pct_local
        notional = abs(float(entry or 0)) * abs(float(qty or 0))

        if closed_pnl_usdc is not None and notional > 0:
            pnl_pct = (float(closed_pnl_usdc) / notional) * 100.0

        result = {
            "ts": datetime.now().isoformat(),
            "symbol": symbol,
            "side": side,
            "entry": round(float(entry or 0), 8),
            "exit": round(float(exit_price or 0), 8),
            "qty": round(float(qty or 0), 8) if qty is not None else None,
            "leverage": float(leverage or 0) if leverage is not None else None,
            "pnl_pct": round(pnl_pct, 4),
            "pnl_pct_local": round(pnl_pct_local, 4),
            "closed_pnl_usdc": round(float(closed_pnl_usdc), 8) if closed_pnl_usdc is not None else None,
            "fee_usdc": round(float(fee_usdc), 8) if fee_usdc is not None else None,
            "reason": reason,
            "source": source,
            "outcome": "WIN" if pnl_pct > 0 else "LOSS",
        }

        self._memory.append(result)
        if len(self._memory) > MAX_MEMORY:
            self._memory = self._memory[-MAX_MEMORY:]
        self._save_memory()

        if closed_pnl_usdc is not None:
            logger.info(
                "Trade enregistré: %s %s PnL=%.4f%% closed=%.4f USDC source=%s",
                symbol,
                side,
                pnl_pct,
                float(closed_pnl_usdc),
                source,
            )
        else:
            logger.info(
                "Trade enregistré: %s %s PnL=%.4f%% source=%s",
                symbol,
                side,
                pnl_pct,
                source,
            )

    def cancel_all_tpsl(self, symbol: str) -> None:
        cancelled = 0
        try:
            try:
                raw_orders = self._exchange._client.get_open_orders()
            except Exception as e:
                logger.warning("[FLIP] get_open_orders a échoué pour %s: %r", symbol, e)
                raw_orders = []

            for o in raw_orders:
                coin = o.get("coin") or o.get("symbol") or ""
                if str(coin).upper() != str(symbol).upper():
                    continue
                oid = o.get("oid") or o.get("order_id") or o.get("orderId")
                reduce_only = (
                    o.get("reduceOnly", False)
                    or o.get("reduce_only", False)
                    or o.get("reduceonly", False)
                )
                order_type = o.get("orderType") or o.get("order_type") or {}
                is_trigger = (
                    "triggerCondition" in o
                    or "triggerPx" in o
                    or "trigger" in str(order_type)
                )

                if (reduce_only or is_trigger) and oid:
                    try:
                        self._exchange._client.cancel(symbol, oid)
                        cancelled += 1
                        logger.info("[FLIP] Ordre annulé %s oid=%s", symbol, oid)
                    except Exception as e:
                        logger.warning("[FLIP] Echec annulation oid=%s %s: %r", oid, symbol, e)

            try:
                get_triggers = getattr(self._exchange._client, "get_open_trigger_orders", None)
                if get_triggers is not None:
                    trigger_orders = get_triggers()
                    for o in trigger_orders or []:
                        coin = o.get("coin") or o.get("symbol") or ""
                        if str(coin).upper() != str(symbol).upper():
                            continue
                        oid = o.get("oid") or o.get("order_id") or o.get("orderId")
                        if not oid:
                            continue
                        try:
                            self._exchange._client.cancel(symbol, oid)
                            cancelled += 1
                            logger.info("[FLIP] Ordre trigger annulé %s oid=%s", symbol, oid)
                        except Exception as e:
                            logger.warning("[FLIP] Echec annulation trigger oid=%s %s: %r", oid, symbol, e)
                else:
                    try:
                        cancel_all = getattr(self._exchange._client, "cancel_all", None)
                        if cancel_all is not None:
                            cancel_all(symbol)
                            logger.info("[FLIP] cancel_all utilisé pour %s (fallback)", symbol)
                    except Exception as e:
                        logger.warning("[FLIP] cancel_all fallback a échoué pour %s: %r", symbol, e)
            except Exception as e:
                logger.warning("[FLIP] get_open_trigger_orders/cancel_all erreur %s: %r", symbol, e)

            logger.info("[FLIP] cancel_all_tpsl %s: %d ordres annulés", symbol, cancelled)
        except Exception as e:
            logger.error("[FLIP] cancel_all_tpsl error %s: %r", symbol, e)

    def wait_flat_and_clean(self, symbol: str, timeout: float = 20.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                pos_list = self._exchange.get_positions()
                flat = True
                for p in pos_list:
                    pos = p.get("position", p) if isinstance(p, dict) else p
                    coin = pos.get("coin") or pos.get("symbol") or getattr(p, "symbol", "")
                    qty = float(pos.get("szi") or pos.get("qty") or getattr(p, "qty", 0) or 0)
                    if str(coin).upper() == str(symbol).upper() and abs(qty) > 0:
                        flat = False
                        break

                if flat:
                    try:
                        raw_orders = self._exchange._client.get_open_orders()
                    except Exception as e:
                        logger.debug("[FLIP] get_open_orders dans wait_flat_and_clean a échoué: %r", e)
                        raw_orders = []

                    orphan = any(
                        str(o.get("coin", "")).upper() == str(symbol).upper()
                        and (
                            o.get("reduceOnly")
                            or o.get("reduce_only")
                            or "triggerCondition" in o
                            or "triggerPx" in o
                            or "trigger" in str(o.get("orderType") or o.get("order_type") or {})
                        )
                        for o in raw_orders
                    )
                    if not orphan:
                        logger.info("[FLIP] %s est flat et propre.", symbol)
                        return True
            except Exception as e:
                logger.debug("[FLIP] wait_flat_and_clean poll error: %r", e)

            time.sleep(0.5)

        logger.warning("[FLIP] Timeout wait_flat_and_clean %s, on continue quand même.", symbol)
        return False

    def log_flip_event(
        self,
        symbol: str,
        old_side: str,
        new_side: str,
        pnl_realise: float,
        context: dict,
    ) -> None:
        event = {
            "ts": datetime.now().isoformat(),
            "type": "flip",
            "symbol": symbol,
            "from_side": old_side,
            "to_side": new_side,
            "pnl_realise_pct": round(pnl_realise * 100, 3),
            "rsi": context.get("rsi"),
            "trend": context.get("trend"),
            "regime": context.get("regime"),
        }
        try:
            self._client.add_flip_event(event)
        except Exception:
            self._memory.append(event)
            self._save_memory()
        logger.info(
            "[FLIP] Evénement enregistré: %s %s→%s PnL=%.2f%%",
            symbol,
            old_side.upper(),
            new_side.upper(),
            pnl_realise * 100,
        )

    def _analyze_one(self, market: MarketScore) -> Optional[TradeSignal]:
        symbol = market.symbol
        candles = self._exchange._client.get_candles(symbol, interval=CANDLE_INTERVAL, limit=CANDLE_LIMIT)
        if len(candles) < 30:
            return None

        closes = [c["close"] for c in candles]
        price = closes[-1]
        ema20 = _ema(closes, 20)
        ema50 = _ema(closes, 50)
        rsi = _rsi(closes)
        atr = _atr(candles)
        bb_low, bb_mid, bb_high = _bollinger(closes)
        macd_val, macd_sig = _macd(closes)
        vol_ratio = _volume_ratio(candles)
        last_5 = closes[-5:]
        trend_5 = "hausse" if last_5[-1] > last_5[0] else "baisse"
        pct_5h = (last_5[-1] - last_5[0]) / last_5[0] * 100

        context = self._build_context(
            symbol, price, rsi, atr, ema20, ema50, bb_low, bb_mid, bb_high,
            macd_val, macd_sig, vol_ratio, trend_5, pct_5h, market,
        )

        decision = self._call_llm(context, symbol)
        if decision is None:
            return None

        side = decision.get("side", "wait")
        confidence = float(decision.get("confidence", 0.0))
        reason = decision.get("reason", "")

        if side == "wait" or confidence < 0.60:
            logger.debug("Agent %s: WAIT (conf=%.2f)", symbol, confidence)
            return None

        atr_safe = atr if atr > 0 else price * 0.02
        sl_atr = float(decision.get("sl_atr", 2.0))
        tp_atr = float(decision.get("tp_atr", 4.0))
        tp_price_llm = decision.get("tp_price")

        if side == "buy":
            sl = price - sl_atr * atr_safe
            tp = float(tp_price_llm) if tp_price_llm else price + tp_atr * atr_safe
        else:
            sl = price + sl_atr * atr_safe
            tp = float(tp_price_llm) if tp_price_llm else price - tp_atr * atr_safe

        return TradeSignal(
            symbol=symbol,
            side=side,
            entry_price=price,
            stop_loss=round(sl, 6),
            take_profit=round(tp, 6),
            confidence=round(confidence, 2),
            reason=reason,
        )

    def _build_context(
        self, symbol, price, rsi, atr, ema20, ema50, bb_low, bb_mid, bb_high,
        macd_val, macd_sig, vol_ratio, trend_5, pct_5h, market,
    ) -> str:
        ema20_val = ema20[-1] if ema20 else price
        ema50_val = ema50[-1] if ema50 else price

        if bb_high and bb_low:
            if price > bb_high:
                bb_pos = "AU-DESSUS des bandes (surachetée)"
            elif price < bb_low:
                bb_pos = "EN-DESSOUS des bandes (survendue)"
            else:
                bb_pct = (price - bb_low) / (bb_high - bb_low) * 100
                bb_pos = "dans les bandes à " + str(int(bb_pct)) + "% du bas"
        else:
            bb_pos = "indisponible"

        macd_cross = (
            "MACD au-dessus signal (bullish)"
            if macd_val > macd_sig
            else "MACD en-dessous signal (bearish)"
        )

        symbol_memory = [m for m in self._memory if m.get("symbol") == symbol][-3:]
        if symbol_memory:
            mem_lines = []
            for m in symbol_memory:
                mem_lines.append(
                    " - "
                    + m["ts"][:10]
                    + ": "
                    + m.get("side", "?").upper()
                    + " entry="
                    + f"{m.get('entry', 0):.4f}"
                    + " exit="
                    + f"{m.get('exit', 0):.4f}"
                    + " PnL="
                    + f"{m.get('pnl_pct', 0):+.2f}"
                    + "% => "
                    + m.get("outcome", "?")
                )
            memory_str = "\n".join(mem_lines)
        else:
            memory_str = " Aucun trade précédent sur ce symbole."

        whale_str = ""
        if market.whale_signal > 1_000_000:
            whale_str = " Baleine: +" + f"{market.whale_signal/1e6:.1f}" + "M$ vers exchanges (bullish)\n"
        elif market.whale_signal < -1_000_000:
            whale_str = " Baleine: -" + f"{abs(market.whale_signal)/1e6:.1f}" + "M$ depuis exchanges (bearish)\n"

        ctx = (market.whale_details or [{}])[0]
        debate = ctx.get("debate", {})
        regime = ctx.get("regime", {})
        news = ctx.get("news", {})
        whales_g = ctx.get("whales", {})
        daily_pnl = ctx.get("daily_pnl", 0.0)
        nb_pos = ctx.get("nb_pos", 0)

        bull_data = debate.get("bull", {})
        bear_data = debate.get("bear", {})
        bull_args = bull_data.get("arguments", [])
        bear_args = bear_data.get("arguments", [])
        bull_conf = bull_data.get("confidence", 0.0)
        bear_risk = bear_data.get("risk_level", "?")
        bull_str = " | ".join(bull_args) if bull_args else "aucun argument disponible"
        bear_str = " | ".join(bear_args) if bear_args else "aucun argument disponible"

        _closes_local = [c for c in (market.closes if hasattr(market, "closes") and market.closes else [])]
        _atr_safe = atr if atr > 0 else price * 0.02
        sr = find_sr_levels(_closes_local or [price], price, _atr_safe)

        sep = "=" * 45
        parts = [
            "Tu es un expert trader crypto sur Hyperliquid futures perpétuels.",
            "Analyse ce marché et prends une décision de trading.",
            "",
            sep,
            "MARCHÉ : " + symbol + "/USDT | " + datetime.now().strftime("%Y-%m-%d %H:%M"),
            sep,
            "",
            "RÉGIME GLOBAL :",
            " Tendance : " + regime.get("trend", "?") + " / vol " + regime.get("volatility", "?"),
            " News sentiment : " + news.get("overall_sentiment", "N/A") + " | Fear/Greed: " + str(news.get("fear_greed", "N/A")),
            " Whales global : " + whales_g.get("sentiment", "N/A"),
            " PnL du jour : " + f"{daily_pnl*100:+.1f}" + "%",
            " Positions ouv. : " + str(nb_pos),
            "",
            "MARCHÉ " + symbol + " :",
            " Prix : " + f"{price:.6f}" + " USDT",
            " Tendance 5h : " + trend_5 + " (" + f"{pct_5h:+.2f}" + "%)",
            " Volume ratio : " + f"{vol_ratio:.2f}" + "x (>1.5 = spike)",
            " Funding rate : " + f"{market.funding_rate:.6f}",
            " Score scanner : " + f"{market.score:.1f}" + "/100",
            whale_str,
            "",
            "INDICATEURS TECHNIQUES :",
            " RSI(14) : " + f"{rsi:.1f}",
            " EMA(20) : " + f"{ema20_val:.6f}" + " (" + ("résistance" if ema20_val > price else "support") + ")",
            " EMA(50) : " + f"{ema50_val:.6f}" + " (" + ("résistance" if ema50_val > price else "support") + ")",
            " EMA20/EMA50 : " + ("HAUSSIER" if ema20_val > ema50_val else "BAISSIER"),
            " Bollinger : " + bb_pos,
            " " + macd_cross,
            " ATR(14) : " + f"{atr:.6f}",
            "",
            "NIVEAUX TECHNIQUES :",
            " Résistances : " + sr["resistances"],
            " Supports : " + sr["supports"],
            " TP suggéré LONG : " + f"{sr['tp_long']:.4f}" + f" (R/R={sr['rr_long']:.1f}:1)",
            " TP suggéré SHORT : " + f"{sr['tp_short']:.4f}" + f" (R/R={sr['rr_short']:.1f}:1)",
            "",
            "DÉBAT BULL (confiance=" + f"{bull_conf:.2f}" + ") :",
            " " + bull_str,
            "",
            "DÉBAT BEAR (risque=" + bear_risk + ") :",
            " " + bear_str,
            "",
            "MES TRADES RÉCENTS SUR " + symbol + " :",
            memory_str,
            "",
            sep,
            "Réponds UNIQUEMENT en JSON valide :",
            '{"side": "buy"|"sell"|"wait", "confidence": 0.0, "sl_atr": 2.0, "tp_price": null, "reason": "..."}',
            "",
            "Règles :",
            "- wait si signal pas clair ou moins de 2 confirmations",
            "- confidence > 0.75 seulement si 3+ confirmations alignées",
            "- tp_price: utilise le TP suggéré si R/R >= 2.0, sinon null (calcul ATR auto)",
            "- sl_atr: 1.5 si faible volatilité, 2.5 si forte volatilité",
            sep,
        ]
        return "\n".join(parts)

    def _call_llm(self, prompt: str, symbol: str) -> Optional[dict]:
        try:
            payload = {
                "model": LOCALAI_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": "Tu es un expert trader crypto. Réponds uniquement en JSON valide.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
                "max_tokens": 300,
            }
            resp = requests.post(LOCALAI_URL, json=payload, timeout=30)
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"].strip()
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start == -1 or end == 0:
                logger.warning("LLM %s: pas de JSON: %s", symbol, raw[:100])
                return None
            decision = json.loads(raw[start:end])
            logger.debug("LLM %s => %s", symbol, decision)
            return decision
        except json.JSONDecodeError as e:
            logger.warning("LLM %s: JSON invalide: %s", symbol, e)
            return None
        except requests.exceptions.ConnectionError:
            logger.error("LocalAI non disponible sur %s", LOCALAI_URL)
            return None
        except Exception as e:
            logger.error("Erreur LLM %s: %s", symbol, e)
            return None

    def _load_memory(self) -> list:
        if Path(MEMORY_FILE).exists():
            try:
                with open(MEMORY_FILE, "r", encoding="utf-8") as f2:
                    data = json.load(f2)
                logger.info("Mémoire trader: %d trades chargés", len(data))
                return data
            except Exception:
                pass
        return []

    def _save_memory(self) -> None:
        try:
            with open(MEMORY_FILE, "w", encoding="utf-8") as f2:
                json.dump(self._memory, f2, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error("Erreur sauvegarde mémoire: %s", e)

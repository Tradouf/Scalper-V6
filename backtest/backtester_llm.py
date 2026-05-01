"""
BacktesterLLM — Salle des Marchés
Backtest basé sur les décisions réelles de l'AgentTrader LLM.
"""
from __future__ import annotations
import json
import logging
import requests
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger("sdm.backtester_llm")

OLLAMA_URL   = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "deepseek-coder-v2:lite"


@dataclass
class LLMBacktestResult:
    symbol:         str
    nb_trades:      int
    total_pnl:      float
    winrate:        float
    profit_factor:  float
    max_drawdown:   float
    avg_confidence: float
    trades:         List[dict] = field(default_factory=list)

    def __str__(self):
        return (f"{self.symbol} trades={self.nb_trades} "
                f"PnL={self.total_pnl:+.2f}% WR={self.winrate*100:.0f}% "
                f"PF={self.profit_factor:.2f} DD={self.max_drawdown:.1f}% "
                f"conf={self.avg_confidence:.2f}")


class BacktesterLLM:

    def __init__(self, exchange_client, step_bars=4, lookback=50):
        self._client   = exchange_client
        self.step_bars = step_bars
        self.lookback  = lookback

    def run(self, symbol, interval="1h", days=14):
        logger.info("BacktesterLLM: %s %s %dd", symbol, interval, days)
        df = self._fetch_ohlcv(symbol, interval, days)
        if df is None or len(df) < self.lookback + 10:
            logger.warning("Donnees insuffisantes pour %s", symbol)
            return None
        df = self._add_indicators(df)

        trades      = []
        position    = None
        confidences = []
        memory      = []

        for i in range(self.lookback, len(df), self.step_bars):
            window = df.iloc[:i+1]
            row    = df.iloc[i]
            price  = float(row["close"])

            if position is not None:
                entry = position["entry"]
                side  = position["side"]
                sl, tp = position["sl"], position["tp"]
                if side == "buy":
                    if float(row["high"]) >= tp:
                        pnl = (tp - entry) / entry
                        trades.append({**position, "exit": tp, "pnl": pnl, "result": "TP"})
                        memory.append({"symbol": symbol, "side": side, "pnl_pct": round(pnl*100,2), "outcome": "WIN"})
                        position = None; continue
                    if float(row["low"]) <= sl:
                        pnl = (sl - entry) / entry
                        trades.append({**position, "exit": sl, "pnl": pnl, "result": "SL"})
                        memory.append({"symbol": symbol, "side": side, "pnl_pct": round(pnl*100,2), "outcome": "LOSS"})
                        position = None; continue
                else:
                    if float(row["low"]) <= tp:
                        pnl = (entry - tp) / entry
                        trades.append({**position, "exit": tp, "pnl": pnl, "result": "TP"})
                        memory.append({"symbol": symbol, "side": side, "pnl_pct": round(pnl*100,2), "outcome": "WIN"})
                        position = None; continue
                    if float(row["high"]) >= sl:
                        pnl = (entry - sl) / entry
                        trades.append({**position, "exit": sl, "pnl": pnl, "result": "SL"})
                        memory.append({"symbol": symbol, "side": side, "pnl_pct": round(pnl*100,2), "outcome": "LOSS"})
                        position = None; continue

            if position is None:
                decision = self._ask_llm(symbol, window, memory)
                if decision is None:
                    continue
                side       = decision.get("side", "wait")
                confidence = float(decision.get("confidence", 0.0))
                if side == "wait" or confidence < 0.60:
                    continue
                confidences.append(confidence)
                atr = float(window["atr"].iloc[-1]) if window["atr"].iloc[-1] > 0 else price * 0.02
                sl_mul = float(decision.get("sl_atr", 2.0))
                tp_mul = float(decision.get("tp_atr", 4.0))
                if side == "buy":
                    sl = price - sl_mul * atr
                    tp = price + tp_mul * atr
                else:
                    sl = price + sl_mul * atr
                    tp = price - tp_mul * atr
                position = {"side": side, "entry": price, "sl": sl, "tp": tp,
                            "confidence": confidence, "reason": decision.get("reason",""), "bar": i}

        if position is not None:
            last_price = float(df.iloc[-1]["close"])
            if position["side"] == "buy":
                pnl = (last_price - position["entry"]) / position["entry"]
            else:
                pnl = (position["entry"] - last_price) / position["entry"]
            trades.append({**position, "exit": last_price, "pnl": pnl, "result": "OPEN_CLOSE"})

        return self._compute_result(symbol, trades, confidences)

    def _ask_llm(self, symbol, window, memory):
        closes = window["close"].tolist()
        price  = closes[-1]
        rsi    = float(window["rsi"].iloc[-1]) if not pd.isna(window["rsi"].iloc[-1]) else 50.0
        ema20  = float(window["ema20"].iloc[-1])
        ema50  = float(window["ema50"].iloc[-1])
        atr    = float(window["atr"].iloc[-1]) if not pd.isna(window["atr"].iloc[-1]) else price*0.02
        macd   = float(window["macd"].iloc[-1]) if not pd.isna(window["macd"].iloc[-1]) else 0.0
        macd_s = float(window["macd_signal"].iloc[-1]) if not pd.isna(window["macd_signal"].iloc[-1]) else 0.0
        vr     = float(window["vol_ratio"].iloc[-1]) if not pd.isna(window["vol_ratio"].iloc[-1]) else 1.0
        bb_u   = float(window["bb_upper"].iloc[-1]) if not pd.isna(window["bb_upper"].iloc[-1]) else price*1.02
        bb_l   = float(window["bb_lower"].iloc[-1]) if not pd.isna(window["bb_lower"].iloc[-1]) else price*0.98
        last5  = closes[-5:] if len(closes)>=5 else closes
        pct5   = (last5[-1]-last5[0])/last5[0]*100
        trend  = "hausse" if last5[-1]>last5[0] else "baisse"
        if bb_u != bb_l:
            bb_pct = (price-bb_l)/(bb_u-bb_l)*100
            if price > bb_u:
                bb_pos = "AU-DESSUS bandes"
            elif price < bb_l:
                bb_pos = "EN-DESSOUS bandes"
            else:
                bb_pos = f"dans bandes {bb_pct:.0f}% du bas"
        else:
            bb_pos = "N/A"
        mem_str = "\n".join(
            [f"  {m['side'].upper()} PnL={m['pnl_pct']:+.1f}% {m['outcome']}" for m in memory[-3:]]
        ) or "  Aucun"
        prompt = (
            f"Expert trader crypto Hyperliquid futures. BACKTEST.\n"
            f"MARCHE: {symbol}  Prix: {price:.6f}  Tendance5h: {trend} ({pct5:+.2f}%)\n"
            f"RSI: {rsi:.1f}  EMA20: {ema20:.6f} ({'>' if ema20>price else '<'} prix)  EMA50: {ema50:.6f}\n"
            f"EMA trend: {'HAUSSIER' if ema20>ema50 else 'BAISSIER'}  Bollinger: {bb_pos}\n"
            f"MACD: {'bullish' if macd>macd_s else 'bearish'}  ATR: {atr:.6f}  Volume: {vr:.2f}x\n"
            f"Trades precedents:\n{mem_str}\n"
            f'JSON uniquement: {{"side":"buy"|"sell"|"wait","confidence":0.0-1.0,"sl_atr":2.0,"tp_atr":4.0,"reason":"court"}}'
        )
        try:
            resp = requests.post(
                OLLAMA_URL,
                json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
                      "options": {"temperature": 0.1, "num_predict": 120}},
                timeout=30
            )
            raw = resp.json().get("response","").strip()
            s = raw.find("{"); e = raw.rfind("}")+1
            if s == -1 or e == 0:
                return None
            return json.loads(raw[s:e])
        except Exception as ex:
            logger.debug("LLM error: %s", ex)
            return None

    def _fetch_ohlcv(self, symbol, interval, days):
        try:
            candles = self._client.get_ohlcv(symbol, interval=interval, days=days)
            if not candles:
                return None
            df = pd.DataFrame(candles, columns=["ts","open","high","low","close","volume"])
            df = df.astype({"open":float,"high":float,"low":float,"close":float,"volume":float})
            df.sort_values("ts", inplace=True)
            df.reset_index(drop=True, inplace=True)
            return df
        except Exception as e:
            logger.warning("OHLCV %s: %s", symbol, e)
            return None

    def _add_indicators(self, df):
        delta = df["close"].diff()
        gain  = delta.clip(lower=0)
        loss  = (-delta).clip(lower=0)
        df["rsi"] = 100 - (
            100 / (1 + (
                gain.ewm(com=13, adjust=False).mean() /
                loss.ewm(com=13, adjust=False).mean().replace(0, np.nan)
            ))
        )
        df["bb_mid"]   = df["close"].rolling(20).mean()
        bb_std         = df["close"].rolling(20).std()
        df["bb_upper"] = df["bb_mid"] + 2 * bb_std
        df["bb_lower"] = df["bb_mid"] - 2 * bb_std
        df["ema20"]    = df["close"].ewm(span=20, adjust=False).mean()
        df["ema50"]    = df["close"].ewm(span=50, adjust=False).mean()
        hl  = df["high"] - df["low"]
        hc  = (df["high"] - df["close"].shift()).abs()
        lc  = (df["low"]  - df["close"].shift()).abs()
        df["atr"]         = pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(14).mean()
        ema12             = df["close"].ewm(span=12, adjust=False).mean()
        ema26             = df["close"].ewm(span=26, adjust=False).mean()
        df["macd"]        = ema12 - ema26
        df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
        df["vol_ratio"]   = df["volume"] / df["volume"].rolling(20).mean()
        return df

    def _compute_result(self, symbol, trades, confidences):
        if not trades:
            return LLMBacktestResult(
                symbol=symbol, nb_trades=0, total_pnl=0.0,
                winrate=0.0, profit_factor=0.0, max_drawdown=0.0,
                avg_confidence=0.0
            )
        pnls    = [t["pnl"] for t in trades]
        winners = [p for p in pnls if p > 0]
        losers  = [p for p in pnls if p < 0]
        gp = sum(winners) if winners else 0.0
        gl = abs(sum(losers)) if losers else 0.0
        pf = gp / gl if gl > 0 else (999.0 if gp > 0 else 0.0)
        cum  = np.cumsum(pnls)
        peak = np.maximum.accumulate(cum)
        mdd  = float(np.max(peak - cum)) * 100
        return LLMBacktestResult(
            symbol=symbol,
            nb_trades=len(trades),
            total_pnl=round(sum(pnls)*100, 2),
            winrate=round(len(winners)/len(pnls), 3),
            profit_factor=round(pf, 2),
            max_drawdown=round(mdd, 2),
            avg_confidence=round(sum(confidences)/len(confidences), 2) if confidences else 0.0,
            trades=trades
        )

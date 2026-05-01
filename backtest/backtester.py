"""
Backtester — Salle des Marchés
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import List, Optional
import pandas as pd
import numpy as np

logger = logging.getLogger("sdm.backtester")

@dataclass
class BacktestResult:
    symbol:        str
    strategy:      str
    nb_trades:     int
    total_pnl:     float
    winrate:       float
    profit_factor: float
    max_drawdown:  float
    trades:        List[dict] = field(default_factory=list)

class Backtester:
    def __init__(self, exchange_client):
        self._client = exchange_client

    def run(self, symbol, interval="1h", days=30, strategy="momentum", tp_pct=0.04, sl_pct=0.02):
        df = self._fetch_ohlcv(symbol, interval, days)
        if df is None or len(df) < 50:
            raise ValueError(f"Données insuffisantes pour {symbol}")
        df = self._add_indicators(df)
        signals = self._signals_momentum(df) if strategy == "momentum" else self._signals_trend(df)
        trades = self._simulate(df, signals, tp_pct, sl_pct)
        return self._compute_result(symbol, strategy, trades)

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
            logger.warning("OHLCV fetch error %s: %s", symbol, e)
            return None

    def _add_indicators(self, df):
        delta = df["close"].diff()
        gain  = delta.clip(lower=0)
        loss  = (-delta).clip(lower=0)
        avg_gain = gain.ewm(com=13, adjust=False).mean()
        avg_loss = loss.ewm(com=13, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        df["rsi"] = 100 - (100 / (1 + rs))
        df["bb_mid"]   = df["close"].rolling(20).mean()
        bb_std         = df["close"].rolling(20).std()
        df["bb_upper"] = df["bb_mid"] + 2 * bb_std
        df["bb_lower"] = df["bb_mid"] - 2 * bb_std
        df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
        df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
        return df

    def _signals_momentum(self, df):
        buy  = (df["rsi"] < 35) & (df["close"] <= df["bb_lower"])
        sell = (df["rsi"] > 65) & (df["close"] >= df["bb_upper"])
        signals = pd.Series(0, index=df.index)
        signals[buy] = 1
        signals[sell] = -1
        return signals

    def _signals_trend(self, df):
        buy  = (df["ema20"] > df["ema50"]) & (df["ema20"].shift(1) <= df["ema50"].shift(1))
        sell = (df["ema20"] < df["ema50"]) & (df["ema20"].shift(1) >= df["ema50"].shift(1))
        signals = pd.Series(0, index=df.index)
        signals[buy] = 1
        signals[sell] = -1
        return signals

    def _simulate(self, df, signals, tp_pct, sl_pct):
        trades = []
        position = None
        for i in range(len(df)):
            row = df.iloc[i]
            if position is not None:
                entry = position["entry"]
                side  = position["side"]
                if side == "buy":
                    if row["high"] >= entry * (1 + tp_pct):
                        trades.append({**position, "exit": entry*(1+tp_pct), "pnl": tp_pct, "result": "TP"})
                        position = None; continue
                    if row["low"] <= entry * (1 - sl_pct):
                        trades.append({**position, "exit": entry*(1-sl_pct), "pnl": -sl_pct, "result": "SL"})
                        position = None; continue
                else:
                    if row["low"] <= entry * (1 - tp_pct):
                        trades.append({**position, "exit": entry*(1-tp_pct), "pnl": tp_pct, "result": "TP"})
                        position = None; continue
                    if row["high"] >= entry * (1 + sl_pct):
                        trades.append({**position, "exit": entry*(1+sl_pct), "pnl": -sl_pct, "result": "SL"})
                        position = None; continue
            if position is None and signals.iloc[i] != 0:
                position = {"side": "buy" if signals.iloc[i]==1 else "sell", "entry": row["close"], "bar": i}
        return trades

    def _compute_result(self, symbol, strategy, trades):
        if not trades:
            return BacktestResult(symbol=symbol, strategy=strategy, nb_trades=0,
                                  total_pnl=0.0, winrate=0.0, profit_factor=0.0, max_drawdown=0.0)
        pnls    = [t["pnl"] for t in trades]
        winners = [p for p in pnls if p > 0]
        losers  = [p for p in pnls if p < 0]
        gp = sum(winners) if winners else 0.0
        gl = abs(sum(losers)) if losers else 0.0
        pf = gp / gl if gl > 0 else (999.0 if gp > 0 else 0.0)
        cumulative = np.cumsum(pnls)
        peak = np.maximum.accumulate(cumulative)
        max_dd = float(np.max(peak - cumulative)) * 100
        return BacktestResult(
            symbol=symbol, strategy=strategy, nb_trades=len(trades),
            total_pnl=round(sum(pnls)*100, 2), winrate=round(len(winners)/len(pnls), 3),
            profit_factor=round(pf, 2), max_drawdown=round(max_dd, 2), trades=trades)

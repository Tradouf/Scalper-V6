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

    # Frais HL par défaut : taker ~0,045% par côté (round-trip ≈ 0,09%). Déduits
    # de chaque trade (entrée + sortie) dans _simulate → les métriques sont NETTES.
    DEFAULT_FEE_PCT = 0.00045

    def run(self, symbol, interval="1h", days=30, strategy="momentum", tp_pct=0.04, sl_pct=0.02,
            fast=5, slow=34, x_long=65.0, x_short=60.0, fee_pct=DEFAULT_FEE_PCT):
        df = self._fetch_ohlcv(symbol, interval, days)
        if df is None or len(df) < 50:
            raise ValueError(f"Données insuffisantes pour {symbol}")
        return self.run_on_df(df, symbol, strategy, tp_pct, sl_pct,
                              fast=fast, slow=slow, x_long=x_long, x_short=x_short, fee_pct=fee_pct)

    def run_on_df(self, df, symbol, strategy="momentum", tp_pct=0.04, sl_pct=0.02,
                  fast=5, slow=34, x_long=65.0, x_short=60.0, cvd_lookback=20,
                  fee_pct=DEFAULT_FEE_PCT, exit_mode="tp_sl", hold_bars=0, trail_pct=0.0):
        """Backtest sur un DataFrame OHLCV déjà chargé (sans fetch réseau).
        Permet au walk-forward de rejouer des tranches sans re-télécharger.
        `exit_mode`/`hold_bars`/`trail_pct` : voir _simulate (sortie alternative)."""
        df = self._add_indicators(df)
        signals = self._signals_for(df, strategy, fast=fast, slow=slow, x_long=x_long,
                                    x_short=x_short, cvd_lookback=cvd_lookback)
        trades = self._simulate(df, signals, tp_pct, sl_pct, fee_pct=fee_pct,
                                exit_mode=exit_mode, hold_bars=hold_bars, trail_pct=trail_pct)
        return self._compute_result(symbol, strategy, trades)

    def _signals_for(self, df, strategy, fast=5, slow=34, x_long=65.0, x_short=60.0, cvd_lookback=20):
        if strategy == "ao":
            return self._signals_ao(df, x_long=x_long, x_short=x_short, fast=fast, slow=slow)
        if strategy == "ao_zerocross":
            return self._signals_ao_zerocross(df, fast=fast, slow=slow)
        if strategy == "cvd_divergence":
            return self._signals_cvd_divergence(df, lookback=cvd_lookback)
        if strategy == "cvd_breakout":
            return self._signals_cvd_breakout(df, lookback=cvd_lookback)
        if strategy == "momentum":
            return self._signals_momentum(df)
        return self._signals_trend(df)

    def _signals_cvd_divergence(self, df, lookback=20):
        """Divergence prix/CVD (épuisement d'agresseurs). Sur les `lookback` barres
        précédentes (fenêtre fermée, shift(1) = pas de look-ahead) :
          - prix fait un nouveau plus-HAUT mais le CVD NE confirme PAS (sous son max)
            → divergence baissière → SHORT.
          - prix fait un nouveau plus-BAS mais le CVD au-dessus de son min
            → divergence haussière → LONG.
        Nécessite une colonne `cvd` (cf. backtest/orderflow.py)."""
        import numpy as np
        if "cvd" not in df.columns:
            return pd.Series(0, index=df.index)
        close, cvd = df["close"], df["cvd"]
        roll_close_max = close.shift(1).rolling(lookback).max()
        roll_close_min = close.shift(1).rolling(lookback).min()
        roll_cvd_max = cvd.shift(1).rolling(lookback).max()
        roll_cvd_min = cvd.shift(1).rolling(lookback).min()
        bear_div = (close > roll_close_max) & (cvd < roll_cvd_max)   # prix HH, CVD pas confirmé
        bull_div = (close < roll_close_min) & (cvd > roll_cvd_min)   # prix LL, CVD pas confirmé
        signals = pd.Series(0, index=df.index)
        signals[bear_div] = -1
        signals[bull_div] = 1
        return signals

    def _signals_cvd_breakout(self, df, lookback=20):
        """Breakout CONFIRMÉ par l'order-flow (thèse de continuation, l'inverse de la
        divergence). Prix ET CVD font un nouveau plus-haut sur `lookback` → LONG ;
        prix ET CVD un nouveau plus-bas → SHORT. shift(1) = pas de look-ahead."""
        if "cvd" not in df.columns:
            return pd.Series(0, index=df.index)
        close, cvd = df["close"], df["cvd"]
        c_max = close.shift(1).rolling(lookback).max()
        c_min = close.shift(1).rolling(lookback).min()
        v_max = cvd.shift(1).rolling(lookback).max()
        v_min = cvd.shift(1).rolling(lookback).min()
        long_brk = (close > c_max) & (cvd > v_max)   # prix HH confirmé par CVD HH
        short_brk = (close < c_min) & (cvd < v_min)
        signals = pd.Series(0, index=df.index)
        signals[long_brk] = 1
        signals[short_brk] = -1
        return signals

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

    def _signals_ao(self, df, x_long=65.0, x_short=60.0, fast=5, slow=34):
        """Awesome Oscillator (AO = SMA(median,fast) − SMA(median,slow)).
        LONG  : AO décroît ∧ AO < −x_long ∧ bougie verte.
        SHORT : AO croît   ∧ AO > +x_short ∧ bougie rouge.
        x_long/x_short = magnitudes positives. Cf. AO_STRATEGY_PLAN.md."""
        median = (df["high"] + df["low"]) / 2.0
        ao = median.rolling(fast).mean() - median.rolling(slow).mean()
        ao_prev = ao.shift(1)
        ao_red = ao < ao_prev      # AO décroît
        ao_green = ao > ao_prev     # AO croît
        candle_green = df["close"] > df["open"]
        candle_red = df["close"] < df["open"]
        buy = ao_red & (ao < -x_long) & candle_green
        sell = ao_green & (ao > x_short) & candle_red
        signals = pd.Series(0, index=df.index)
        signals[buy] = 1
        signals[sell] = -1
        return signals

    def _signals_ao_zerocross(self, df, fast=5, slow=34):
        """AO zero-cross classique (motif TradingView) : pas de seuil.
        LONG quand l'AO franchit 0 à la HAUSSE, SHORT quand il franchit 0 à la BAISSE."""
        median = (df["high"] + df["low"]) / 2.0
        ao = median.rolling(fast).mean() - median.rolling(slow).mean()
        ao_prev = ao.shift(1)
        buy = (ao > 0) & (ao_prev <= 0)
        sell = (ao < 0) & (ao_prev >= 0)
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

    def _simulate(self, df, signals, tp_pct, sl_pct, fee_pct=0.0,
                  exit_mode="tp_sl", hold_bars=0, trail_pct=0.0):
        """Simule l'enchaînement entrées→sorties. `exit_mode` choisit la SORTIE
        (l'entrée reste la même : on entre au close quand signal≠0 et qu'on est flat) :
          - "tp_sl"   : barrière fixe TP/SL (historique — numériquement INCHANGÉ).
          - "reverse" : sort quand un signal OPPOSÉ apparaît (tient jusqu'au retournement).
          - "time"    : sort après `hold_bars` barres.
          - "trail"   : trailing stop à `trail_pct` du plus-haut (long) / plus-bas (short).
        Hypothèse francois : la barrière TP/SL n'est peut-être pas la bonne sortie ;
        un edge réel devrait survivre à plusieurs sorties (sinon = TP sur-ajusté)."""
        # sl_pct <= 0 (ou None) → pas de stop (sortie TP seul, ex. Awesome Oscillator).
        use_sl = sl_pct is not None and sl_pct > 0
        # Coût round-trip (entrée + sortie). Déduit du pnl brut de chaque trade.
        roundtrip_cost = 2.0 * (fee_pct or 0.0)
        # Réaligne l'index pour les tranches walk-forward (df.iloc + signals.iloc).
        df = df.reset_index(drop=True)
        signals = signals.reset_index(drop=True)
        trades = []
        position = None
        for i in range(len(df)):
            row = df.iloc[i]
            if position is not None:
                entry = position["entry"]
                side  = position["side"]
                if exit_mode == "tp_sl":
                    if side == "buy":
                        if row["high"] >= entry * (1 + tp_pct):
                            trades.append({**position, "exit": entry*(1+tp_pct), "pnl": tp_pct, "result": "TP"})
                            position = None; continue
                        if use_sl and row["low"] <= entry * (1 - sl_pct):
                            trades.append({**position, "exit": entry*(1-sl_pct), "pnl": -sl_pct, "result": "SL"})
                            position = None; continue
                    else:
                        if row["low"] <= entry * (1 - tp_pct):
                            trades.append({**position, "exit": entry*(1-tp_pct), "pnl": tp_pct, "result": "TP"})
                            position = None; continue
                        if use_sl and row["high"] >= entry * (1 + sl_pct):
                            trades.append({**position, "exit": entry*(1+sl_pct), "pnl": -sl_pct, "result": "SL"})
                            position = None; continue
                else:
                    # Modes alternatifs : on calcule un prix de sortie puis le pnl signé.
                    exit_px = None; result = None
                    if side == "buy":
                        position["peak"] = max(position.get("peak", entry), float(row["high"]))
                    else:
                        position["trough"] = min(position.get("trough", entry), float(row["low"]))
                    if exit_mode == "reverse":
                        sig = signals.iloc[i]
                        if (side == "buy" and sig == -1) or (side == "sell" and sig == 1):
                            exit_px, result = float(row["close"]), "REV"
                    elif exit_mode == "time":
                        if hold_bars > 0 and (i - position["bar"]) >= hold_bars:
                            exit_px, result = float(row["close"]), "TIME"
                    elif exit_mode == "trail":
                        if side == "buy" and row["low"] <= position["peak"] * (1 - trail_pct):
                            exit_px, result = position["peak"] * (1 - trail_pct), "TRAIL"
                        elif side == "sell" and row["high"] >= position["trough"] * (1 + trail_pct):
                            exit_px, result = position["trough"] * (1 + trail_pct), "TRAIL"
                    if exit_px is not None:
                        pnl = (exit_px / entry - 1.0) if side == "buy" else (entry / exit_px - 1.0)
                        trades.append({**position, "exit": exit_px, "pnl": float(pnl), "result": result})
                        position = None; continue
            if position is None and signals.iloc[i] != 0:
                position = {"side": "buy" if signals.iloc[i]==1 else "sell", "entry": row["close"], "bar": i}
        # Clôture mark-to-market de la position encore ouverte en fin de données.
        # Essentiel en TP seul (sans SL) : une position prise à contre-tendance ne
        # touche jamais son TP et resterait sinon invisible dans le PnL (elle
        # verrouille aussi le book — aucune nouvelle entrée tant qu'elle est tenue).
        if position is not None:
            last = df.iloc[-1]
            entry = position["entry"]
            if position["side"] == "buy":
                pnl = last["close"] / entry - 1.0
            else:
                pnl = entry / last["close"] - 1.0
            trades.append({**position, "exit": float(last["close"]), "pnl": float(pnl), "result": "EOD"})
        # Frais : déduit le coût round-trip du pnl de chaque trade (métriques nettes).
        if roundtrip_cost:
            for t in trades:
                t["pnl_gross"] = t["pnl"]
                t["pnl"] = t["pnl"] - roundtrip_cost
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

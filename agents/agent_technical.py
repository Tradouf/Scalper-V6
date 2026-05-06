"""
AgentTechnique V5.9 — calcule les indicateurs, les features avancees et le regime.
Utilise les vraies APIs FeatureEngine et RegimeEngine.
"""
from __future__ import annotations
import logging
import numpy as np
import pandas as pd
from typing import Dict, Optional
from .base_agent import BaseAgent
from memory.shared_memory import SharedMemory

try:
    from agents.feature_engine import FeatureEngine
except Exception:
    FeatureEngine = None

try:
    from agents.regime_engine import RegimeEngine
except Exception:
    RegimeEngine = None

logger = logging.getLogger("sdm.technical")


class AgentTechnical(BaseAgent):

    def __init__(self, memory: SharedMemory, exchange_client):
        super().__init__("technical", memory)
        self._client = exchange_client
        self._feature_engine = FeatureEngine(memory=memory) if FeatureEngine else None
        self._regime_engine  = RegimeEngine(memory=memory)  if RegimeEngine  else None

    def analyze(self, symbol: str) -> Dict:
        df = self._fetch_ohlcv(symbol)
        if df is None or len(df) < 60:
            return {}

        ind    = self._compute_indicators(df)
        result = self._interpret(symbol, ind)

        # Promote raw indicators to top level for downstream access
        for k in ("atr", "rsi", "price", "vol_ratio", "macd", "macd_signal",
                  "bb_upper", "bb_lower", "bb_mid", "bb_position"):
            if k not in result:
                result[k] = ind.get(k, 0)
        price = float(ind.get("price", 1) or 1)
        result["macd_hist"] = float(ind.get("macd", 0) or 0) - float(ind.get("macd_signal", 0) or 0)
        result["atr_pct"]   = float(ind.get("atr", 0) or 0) / price * 100

        try:
            self.memory.update_analysis(symbol, "technical", result)
        except Exception:
            pass

        # Features via FeatureEngine
        features = {}
        if self._feature_engine is not None:
            try:
                features = self._feature_engine.compute(symbol, result) or {}
            except Exception as e:
                logger.warning("TECH %s FeatureEngine.compute error: %r", symbol, e)

        # Regime via RegimeEngine
        regime_features = {}
        if self._regime_engine is not None and features:
            try:
                regime_features = self._regime_engine.assess(symbol, features) or {}
            except Exception as e:
                logger.warning("TECH %s RegimeEngine.assess error: %r", symbol, e)

        merged = {**features, **regime_features}

        if merged:
            try:
                self.memory.update_advanced_features(symbol, merged)
            except Exception as e:
                logger.warning("TECH %s update_advanced_features error: %r", symbol, e)

        logger.info(
            "TECH FEATURES %s slope=%s micro=%s vwap_rev=%.4f persist=%.2f latent=%s/%s conf=%.2f",
            symbol,
            merged.get("slope_multi_horizon", "?"),
            merged.get("micro_trend", "?"),
            float(merged.get("vwap_reversion_score", 0.0) or 0.0),
            float(merged.get("regime_persistence_score", 0.0) or 0.0),
            merged.get("latent_trend_state", "?"),
            merged.get("latent_market_state", "?"),
            float(merged.get("latent_confidence", 0.0) or 0.0),
        )

        return result

    def _interpret(self, symbol: str, ind: Dict) -> Dict:
        system = (
            "Tu es analyste technique expert crypto. "
            "Tu analyses des indicateurs et identifies des signaux de trading precis. "
            "Reponds UNIQUEMENT en JSON strict."
        )
        user = (
            f"Symbole: {symbol}\n"
            f"Prix: {ind['price']:.6f}\n"
            f"RSI(14): {ind['rsi']:.1f}\n"
            f"EMA20: {ind['ema20']:.6f} | EMA50: {ind['ema50']:.6f}\n"
            f"EMA croisement: {'HAUSSIER' if ind['ema20']>ind['ema50'] else 'BAISSIER'}\n"
            f"MACD: {ind['macd']:.6f} | Signal: {ind['macd_signal']:.6f}\n"
            f"Bollinger: bas={ind['bb_lower']:.6f} mid={ind['bb_mid']:.6f} haut={ind['bb_upper']:.6f}\n"
            f"Position BB: {ind['bb_position']:.0f}%\n"
            f"ATR: {ind['atr']:.6f}\n"
            f"Volume ratio: {ind['vol_ratio']:.2f}x\n"
            f"Tendance 5 bougies: {ind['trend_5']}\n"
            f"Momentum 5h: {ind['pct_5h']:+.2f}%\n\n"
            "Analyse et donne:\n"
            '{"signal":"buy"|"sell"|"wait",'
            '"confidence":0.0-1.0,'
            '"sl_atr":1.5-3.0,'
            '"tp_atr":2.0-6.0,'
            '"key_levels":{"support":0.0,"resistance":0.0},'
            '"regime":"trending"|"ranging"|"volatile",'
            '"reason":"explication courte"}'
        )
        llm_resp = self._llm(system, user, temperature=0.1, max_tokens=300)
        # Distinguer LLM down (None) d'un parsing raté ou d'une vraie réponse
        # neutre. Sans ça, le consensus traite tech=0.00 comme un signal légitime
        # et le bot reste flat pendant les pics de saturation LocalAI.
        # cf. code_proposals.md 2026-05-05 [INFO] LLM timeouts.
        if llm_resp is None:
            return {"signal": "wait", "confidence": 0.0, "reason": "llm_down",
                    "llm_status": "down", "indicators": ind}
        parsed = self._parse_json(llm_resp)
        if not parsed:
            return {"signal": "wait", "confidence": 0.0, "reason": "parse error",
                    "indicators": ind}
        parsed["indicators"] = ind
        return parsed

    def _fetch_ohlcv(self, symbol: str) -> Optional[pd.DataFrame]:
        try:
            candles = self._client.get_ohlcv(symbol, interval="1h", days=7)
            if not candles:
                return None
            df = pd.DataFrame(candles, columns=["ts","open","high","low","close","volume"])
            df = df.astype({"open":float,"high":float,"low":float,"close":float,"volume":float})
            df.sort_values("ts", inplace=True)
            df.reset_index(drop=True, inplace=True)
            return df
        except Exception as e:
            self.logger.warning("OHLCV %s: %s", symbol, e)
            return None

    def _compute_indicators(self, df: pd.DataFrame) -> Dict:
        close = df["close"]
        price = float(close.iloc[-1])
        delta = close.diff()
        gain  = delta.clip(lower=0)
        loss  = (-delta).clip(lower=0)
        rs    = gain.ewm(com=13, adjust=False).mean() / loss.ewm(com=13, adjust=False).mean().replace(0, np.nan)
        rsi   = float((100 - 100/(1+rs)).iloc[-1])
        ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd        = float((ema12 - ema26).iloc[-1])
        macd_signal = float((ema12 - ema26).ewm(span=9, adjust=False).mean().iloc[-1])
        bb_mid   = float(close.rolling(20).mean().iloc[-1])
        bb_std   = float(close.rolling(20).std().iloc[-1])
        bb_upper = bb_mid + 2*bb_std
        bb_lower = bb_mid - 2*bb_std
        bb_position = (price - bb_lower) / (bb_upper - bb_lower) * 100 if bb_upper != bb_lower else 50
        hl  = df["high"] - df["low"]
        hc  = (df["high"] - df["close"].shift()).abs()
        lc  = (df["low"]  - df["close"].shift()).abs()
        atr = float(pd.concat([hl,hc,lc], axis=1).max(axis=1).rolling(14).mean().iloc[-1])
        vol_ratio = float((df["volume"] / df["volume"].rolling(20).mean()).iloc[-1])
        last5  = close.iloc[-5:].tolist()
        pct_5h = (last5[-1] - last5[0]) / last5[0] * 100
        trend_5 = "hausse" if pct_5h > 0 else "baisse"
        return {
            "price": price, "rsi": rsi if not np.isnan(rsi) else 50.0,
            "ema20": ema20, "ema50": ema50,
            "macd": macd, "macd_signal": macd_signal,
            "bb_upper": bb_upper, "bb_mid": bb_mid, "bb_lower": bb_lower,
            "bb_position": bb_position,
            "atr": atr, "vol_ratio": vol_ratio if not np.isnan(vol_ratio) else 1.0,
            "trend_5": trend_5, "pct_5h": pct_5h,
        }

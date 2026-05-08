"""
Architecture multi-timeframes "trading floor" (2026-05-07).

Trois strates LLM hiérarchisées, chacune sur sa fenêtre :

- StrategistH1   : H1 — biais directionnel macro (cache 5 min)
- TacticalM15    : M15 (proxy M10) — confirme le setup intraday (à chaque cycle)
- ExecutionM1    : M1 — timing d'entrée précis (à chaque cycle)

Consensus : VETO STRICT. Toute strate qui dit "wait" ou contredite → pas de trade.
Ce module N'OUVRE PAS de positions ; il fournit un signal `gate()` que le main
loop utilise comme pré-filtre AVANT le pipeline existant (bull/bear/scalper).
"""
from __future__ import annotations
import logging
import time
from typing import Dict, Optional

import numpy as np
import pandas as pd

from .base_agent import BaseAgent
from memory.shared_memory import SharedMemory

logger = logging.getLogger("sdm.multi_tf")


def _history_snippet(symbol: str, max_chars: int = 200) -> str:
    """Phase C : extrait court de l'historique récent pour injection LLM.
    Les agents reçoivent dans leur prompt : 'tes 10 derniers trades : WR=X% ...'
    Permet l'auto-correction sur patterns perdants."""
    try:
        from agents.scalp_memory import get_scalp_memory
        s = get_scalp_memory().stats_summary(symbol=symbol, n=10)
        if s.get("n", 0) == 0:
            return ""
        return f"Historique récent {symbol} : {s.get('summary', '')}"[:max_chars]
    except Exception:
        return ""


# ── OHLCV CACHE ───────────────────────────────────────────────────────────────
# Cache global partagé entre les 3 agents pour économiser les API calls.
# Chaque entrée : {(symbol, interval): (timestamp, dataframe)}
_OHLCV_CACHE: Dict[tuple, tuple] = {}


def fetch_ohlcv_cached(client, symbol: str, interval: str, limit: int, ttl_sec: float) -> Optional[pd.DataFrame]:
    """Récupère OHLCV avec cache TTL. Retourne None en cas d'échec."""
    key = (symbol.upper(), interval)
    now = time.time()
    cached = _OHLCV_CACHE.get(key)
    if cached and (now - cached[0] < ttl_sec):
        return cached[1]
    try:
        candles = client.get_candles(symbol, interval=interval, limit=limit)
        if not candles:
            return None
        # get_candles retourne une liste de dicts {'open','high','low','close','vol','time'}
        df = pd.DataFrame(candles)
        if df.empty:
            return None
        # Renommage cohérent + tri chronologique
        df = df.rename(columns={"vol": "volume", "time": "ts"})
        for c in ("open", "high", "low", "close", "volume"):
            if c not in df.columns:
                logger.warning("fetch_ohlcv_cached(%s,%s): colonne %s manquante", symbol, interval, c)
                return None
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["open", "high", "low", "close"]).sort_values("ts").reset_index(drop=True)
        if len(df) < 10:
            return None
        _OHLCV_CACHE[key] = (now, df)
        return df
    except Exception as e:
        logger.warning("fetch_ohlcv_cached(%s,%s): %r", symbol, interval, e)
        return None


# ── INDICATEURS RÉUTILISABLES ─────────────────────────────────────────────────
def compute_basic_indicators(df: pd.DataFrame, rsi_period: int = 14,
                              ema_fast: int = 20, ema_slow: int = 50,
                              slope_short: int = 10, slope_long: int = 100) -> Dict:
    """Suite d'indicateurs adaptable selon la strate (period configurable).

    slope_short : pente sur fenêtre courte (lecture du moment)
    slope_long  : pente sur fenêtre longue (contexte multi-temporel) — clé
                  pour détecter un trend doux (ex: BTC +5%/semaine sur H1).
    """
    close = df["close"]
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(rsi_period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(rsi_period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = float((100 - 100 / (1 + rs)).iloc[-1])

    ema_f = float(close.ewm(span=ema_fast, adjust=False).mean().iloc[-1])
    ema_s = float(close.ewm(span=ema_slow, adjust=False).mean().iloc[-1])
    price = float(close.iloc[-1])

    # Volatilité ATR simplifiée
    high, low = df["high"], df["low"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = float(tr.rolling(rsi_period).mean().iloc[-1])
    atr_pct = (atr / price) if price > 0 else 0.0

    # Volume relatif (current vs rolling 20)
    vol_ratio = float((df["volume"].iloc[-1] / df["volume"].rolling(20).mean().iloc[-1])
                      if not pd.isna(df["volume"].rolling(20).mean().iloc[-1]) else 1.0)

    # Slopes multi-fenêtre (court + long pour le contexte multi-temporel)
    def _slope(window):
        n = min(window, len(close))
        if n < 2:
            return 0.0
        sub = close.iloc[-n:].values
        return (sub[-1] - sub[0]) / max(abs(sub[0]), 1e-9)

    return {
        "rsi": rsi if not np.isnan(rsi) else 50.0,
        "ema_fast": ema_f,
        "ema_slow": ema_s,
        "ema_cross": "bull" if ema_f > ema_s else "bear",
        "price": price,
        "atr": atr if not np.isnan(atr) else 0.0,
        "atr_pct": atr_pct,
        "vol_ratio": vol_ratio if not np.isnan(vol_ratio) else 1.0,
        "slope_short": _slope(slope_short),
        "slope_long":  _slope(slope_long),
        "slope_short_n": slope_short,
        "slope_long_n":  slope_long,
    }


# ── BASE CLASSE ──────────────────────────────────────────────────────────────
class _MultiTFAgent(BaseAgent):
    """Mère commune : fetch + compute + LLM call."""
    INTERVAL: str = "1h"
    LIMIT: int = 168
    TTL_SEC: float = 30
    RSI_PERIOD: int = 14
    EMA_FAST: int = 20
    EMA_SLOW: int = 50
    SLOPE_SHORT: int = 10   # fenêtre "lecture du moment"
    SLOPE_LONG:  int = 100  # fenêtre "contexte trend" (sera adapté par strate)

    def __init__(self, name: str, memory: SharedMemory, exchange_client):
        super().__init__(name, memory)
        self._client = exchange_client

    def _fetch_indicators(self, symbol: str) -> Optional[Dict]:
        df = fetch_ohlcv_cached(self._client, symbol, self.INTERVAL, self.LIMIT, self.TTL_SEC)
        if df is None or len(df) < max(self.EMA_SLOW, self.RSI_PERIOD) + 5:
            return None
        return compute_basic_indicators(df, self.RSI_PERIOD, self.EMA_FAST, self.EMA_SLOW,
                                        self.SLOPE_SHORT, self.SLOPE_LONG)

    def _llm_call(self, system: str, user: str) -> Dict:
        """Appelle le LLM, parse JSON, gère LLM down."""
        resp = self._llm(system, user, temperature=0.15, max_tokens=160)
        if resp is None:
            return {"signal": "wait", "confidence": 0.0, "reason": "llm_down",
                    "llm_status": "down"}
        parsed = self._parse_json(resp)
        if not parsed or "signal" not in parsed:
            return {"signal": "wait", "confidence": 0.0, "reason": "parse_error"}
        # Normalise
        sig = str(parsed.get("signal", "wait")).lower()
        if sig not in ("buy", "sell", "wait"):
            sig = "wait"
        try:
            conf = float(parsed.get("confidence", 0))
        except (ValueError, TypeError):
            conf = 0.0
        return {"signal": sig, "confidence": max(0.0, min(1.0, conf)),
                "reason": str(parsed.get("reason", ""))[:80]}


# ── 1) STRATÈGE H1 ────────────────────────────────────────────────────────────
class StrategistH1(_MultiTFAgent):
    """Définit le BIAIS macro. Cache 5 min (lent → on ne recalcule pas tout le temps)."""
    INTERVAL = "1h"
    LIMIT = 168          # 7 jours
    TTL_SEC = 300        # cache 5 min
    RSI_PERIOD = 14
    EMA_FAST = 20
    EMA_SLOW = 50
    SLOPE_SHORT = 24     # 24 dernières heures
    SLOPE_LONG  = 168    # 7 jours complets (= toute la fenêtre)

    def __init__(self, memory: SharedMemory, exchange_client):
        super().__init__("strategist_h1", memory, exchange_client)

    def analyze(self, symbol: str) -> Dict:
        ind = self._fetch_indicators(symbol)
        if ind is None:
            return {"signal": "wait", "confidence": 0.0, "reason": "no_data", "tf": "H1"}

        system = (
            "Tu es portfolio manager senior. Tu analyses sur base H1 pour donner "
            "un BIAIS directionnel macro. Le SIGNAL DOMINANT est le slope LONG "
            "(7j) : si le marché a fait +5% en 7j, le bias est BULL même si les "
            "dernières heures pull-back. WAIT seulement si slope long < ±2%."
        )
        hist = _history_snippet(symbol)
        user = (
            f"H1 indicators on {symbol}:\n"
            f"  Price={ind['price']:.4f}\n"
            f"  RSI(14)={ind['rsi']:.1f}\n"
            f"  EMA20={ind['ema_fast']:.4f} EMA50={ind['ema_slow']:.4f} ({ind['ema_cross']} cross)\n"
            f"  ATR%={ind['atr_pct']*100:.2f}%\n"
            f"  Slope court ({ind['slope_short_n']}h)={ind['slope_short']*100:+.2f}%  ← lecture récente\n"
            f"  Slope long  ({ind['slope_long_n']}h)={ind['slope_long']*100:+.2f}%   ← TREND DOMINANT\n"
            + (f"\n{hist}\n" if hist else "")
            + f"\nQuel BIAIS macro pour les 4-12 prochaines heures ?\n"
            f"Règle : si |slope long| > 2% → BUY/SELL aligné, sinon WAIT.\n"
            f"Si l'historique récent montre un pattern perdant (ex: trail_hit_profit dominant en perte), "
            f"sois plus exigeant ou WAIT.\n"
            'JSON: {"signal":"buy"|"sell"|"wait","confidence":0.0-1.0,"reason":"max 10 mots"}'
        )
        out = self._llm_call(system, user)
        out["tf"] = "H1"
        out["indicators"] = ind
        return out


# ── 2) TACTICAL M15 (proxy M10) ───────────────────────────────────────────────
class TacticalM15(_MultiTFAgent):
    """Confirme/infirme le biais H1 sur le moyen terme. Cache court (30s)."""
    INTERVAL = "15m"
    LIMIT = 96           # 24 heures
    TTL_SEC = 30
    RSI_PERIOD = 9
    EMA_FAST = 9
    EMA_SLOW = 21
    SLOPE_SHORT = 12     # 3 dernières heures
    SLOPE_LONG  = 96     # 24h complètes

    def __init__(self, memory: SharedMemory, exchange_client):
        super().__init__("tactical_m15", memory, exchange_client)

    def analyze(self, symbol: str, h1_bias: Optional[str] = None) -> Dict:
        ind = self._fetch_indicators(symbol)
        if ind is None:
            return {"signal": "wait", "confidence": 0.0, "reason": "no_data", "tf": "M15"}

        bias_hint = f"\nBIAIS H1 imposé par le stratège : {h1_bias.upper()}" if h1_bias else ""
        system = (
            "Tu es trader tactique intraday. Tu analyses sur base M15 (24h) "
            "pour valider un setup d'entrée court terme : pullback, breakout, "
            "retest. Tu confirmes le biais H1 si conditions présentes, sinon WAIT."
        )
        sh_h = ind['slope_short_n'] * 0.25   # M15 : 1 bougie = 0.25h
        lg_h = ind['slope_long_n']  * 0.25
        hist = _history_snippet(symbol)
        user = (
            f"M15 indicators on {symbol}:\n"
            f"  Price={ind['price']:.4f}\n"
            f"  RSI(9)={ind['rsi']:.1f}\n"
            f"  EMA9={ind['ema_fast']:.4f} EMA21={ind['ema_slow']:.4f} ({ind['ema_cross']})\n"
            f"  ATR%={ind['atr_pct']*100:.2f}%\n"
            f"  Slope court ({sh_h:.0f}h)={ind['slope_short']*100:+.2f}%\n"
            f"  Slope long  ({lg_h:.0f}h)={ind['slope_long']*100:+.2f}%\n"
            f"  Volume relatif={ind['vol_ratio']:.2f}x"
            f"{bias_hint}\n"
            + (f"{hist}\n" if hist else "")
            + f"\nLe setup intraday valide-t-il une entrée alignée avec le biais H1 ? (sinon WAIT)\n"
            'JSON: {"signal":"buy"|"sell"|"wait","confidence":0.0-1.0,"reason":"max 10 mots"}'
        )
        out = self._llm_call(system, user)
        out["tf"] = "M15"
        out["indicators"] = ind
        return out


# ── 3) EXECUTION M1 ───────────────────────────────────────────────────────────
class ExecutionM1(_MultiTFAgent):
    """Timing précis. Cache court (15s)."""
    INTERVAL = "1m"
    LIMIT = 120          # 2 heures
    TTL_SEC = 15
    RSI_PERIOD = 7
    EMA_FAST = 5
    EMA_SLOW = 13
    SLOPE_SHORT = 10     # 10 dernières minutes
    SLOPE_LONG  = 60     # 1h complète

    def __init__(self, memory: SharedMemory, exchange_client):
        super().__init__("execution_m1", memory, exchange_client)

    def analyze(self, symbol: str, h1_bias: Optional[str] = None,
                m15_signal: Optional[str] = None) -> Dict:
        ind = self._fetch_indicators(symbol)
        if ind is None:
            return {"signal": "wait", "confidence": 0.0, "reason": "no_data", "tf": "M1"}

        ctx = ""
        if h1_bias:
            ctx += f"\n  H1 bias = {h1_bias.upper()}"
        if m15_signal:
            ctx += f"\n  M15 signal = {m15_signal.upper()}"

        system = (
            "Tu es scalper d'exécution. Tu valides le TIMING d'entrée sur base "
            "M1 (2h). Tu cherches : momentum tick frais, vol burst, pas de "
            "divergence M1. Si momentum baisse ou divergence avec H1/M15 → WAIT."
        )
        hist = _history_snippet(symbol)
        user = (
            f"M1 indicators on {symbol}:\n"
            f"  Price={ind['price']:.4f}\n"
            f"  RSI(7)={ind['rsi']:.1f}\n"
            f"  EMA5={ind['ema_fast']:.4f} EMA13={ind['ema_slow']:.4f} ({ind['ema_cross']})\n"
            f"  ATR%={ind['atr_pct']*100:.3f}%\n"
            f"  Slope court ({ind['slope_short_n']}m)={ind['slope_short']*100:+.3f}%\n"
            f"  Slope long  ({ind['slope_long_n']}m)={ind['slope_long']*100:+.3f}%\n"
            f"  Volume relatif={ind['vol_ratio']:.2f}x"
            f"{ctx}\n"
            + (f"{hist}\n" if hist else "")
            + f"\nLe timing intra-minute est-il favorable pour entrer dans le sens H1+M15 ?\n"
            'JSON: {"signal":"buy"|"sell"|"wait","confidence":0.0-1.0,"reason":"max 10 mots"}'
        )
        out = self._llm_call(system, user)
        out["tf"] = "M1"
        out["indicators"] = ind
        return out


# ── GATE : VETO STRICT ────────────────────────────────────────────────────────
def strate_gate(h1: Dict, m15: Dict, m1: Dict) -> Dict:
    """
    Combine les 3 strates avec veto strict.

    Sortie :
        {
            "side": "buy"|"sell"|"wait",
            "confidence": float,         # = min des 3 confs si même direction
            "reason": str,
            "h1_signal": ..., "m15_signal": ..., "m1_signal": ...,
            "veto": str|None,            # raison du veto si applicable
            "llm_status": "down"|None,   # propagé pour distinguer LLM saturé
        }
    """
    # Détection LLM down propagée
    llm_down = any(s.get("llm_status") == "down" for s in (h1, m15, m1))
    h1_sig = h1.get("signal", "wait")
    m15_sig = m15.get("signal", "wait")
    m1_sig = m1.get("signal", "wait")

    base = {
        "h1_signal": h1_sig, "m15_signal": m15_sig, "m1_signal": m1_sig,
        "h1_conf": float(h1.get("confidence", 0) or 0),
        "m15_conf": float(m15.get("confidence", 0) or 0),
        "m1_conf": float(m1.get("confidence", 0) or 0),
        "llm_status": "down" if llm_down else None,
    }

    if llm_down:
        return {**base, "side": "wait", "confidence": 0.0,
                "veto": "llm_down", "reason": "LLM unavailable on at least one strate"}

    # Veto si une strate dit wait
    if h1_sig == "wait":
        return {**base, "side": "wait", "confidence": 0.0,
                "veto": "h1_wait", "reason": "H1 strategist no bias"}
    if m15_sig == "wait":
        return {**base, "side": "wait", "confidence": 0.0,
                "veto": "m15_wait", "reason": "M15 setup not present"}
    if m1_sig == "wait":
        return {**base, "side": "wait", "confidence": 0.0,
                "veto": "m1_wait", "reason": "M1 timing not favorable"}

    # Toutes les strates ont un signal (buy ou sell). Vérifie cohérence.
    if not (h1_sig == m15_sig == m1_sig):
        return {**base, "side": "wait", "confidence": 0.0,
                "veto": "disagreement",
                "reason": f"strates disagree H1={h1_sig} M15={m15_sig} M1={m1_sig}"}

    # Conjonction : confiance = min des trois (le plus faible maillon)
    conf = min(base["h1_conf"], base["m15_conf"], base["m1_conf"])
    return {**base, "side": h1_sig, "confidence": conf, "veto": None,
            "reason": f"3-strates {h1_sig.upper()} (min conf {conf:.2f})"}

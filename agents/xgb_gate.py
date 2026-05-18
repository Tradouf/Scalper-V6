"""
XGB Gate — filtre prédictif sur les entrées scalp (2026-05-18).

Charge un modèle XGBoost entraîné par backtest_alpha.py et l'utilise
en inférence pour rejeter les trades à faible probabilité de gain
(top-quantile filter).

Pipeline d'inférence :
  1. Au moment d'une décision d'entrée (après strate gate H1+M15+M1)
  2. Fetch OHLCV H1+M15 (cache partagé via multi_tf.fetch_ohlcv_cached)
  3. Calcule RSI / EMA / ATR / slopes (mêmes formules que le backtest)
  4. Construit le vecteur features dans l'ordre exact du modèle
  5. Renvoie proba_win + décision "go"/"skip"

Activation : XGB_GATE_ENABLED dans settings.py.
Seuil : XGB_GATE_THRESHOLD (default 0.55 = ~top 50% proba ; 0.62 = top 25%).
"""
from __future__ import annotations
import logging
import os
import time
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("sdm.xgb_gate")

MODEL_PATH = Path(__file__).parent.parent / "memory" / "xgb_gate_model.pkl"


def _compute_indicators(df: pd.DataFrame, rsi_n: int, ema_fast: int, ema_slow: int,
                        slope_short: int, slope_long: int) -> Dict:
    """Reproduit EXACTEMENT compute_indicators() de backtest_alpha.py.
    Format aligné sur les colonnes du modèle entraîné (ema_cross int 0/1)."""
    if df is None or len(df) < max(ema_slow, rsi_n, slope_long) + 2:
        return {}
    close = df["close"]
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(rsi_n).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(rsi_n).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = float((100 - 100 / (1 + rs)).iloc[-1])
    if np.isnan(rsi):
        rsi = 50.0

    ema_f = float(close.ewm(span=ema_fast, adjust=False).mean().iloc[-1])
    ema_s = float(close.ewm(span=ema_slow, adjust=False).mean().iloc[-1])
    price = float(close.iloc[-1])

    high, low = df["high"], df["low"]
    pc = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    atr = float(tr.rolling(rsi_n).mean().iloc[-1])
    atr_pct = atr / price if price > 0 else 0.0
    vol_mean = df["volume"].rolling(20).mean().iloc[-1]
    vol_ratio = float(df["volume"].iloc[-1] / vol_mean) if pd.notna(vol_mean) and vol_mean > 0 else 1.0

    def slope(window):
        n = min(window, len(close))
        if n < 2:
            return 0.0
        sub = close.iloc[-n:].values
        return (sub[-1] - sub[0]) / max(abs(sub[0]), 1e-9)

    return {
        "rsi": rsi,
        "ema_fast": ema_f,
        "ema_slow": ema_s,
        "ema_cross": 1 if ema_f > ema_s else 0,   # ← int, comme dans le backtest
        "atr_pct": atr_pct,
        "vol_ratio": vol_ratio if not np.isnan(vol_ratio) else 1.0,
        "slope_short": slope(slope_short),
        "slope_long": slope(slope_long),
    }


class XGBGate:
    """Filtre XGBoost. Singleton, model chargé au boot."""

    def __init__(self, model_path: Path = MODEL_PATH):
        self._model = None
        self._cols: list = []
        self._meta: Dict = {}
        self._load(model_path)

    def _load(self, model_path: Path) -> None:
        if not model_path.exists():
            logger.warning("XGB Gate : modèle introuvable %s (gate désactivé)", model_path)
            return
        try:
            import joblib
            artifact = joblib.load(model_path)
            self._model = artifact["model"]
            self._cols = list(artifact["feature_cols"])
            self._meta = {
                "trained_at": artifact.get("trained_at"),
                "n_train": artifact.get("n_train"),
                "cv_acc": artifact.get("cv_mean_acc"),
                "cv_auc": artifact.get("cv_mean_auc"),
                "wr_train": artifact.get("win_rate_train"),
            }
            age_h = (time.time() - (self._meta["trained_at"] or 0)) / 3600
            logger.info(
                "XGB Gate chargé : %d features, n_train=%s, AUC=%.3f, WR_train=%.1f%%, age=%.1fh",
                len(self._cols), self._meta["n_train"], self._meta["cv_auc"] or 0,
                (self._meta["wr_train"] or 0) * 100, age_h,
            )
        except Exception as e:
            logger.error("XGB Gate : load failed : %r", e)
            self._model = None

    def is_ready(self) -> bool:
        return self._model is not None and bool(self._cols)

    def evaluate(self, client, symbol: str, side: str) -> Optional[Dict]:
        """Calcule proba_win pour un trade {symbol, side}.

        Retourne {"proba_win": float, "features_ok": bool} ou None si problème.
        side : "buy" ou "sell" (entry direction).
        """
        if not self.is_ready():
            return None
        try:
            # Lazy import pour éviter le coût au boot
            from agents.multi_tf import fetch_ohlcv_cached
            df_h1  = fetch_ohlcv_cached(client, symbol, "1h",  168, ttl_sec=300)
            df_m15 = fetch_ohlcv_cached(client, symbol, "15m", 96,  ttl_sec=30)

            ind_h1  = _compute_indicators(df_h1,  rsi_n=14, ema_fast=20, ema_slow=50,
                                          slope_short=24, slope_long=168) if df_h1 is not None else {}
            ind_m15 = _compute_indicators(df_m15, rsi_n=9,  ema_fast=9,  ema_slow=21,
                                          slope_short=12, slope_long=96)  if df_m15 is not None else {}

            if not ind_h1 or not ind_m15:
                return {"proba_win": 0.5, "features_ok": False,
                        "reason": "missing_indicators"}

            # Construit le vecteur features dans l'ordre exact du modèle
            row = []
            symbol_u = symbol.upper()
            side_l = str(side).lower()
            for c in self._cols:
                if c.startswith("1h_"):
                    row.append(float(ind_h1.get(c[3:], 0.0)))
                elif c.startswith("15m_"):
                    row.append(float(ind_m15.get(c[4:], 0.0)))
                elif c.startswith("coin_"):
                    row.append(1.0 if symbol_u == c[5:] else 0.0)
                elif c.startswith("side_"):
                    row.append(1.0 if side_l == c[5:] else 0.0)
                else:
                    row.append(0.0)

            X = np.array([row], dtype=float)
            proba = float(self._model.predict_proba(X)[0, 1])
            return {"proba_win": proba, "features_ok": True}
        except Exception as e:
            logger.warning("XGB Gate evaluate(%s, %s) failed: %r", symbol, side, e)
            return {"proba_win": 0.5, "features_ok": False, "reason": str(e)[:80]}


# Singleton global
_GATE: Optional[XGBGate] = None


def get_xgb_gate() -> XGBGate:
    global _GATE
    if _GATE is None:
        _GATE = XGBGate()
    return _GATE

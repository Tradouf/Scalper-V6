"""
Chargement orderflow (data/orderflow_hf.db) → barres OHLCV + CVD pour le backtest.

CVD (Cumulative Volume Delta) = somme cumulée du volume agresseur signé :
  side 'B' = taker BUY (+sz), side 'A' = taker SELL (−sz).
La divergence prix/CVD (prix fait un plus-haut que le CVD ne confirme pas) signale
un épuisement d'agresseurs → reversal probable. Microstructure, pas motif de prix.

⚠️ 12 jours de données seulement (un seul régime probable) → prudence sur la
généralisation, même si le gate OOS passe.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO / "data" / "orderflow_hf.db"


def load_cvd_bars(coin: str, bar_sec: int = 300, db_path: Path | None = None) -> pd.DataFrame:
    """Agrège le tape de trades en barres OHLCV + colonne `cvd` (CVD cumulé).

    Colonnes de sortie : ts, open, high, low, close, volume, cvd. Compatible avec
    le Backtester (OHLCV) ; `cvd` est préservée par `_add_indicators` et lue par
    `_signals_cvd_divergence`.
    """
    path = str(db_path or DEFAULT_DB)
    con = sqlite3.connect(path)
    try:
        df = pd.read_sql_query(
            "SELECT ts_ms, side, px, sz FROM trades WHERE coin = ? ORDER BY ts_ms",
            con, params=(coin.upper(),),
        )
    finally:
        con.close()
    if df.empty:
        return df

    df["bar"] = (df["ts_ms"] // (bar_sec * 1000)).astype("int64")
    df["signed"] = df["sz"].where(df["side"] == "B", -df["sz"])

    g = df.groupby("bar", sort=True)
    bars = pd.DataFrame({
        "ts": g["ts_ms"].first(),
        "open": g["px"].first(),
        "high": g["px"].max(),
        "low": g["px"].min(),
        "close": g["px"].last(),
        "volume": g["sz"].sum(),
        "delta": g["signed"].sum(),
    }).reset_index(drop=True)
    bars["cvd"] = bars["delta"].cumsum()
    return bars[["ts", "open", "high", "low", "close", "volume", "cvd"]]

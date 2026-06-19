"""Tests CVD : agrégation barres + signal de divergence."""
from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.backtester import Backtester


def test_cvd_divergence_signal_directions():
    bt = Backtester(None)
    n = 60
    # Prix qui monte en fin de série (nouveau plus-haut) mais CVD qui retombe
    # (sous son max) → divergence baissière attendue (signal -1) sur la dernière barre.
    close = np.concatenate([np.linspace(100, 110, n - 1), [111.0]])
    cvd = np.concatenate([np.linspace(0, 50, n - 10), np.linspace(50, 20, 10)])
    df = pd.DataFrame({
        "ts": np.arange(n), "open": close, "high": close + 0.1,
        "low": close - 0.1, "close": close, "volume": np.ones(n), "cvd": cvd,
    })
    sig = bt._signals_cvd_divergence(df, lookback=20)
    assert sig.iloc[-1] == -1   # prix HH non confirmé par le CVD → SHORT


def test_cvd_divergence_no_column_returns_zero():
    bt = Backtester(None)
    df = pd.DataFrame({"close": np.arange(60.0)})
    sig = bt._signals_cvd_divergence(df, lookback=20)
    assert (sig == 0).all()


def test_cvd_dispatch_via_run_on_df():
    bt = Backtester(None)
    n = 400
    rng = np.random.default_rng(0)
    close = 100 + np.cumsum(rng.normal(0, 0.3, n))
    cvd = np.cumsum(rng.normal(0, 1, n))
    df = pd.DataFrame({
        "ts": np.arange(n), "open": close, "high": close + 0.2,
        "low": close - 0.2, "close": close, "volume": np.ones(n), "cvd": cvd,
    })
    r = bt.run_on_df(df, "BTC", "cvd_divergence", tp_pct=0.006, sl_pct=0.003,
                     cvd_lookback=20, fee_pct=0.0005)
    assert r.nb_trades >= 0  # tourne sans erreur, métriques calculées

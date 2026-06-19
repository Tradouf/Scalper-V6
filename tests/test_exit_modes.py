"""Tests des modes de sortie alternatifs du backtester (_simulate exit_mode).

Vérifie : tp_sl INCHANGÉ (non-régression), reverse sort sur signal opposé,
time sort après N barres, trail coupe au retournement depuis l'extrême."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.backtester import Backtester


def _df(o, h, l, c):
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c,
                         "volume": np.ones(len(c))})


@pytest.fixture
def bt():
    return Backtester(None)


def test_tp_sl_unchanged_long_tp(bt):
    # Entrée long bar0 (signal +1), TP touché bar1 → pnl = tp_pct (net frais).
    df = _df([100, 100, 100], [100, 103, 103], [99, 100, 100], [100, 102, 102])
    sig = pd.Series([1, 0, 0])
    trades = bt._simulate(df, sig, tp_pct=0.02, sl_pct=0.01, fee_pct=0.0, exit_mode="tp_sl")
    assert len(trades) == 1 and trades[0]["result"] == "TP"
    assert abs(trades[0]["pnl"] - 0.02) < 1e-9


def test_reverse_exits_on_opposite_signal(bt):
    # Long bar0 ; signal −1 à bar2 → sortie REV au close de bar2.
    df = _df([100, 101, 102], [101, 102, 103], [99, 100, 101], [100, 101, 102])
    sig = pd.Series([1, 0, -1])
    trades = bt._simulate(df, sig, tp_pct=9.9, sl_pct=0.0, fee_pct=0.0, exit_mode="reverse")
    assert len(trades) == 1 and trades[0]["result"] == "REV"
    assert abs(trades[0]["exit"] - 102) < 1e-9


def test_time_exit_after_hold_bars(bt):
    df = _df([100]*6, [101]*6, [99]*6, [100, 100, 100, 100, 105, 105])
    sig = pd.Series([1, 0, 0, 0, 0, 0])
    trades = bt._simulate(df, sig, tp_pct=9.9, sl_pct=0.0, fee_pct=0.0,
                          exit_mode="time", hold_bars=4)
    assert len(trades) == 1 and trades[0]["result"] == "TIME"
    assert trades[0]["bar"] == 0  # entré bar0, sorti bar4 (i-bar>=4)


def test_trail_exits_on_pullback_from_peak(bt):
    # Long bar0@100, monte à 110 (bar1 high), recule : trail 5% → stop à 104.5.
    df = _df([100, 105, 103], [101, 110, 105], [99, 104, 100], [100, 108, 103])
    sig = pd.Series([1, 0, 0])
    trades = bt._simulate(df, sig, tp_pct=9.9, sl_pct=0.0, fee_pct=0.0,
                          exit_mode="trail", trail_pct=0.05)
    assert len(trades) == 1 and trades[0]["result"] == "TRAIL"
    # peak=110 → stop 104.5 ; touché bar2 (low 100 <= 104.5)
    assert abs(trades[0]["exit"] - 104.5) < 1e-6


def test_fees_deducted_in_alt_modes(bt):
    df = _df([100, 101, 102], [101, 102, 103], [99, 100, 101], [100, 101, 102])
    sig = pd.Series([1, 0, -1])
    free = bt._simulate(df, sig, 9.9, 0.0, fee_pct=0.0, exit_mode="reverse")
    feed = bt._simulate(df, sig, 9.9, 0.0, fee_pct=0.00045, exit_mode="reverse")
    assert feed[0]["pnl"] < free[0]["pnl"]

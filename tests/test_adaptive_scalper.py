"""Tests du scalper adaptatif « Le Danseur » (backtest/adaptive_scalper.py).

Vérifie : signaux MA×RSI corrects, absence de look-ahead, frais nets déduits,
et que le walk-forward glissant tourne + remplit le gate sans planter."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.adaptive_scalper import (
    ScalpParams, compute_rsi, compute_signals, run_params,
    rolling_walkforward, default_grid,
)
from backtest.backtester import Backtester


def _df(closes, highs=None, lows=None, opens=None):
    closes = np.asarray(closes, dtype=float)
    highs = closes if highs is None else np.asarray(highs, dtype=float)
    lows = closes if lows is None else np.asarray(lows, dtype=float)
    opens = closes if opens is None else np.asarray(opens, dtype=float)
    return pd.DataFrame({"open": opens, "high": highs, "low": lows,
                         "close": closes, "volume": np.ones_like(closes)})


def test_rsi_bounds_and_uptrend_high():
    # Hausse bruitée (quelques baisses → pertes non nulles, sinon RSI=NaN comme
    # dans la formule Wilder du repo). RSI borné [0,100] et élevé en tendance.
    rng = np.random.default_rng(1)
    closes = np.linspace(100, 200, 200) + rng.normal(0, 0.5, 200)
    rsi = compute_rsi(pd.Series(closes), 14).dropna()
    assert len(rsi) > 0
    assert (rsi >= 0).all() and (rsi <= 100).all()
    assert rsi.iloc[-1] > 70


def test_signals_values_in_set():
    rng = np.random.default_rng(0)
    closes = 100 + np.cumsum(rng.normal(0, 1, 500))
    df = _df(closes, highs=closes + 0.5, lows=closes - 0.5)
    sig = compute_signals(df, ScalpParams(5, 30, 14, 50.0, 0.008, 0.004))
    assert set(sig.unique()).issubset({-1, 0, 1})


def test_long_signal_on_golden_cross_with_rsi_confirm():
    # Descente puis remontée nette → la rapide repasse au-dessus de la lente avec
    # un RSI > 50 → au moins un signal LONG.
    closes = np.concatenate([np.linspace(100, 90, 40), np.linspace(90, 120, 40)])
    df = _df(closes, highs=closes + 0.5, lows=closes - 0.5)
    sig = compute_signals(df, ScalpParams(3, 10, 7, 50.0, 0.008, 0.004))
    assert (sig == 1).any()


def test_no_look_ahead_signal_depends_only_on_past():
    # Tronquer la série au point t ne doit pas changer le signal en t
    # (le signal n'utilise que [.., t], pas le futur).
    rng = np.random.default_rng(7)
    closes = 100 + np.cumsum(rng.normal(0, 1, 300))
    df = _df(closes, highs=closes + 0.4, lows=closes - 0.4)
    p = ScalpParams(5, 30, 14, 55.0, 0.008, 0.004)
    full = compute_signals(df, p)
    t = 250
    trunc = compute_signals(df.iloc[: t + 1], p)
    assert full.iloc[t] == trunc.iloc[t]


def test_fees_reduce_pnl_vs_zero_fee():
    rng = np.random.default_rng(3)
    closes = 100 + np.cumsum(rng.normal(0, 1.5, 600))
    df = _df(closes, highs=closes + 1.0, lows=closes - 1.0)
    p = ScalpParams(3, 30, 7, 50.0, 0.01, 0.005)
    r_free = run_params(df, "X", p, fee=0.0)
    r_fee = run_params(df, "X", p, fee=Backtester.DEFAULT_FEE_PCT)
    if r_free.nb_trades > 0:
        assert r_fee.total_pnl < r_free.total_pnl  # les frais mordent


def test_rolling_walkforward_runs_and_reports():
    rng = np.random.default_rng(11)
    closes = 100 + np.cumsum(rng.normal(0, 1.0, 2500))
    df = _df(closes, highs=closes + 0.6, lows=closes - 0.6,
             opens=np.concatenate([[100.0], closes[:-1]]))
    # Petite grille pour la vitesse.
    grid = [ScalpParams(f, s, 7, 50.0, 0.008, 0.004) for f in (3, 5) for s in (30, 50)]
    rep = rolling_walkforward(df, "X", grid=grid, train_bars=600, test_bars=150)
    assert rep.n_steps >= 2
    assert isinstance(rep.adaptive_oos_pnl, float)
    assert isinstance(rep.fixed_oos_pnl, float)
    # Le gate doit être tranché (bool) et accompagné de raisons.
    assert isinstance(rep.report.passed, bool)
    assert rep.report.gate_reasons


def test_default_grid_within_requested_bounds():
    grid = default_grid()
    assert len(grid) > 0
    for p in grid:
        assert 2 <= p.fast <= 20
        assert 30 <= p.slow <= 50
        assert 2 <= p.rsi_period <= 20


def test_random_walk_mostly_fails_gate():
    # Marche aléatoire pure → pas d'edge → le gate doit (presque toujours) REJETER.
    # On ne teste pas une seule graine (le gate a ~4% de faux positifs) : on vérifie
    # que sur plusieurs graines, l'écrasante majorité échoue.
    grid = [ScalpParams(f, s, 7, 50.0, 0.008, 0.004) for f in (3, 5) for s in (30, 50)]
    passes = 0
    for seed in range(6):
        rng = np.random.default_rng(seed)
        closes = 100 + np.cumsum(rng.normal(0, 1.0, 2500))
        df = _df(closes, highs=closes + 0.6, lows=closes - 0.6)
        rep = rolling_walkforward(df, "X", grid=grid, train_bars=600, test_bars=150)
        passes += int(rep.report.passed)
    assert passes <= 2  # tolérance faux positifs

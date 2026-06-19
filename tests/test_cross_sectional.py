"""Tests du backtest cross-sectionnel (panier momentum/reversal)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.cross_sectional import CrossSectionalWalkForward, cs_backtest


def _panel_trending(n=600, n_sym=6, seed=0):
    """Panel où chaque symbole a une dérive (drift) propre et stable → un momentum
    cross-sectionnel doit être profitable (les drifters montent, les autres non)."""
    rng = np.random.default_rng(seed)
    drifts = np.linspace(-0.002, 0.002, n_sym)  # drifts hétérogènes persistants
    cols = {}
    for s in range(n_sym):
        steps = drifts[s] + rng.normal(0, 0.001, n)
        cols[f"S{s}"] = 100.0 * np.exp(np.cumsum(steps))
    return pd.DataFrame(cols)


def test_cs_backtest_momentum_beats_reversal_on_trending_panel():
    closes = _panel_trending()
    mom = cs_backtest(closes, lookback=24, k=2, sign=1, rebal=24, fee_pct=0.0)
    rev = cs_backtest(closes, lookback=24, k=2, sign=-1, rebal=24, fee_pct=0.0)
    assert mom.nb_trades > 0
    # Sur des drifts persistants, le momentum capte la dispersion, le reversal la subit.
    assert mom.total_pnl > rev.total_pnl


def test_cs_backtest_fees_reduce_pnl():
    closes = _panel_trending()
    gross = cs_backtest(closes, lookback=24, k=2, sign=1, rebal=24, fee_pct=0.0)
    net = cs_backtest(closes, lookback=24, k=2, sign=1, rebal=24, fee_pct=0.001)
    assert net.nb_trades == gross.nb_trades
    assert net.total_pnl < gross.total_pnl


def test_cs_walkforward_runs_and_gates():
    closes = _panel_trending(n=1500)
    combos = [{"lookback": 24, "rebal": 24, "k": 2, "sign": 1},
              {"lookback": 48, "rebal": 48, "k": 2, "sign": -1}]
    rep = CrossSectionalWalkForward(fee_pct=0.0005).evaluate(closes, combos, n_folds=4)
    assert rep.n_folds == 4
    assert 1 <= len(rep.folds) <= 4
    assert isinstance(rep.passed, bool)
    assert "Walk-forward" in rep.summary()

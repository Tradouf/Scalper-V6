"""Tests du harnais walk-forward : frais, expand_grid, folds OOS, gate."""
from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.backtester import Backtester
from backtest.evaluator import WalkForwardEvaluator, expand_grid


def _synth(n=2000, seed=1):
    """OHLCV synthétique = marche aléatoire (aucun edge réel attendu)."""
    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(rng.normal(0, 1, n))
    close = np.maximum(close, 1.0)
    return pd.DataFrame({
        "ts": np.arange(n) * 3_600_000,
        "open": close, "high": close + 1.0, "low": close - 1.0,
        "close": close, "volume": np.ones(n),
    })


def test_expand_grid():
    g = expand_grid({"a": [1, 2], "b": [3]})
    assert len(g) == 2
    assert {"a": 1, "b": 3} in g and {"a": 2, "b": 3} in g
    assert expand_grid({}) == [{}]


def test_fees_reduce_pnl():
    bt = Backtester(None)
    df = _synth()
    gross = bt.run_on_df(df, "X", "momentum", tp_pct=0.03, sl_pct=0.015, fee_pct=0.0)
    net = bt.run_on_df(df, "X", "momentum", tp_pct=0.03, sl_pct=0.015, fee_pct=0.001)
    assert net.nb_trades == gross.nb_trades
    # Chaque trade perd 2×fee = 0,2% ; sur N trades l'écart total ≈ N×0,2 points.
    if gross.nb_trades > 0:
        expected_gap = gross.nb_trades * 2 * 0.001 * 100.0
        assert abs((gross.total_pnl - net.total_pnl) - expected_gap) < 1e-6


def test_walkforward_runs_and_reports():
    bt = Backtester(None)
    df = _synth(2400)
    ev = WalkForwardEvaluator(bt, fee_pct=0.0005)
    combos = [{"tp_pct": 0.02, "sl_pct": 0.01}, {"tp_pct": 0.04, "sl_pct": 0.02}]
    rep = ev.evaluate(df, "X", "momentum", combos, n_folds=4, train_frac=0.6)
    assert rep.n_folds == 4
    assert 1 <= len(rep.folds) <= 4
    # in_sample_best_pnl est le max plein-échantillon → ≥ la moyenne OOS par fold.
    assert rep.in_sample_best_pnl >= min(f.oos_pnl for f in rep.folds)
    assert isinstance(rep.passed, bool)
    assert rep.gate_reasons  # toujours renseigné (raisons de rejet ou de succès)
    # Le résumé doit être imprimable sans erreur.
    assert "Walk-forward" in rep.summary()


def test_gate_low_false_positive_on_random_walk():
    # Garde-fou central : une marche aléatoire NETTE DE FRAIS n'a pas d'edge. Le
    # gate ne doit la laisser passer que rarement (faux positifs ≤ ~12% sur 25
    # réalisations ; calibré à ~4% en pratique). Empêche le mirage type +18%.
    bt = Backtester(None)
    combos = [{"tp_pct": 0.02, "sl_pct": 0.01}, {"tp_pct": 0.04, "sl_pct": 0.02},
              {"tp_pct": 0.06, "sl_pct": 0.02}]
    passes = 0
    N = 25
    for seed in range(N):
        rep = WalkForwardEvaluator(bt, fee_pct=0.0005).evaluate(
            _synth(2400, seed), "X", "momentum", combos, n_folds=5)
        passes += int(rep.passed)
    assert passes / N <= 0.12, f"taux de faux positifs trop élevé : {passes}/{N}"

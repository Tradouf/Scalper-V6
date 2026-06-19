"""Tests du stat-arb cointégration (backtest/pairs_statarb.py).

Vérifie : ADF détecte la stationnarité, β récupère le ratio, z causal sans fuite,
le backtest tourne, et le gate REJETTE une marche aléatoire (pas de fausse paire)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.pairs_statarb import (
    adf_tstat, half_life, hedge_ratio, select_pairs, pair_backtest,
    PairsWalkForward, default_combos, _causal_z,
)


def test_adf_stationary_vs_randomwalk():
    rng = np.random.default_rng(0)
    # Bruit blanc stationnaire → ADF très négatif.
    white = rng.normal(0, 1, 1000)
    # Marche aléatoire → ADF proche de 0 (non stationnaire).
    walk = np.cumsum(rng.normal(0, 1, 1000))
    assert adf_tstat(white) < -5.0
    assert adf_tstat(walk) > -2.0


def test_hedge_ratio_recovers_beta():
    rng = np.random.default_rng(1)
    lb = np.cumsum(rng.normal(0, 0.01, 800)) + 5.0
    la = 0.3 + 1.7 * lb + rng.normal(0, 0.001, 800)  # β réel = 1.7
    _, beta = hedge_ratio(la, lb)
    assert abs(beta - 1.7) < 0.05


def test_half_life_mean_reverting_positive_finite():
    rng = np.random.default_rng(2)
    # AR(1) fortement mean-reverting → demi-vie courte et finie.
    s = np.zeros(2000)
    for i in range(1, 2000):
        s[i] = 0.5 * s[i - 1] + rng.normal(0, 1)
    hl = half_life(s)
    assert 0 < hl < 10


def test_causal_z_no_lookahead():
    rng = np.random.default_rng(4)
    spread = np.cumsum(rng.normal(0, 1, 300))
    z_full = _causal_z(spread, 50)
    z_trunc = _causal_z(spread[:201], 50)
    # le z à t=200 ne doit dépendre que du passé → identique si on tronque après t.
    assert np.isclose(z_full[200], z_trunc[200], equal_nan=True) or (
        np.isnan(z_full[200]) and np.isnan(z_trunc[200]))


def test_cointegrated_pair_is_selected():
    # Construit deux prix cointégrés : pb marche aléatoire, pa = pb + bruit stationnaire.
    rng = np.random.default_rng(5)
    lb = np.cumsum(rng.normal(0, 0.01, 1000)) + 5.0
    noise = np.zeros(1000)
    for i in range(1, 1000):
        # résidu mean-reverting AR(1) φ=0.9 → demi-vie ≈ 6,6 barres (dans [hl_min, hl_max])
        noise[i] = 0.9 * noise[i - 1] + rng.normal(0, 0.01)
    la = lb + noise
    df = pd.DataFrame({"A": np.exp(la), "B": np.exp(lb),
                       "C": np.exp(np.cumsum(rng.normal(0, 0.01, 1000)) + 3.0)})
    pairs = select_pairs(df, adf_thr=-2.9, max_pairs=5)
    found = {(p["a"], p["b"]) for p in pairs}
    assert ("A", "B") in found


def test_pair_backtest_runs():
    rng = np.random.default_rng(6)
    lb = np.cumsum(rng.normal(0, 0.01, 800)) + 5.0
    noise = np.zeros(800)
    for i in range(1, 800):
        noise[i] = 0.7 * noise[i - 1] + rng.normal(0, 0.02)
    a = pd.Series(np.exp(lb + noise))
    b = pd.Series(np.exp(lb))
    r = pair_backtest(a, b, beta=1.0, entry_z=2.0, exit_z=0.5, z_window=60)
    assert r.nb_trades >= 0
    assert isinstance(r.total_pnl, float)


def test_gate_rejects_random_walks():
    # Trois marches aléatoires indépendantes → aucune vraie cointégration →
    # le gate (barre relevée) doit REJETER sur la plupart des graines.
    passes = 0
    for seed in range(5):
        rng = np.random.default_rng(100 + seed)
        df = pd.DataFrame({
            s: np.exp(np.cumsum(rng.normal(0, 0.01, 1500)) + 5.0)
            for s in ("A", "B", "C", "D")
        })
        rep = PairsWalkForward().evaluate(df, default_combos(), n_folds=5)
        passes += int(rep.passed)
    assert passes <= 1

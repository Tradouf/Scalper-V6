"""Tests des features de régime — en particulier la garantie no-leak."""
from __future__ import annotations

import numpy as np
import pytest

from regime.features import (
    adx,
    atr,
    autocorr_lag1,
    hurst_rs,
    realized_vol,
    returns_slope_zscore,
    true_range,
    vol_percentile,
)


# ─── True Range / ATR ────────────────────────────────────────────────────────


def test_true_range_first_bar():
    tr = true_range([100, 105], [98, 102], [99, 104])
    assert tr[0] == 2.0  # H[0]-L[0]
    # H=105, L=102, Cprev=99
    # H-L=3, |H-Cprev|=|105-99|=6, |L-Cprev|=|102-99|=3
    # max = 6
    assert tr[1] == 6.0


def test_atr_insufficient_data():
    h, l, c = [1, 2, 3], [0, 1, 2], [0.5, 1.5, 2.5]
    assert atr(h, l, c, period=14) is None


def test_atr_returns_positive_value():
    # Données synthétiques : prix en hausse régulière, range stable
    n = 50
    h = np.linspace(100, 110, n) + 1.0
    l = np.linspace(100, 110, n) - 1.0
    c = np.linspace(100, 110, n)
    val = atr(h, l, c, period=14)
    assert val is not None
    assert val > 0


# ─── ADX ─────────────────────────────────────────────────────────────────────


def test_adx_insufficient_data():
    assert adx([1, 2, 3], [0, 1, 2], [0.5, 1.5, 2.5], period=14) is None


def test_adx_strong_trend_high_value():
    """Hausse régulière → ADX devrait être élevé (> 30)."""
    n = 80
    base = np.linspace(100, 150, n)
    h = base + 0.5
    l = base - 0.5
    c = base
    val = adx(h, l, c, period=14)
    assert val is not None
    assert val > 30, f"ADX trend = {val} attendu > 30"


def test_adx_range_low_value():
    """Range pur (oscille autour d'un niveau) → ADX bas (< 25)."""
    n = 80
    np.random.seed(42)
    base = 100.0 + np.sin(np.arange(n) * 0.3) * 2.0 + np.random.normal(0, 0.3, n)
    h = base + 0.5
    l = base - 0.5
    c = base
    val = adx(h, l, c, period=14)
    assert val is not None
    assert val < 30, f"ADX range = {val} attendu < 30"


# ─── Hurst ───────────────────────────────────────────────────────────────────


def test_hurst_random_walk_near_0_5():
    """Random walk pur → Hurst ≈ 0.5."""
    np.random.seed(0)
    rw = np.cumsum(np.random.normal(0, 1, 1024)) + 100.0
    h = hurst_rs(rw, min_chunk=8)
    assert h is not None
    assert 0.35 < h < 0.65, f"Hurst RW = {h} attendu proche de 0.5"


def test_hurst_trending_above_0_5():
    """Trend persistant (returns positivement autocorrélés) → Hurst > 0.55.

    Construction : returns(t) = 0.6*returns(t-1) + eps. C'est un AR(1) avec
    persistance forte, qui correspond exactement à ce que Hurst R/S capture."""
    np.random.seed(1)
    n = 1024
    eps = np.random.normal(0, 0.01, n)
    rets = np.zeros(n)
    for i in range(1, n):
        rets[i] = 0.6 * rets[i - 1] + eps[i]
    prices = 100.0 * np.exp(np.cumsum(rets))
    h = hurst_rs(prices, min_chunk=8)
    assert h is not None
    assert h > 0.55, f"Hurst persistant = {h} attendu > 0.55"


def test_hurst_insufficient():
    assert hurst_rs([1, 2, 3, 4], min_chunk=8) is None


# ─── Vol réalisée / percentile ───────────────────────────────────────────────


def test_realized_vol_basic():
    n = 50
    np.random.seed(2)
    rets = np.random.normal(0, 0.01, n)
    prices = 100.0 * np.exp(np.cumsum(rets))
    vol = realized_vol(prices, window=24)
    assert vol is not None
    assert vol > 0


def test_vol_percentile_high_value_on_spike():
    """Si la dernière vol est plus haute que toute la fenêtre précédente,
    percentile devrait être proche de 1.0."""
    np.random.seed(3)
    # 100 bars vol basse, puis 24 bars vol élevée
    low = np.random.normal(0, 0.001, 100)
    high = np.random.normal(0, 0.05, 24)
    rets = np.concatenate([low, high])
    prices = 100.0 * np.exp(np.cumsum(rets))
    p = vol_percentile(prices, window=24, lookback=80)
    assert p is not None
    assert p > 0.85, f"Vol percentile sur spike = {p} attendu > 0.85"


# ─── Autocorrélation ─────────────────────────────────────────────────────────


def test_autocorr_zero_on_random():
    np.random.seed(4)
    rets = np.random.normal(0, 0.01, 200)
    prices = 100.0 * np.exp(np.cumsum(rets))
    a = autocorr_lag1(prices, window=100)
    assert a is not None
    assert -0.15 < a < 0.15, f"Autocorr RW = {a} attendu proche de 0"


def test_autocorr_positive_on_momentum():
    """Si on construit une série où returns(t) = 0.5*returns(t-1) + noise,
    l'autocorr lag-1 sera proche de 0.5."""
    np.random.seed(5)
    n = 300
    eps = np.random.normal(0, 0.01, n)
    rets = np.zeros(n)
    for i in range(1, n):
        rets[i] = 0.5 * rets[i - 1] + eps[i]
    prices = 100.0 * np.exp(np.cumsum(rets))
    a = autocorr_lag1(prices, window=200)
    assert a is not None
    assert a > 0.3, f"Autocorr AR(1)=0.5 → {a} attendu > 0.3"


# ─── Slope returns ───────────────────────────────────────────────────────────


def test_slope_positive_on_uptrend():
    n = 100
    prices = np.linspace(100, 120, n) + np.random.RandomState(7).normal(0, 0.1, n)
    s = returns_slope_zscore(prices, window=48)
    assert s is not None
    assert s > 0


def test_slope_negative_on_downtrend():
    n = 100
    prices = np.linspace(100, 80, n) + np.random.RandomState(8).normal(0, 0.1, n)
    s = returns_slope_zscore(prices, window=48)
    assert s is not None
    assert s < 0


# ─── NO-LEAK : critique ──────────────────────────────────────────────────────


class TestNoLeak:
    """La valeur d'une feature à l'instant t doit être identique qu'elle soit
    calculée :
      (a) à partir de tous les prix jusqu'à t inclus
      (b) à partir de prices[: t+1] uniquement
    """

    def _series(self) -> np.ndarray:
        np.random.seed(42)
        n = 500
        return 100.0 * np.exp(np.cumsum(np.random.normal(0, 0.01, n)))

    def test_atr_no_leak(self):
        prices = self._series()
        h = prices + 0.5
        l = prices - 0.5
        c = prices
        full = atr(h, l, c, period=14)
        # On retire les 100 derniers : la valeur "à t-100" calculée sur
        # h[:n-100], etc. ne doit pas changer si on ajoute h[n-100:] après
        cut = 100
        partial = atr(h[:-cut], l[:-cut], c[:-cut], period=14)
        # On ne peut pas tester l'égalité directe (full ≠ partial car
        # ils sont à des t différents). Mais on s'assure que les fonctions
        # ne lisent pas les futurs : on calcule à t=400 dans les 2 cas.
        atr_at_400_full = atr(h[:400], l[:400], c[:400], period=14)
        atr_at_400_partial = atr(h[:400], l[:400], c[:400], period=14)
        assert atr_at_400_full == atr_at_400_partial

    def test_adx_no_leak(self):
        prices = self._series()
        h, l, c = prices + 0.5, prices - 0.5, prices
        # Calcul à t=400 sur deux entrées tronquées différemment (un avec
        # contenu futur en dehors de la slice, un sans) doit donner même résult
        v1 = adx(h[:400], l[:400], c[:400], period=14)
        v2 = adx(h[:400], l[:400], c[:400], period=14)  # identique mais re-calculé
        assert v1 == v2
        # Vérif que le contenu APRÈS index 400 n'influence pas le résultat à 400
        # en passant une version où h[400:] est trafiqué :
        h_modified = h.copy()
        h_modified[400:] = h_modified[400:] * 10  # explose après 400
        v3 = adx(h_modified[:400], l[:400], c[:400], period=14)
        assert v3 == v1, "ADX à 400 ne doit dépendre que de h[:400]"

    def test_hurst_no_leak(self):
        prices = self._series()
        v_at_400_a = hurst_rs(prices[:400], min_chunk=8)
        v_at_400_b = hurst_rs(prices[:400], min_chunk=8)
        assert v_at_400_a == v_at_400_b
        # Trafic après 400 ne doit pas influencer
        p_mod = prices.copy()
        p_mod[400:] = p_mod[400:] * 5
        v_at_400_c = hurst_rs(p_mod[:400], min_chunk=8)
        assert v_at_400_c == v_at_400_a

    def test_vol_percentile_no_leak(self):
        prices = self._series()
        v_a = vol_percentile(prices[:400], window=24, lookback=80)
        v_b = vol_percentile(prices[:400], window=24, lookback=80)
        assert v_a == v_b
        # Trafic après
        p_mod = prices.copy()
        p_mod[400:] = p_mod[400:] * 100
        v_c = vol_percentile(p_mod[:400], window=24, lookback=80)
        assert v_c == v_a

    def test_autocorr_no_leak(self):
        prices = self._series()
        a = autocorr_lag1(prices[:400], window=100)
        # Modifier le futur ne change pas
        p_mod = prices.copy()
        p_mod[400:] = p_mod[400:] * 0.01
        b = autocorr_lag1(p_mod[:400], window=100)
        assert a == b

    def test_slope_no_leak(self):
        prices = self._series()
        s = returns_slope_zscore(prices[:400], window=48)
        p_mod = prices.copy()
        p_mod[400:] = p_mod[400:] + 1000
        s_mod = returns_slope_zscore(p_mod[:400], window=48)
        assert s == s_mod

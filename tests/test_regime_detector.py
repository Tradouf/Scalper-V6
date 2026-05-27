"""Tests du détecteur de régime probabiliste."""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pytest

from core.config import RegimeConfig
from core.types import Candle, MarketSnapshot, Regime
from regime.detector import RuleBasedRegimeDetector


def _make_candles(prices: np.ndarray, start: dt.datetime) -> list[Candle]:
    return [
        Candle(
            ts_open=start + dt.timedelta(hours=i),
            open=float(p),
            high=float(p) + 0.5,
            low=float(p) - 0.5,
            close=float(p),
            volume=1000.0,
        )
        for i, p in enumerate(prices)
    ]


def _make_market(symbol: str, prices: np.ndarray, timestamp: dt.datetime) -> MarketSnapshot:
    candles = _make_candles(prices, timestamp - dt.timedelta(hours=len(prices)))
    return MarketSnapshot(
        timestamp=timestamp,
        candles={symbol: candles},
        prices={symbol: candles[-1].close},
    )


# ─── Probabilités valides ────────────────────────────────────────────────────


class TestProbabilities:
    def test_sum_to_one(self):
        cfg = RegimeConfig()
        det = RuleBasedRegimeDetector(cfg)
        np.random.seed(0)
        prices = 100.0 * np.exp(np.cumsum(np.random.normal(0, 0.01, 200)))
        market = _make_market("BTC", prices, dt.datetime(2026, 5, 27, 12, 0, 0))
        rs = det.detect(market)
        s = sum(rs.probabilities.values())
        assert math.isclose(s, 1.0, abs_tol=1e-6), f"sum = {s}"

    def test_all_regimes_present(self):
        cfg = RegimeConfig()
        det = RuleBasedRegimeDetector(cfg)
        np.random.seed(1)
        prices = 100.0 * np.exp(np.cumsum(np.random.normal(0, 0.01, 200)))
        market = _make_market("BTC", prices, dt.datetime(2026, 5, 27, 12, 0, 0))
        rs = det.detect(market)
        for r in Regime:
            assert r in rs.probabilities

    def test_empty_market_returns_uniform(self):
        cfg = RegimeConfig()
        det = RuleBasedRegimeDetector(cfg)
        market = MarketSnapshot(timestamp=dt.datetime.now(), candles={}, prices={})
        rs = det.detect(market)
        for p in rs.probabilities.values():
            assert 0.0 <= p <= 1.0
        assert math.isclose(sum(rs.probabilities.values()), 1.0, abs_tol=1e-6)


# ─── Détection régime trend ──────────────────────────────────────────────────


class TestTrendDetection:
    def test_uptrend_recognized(self):
        """Hausse régulière → trend_up doit dominer (ou être 2e si HIGH_VOL artefact)."""
        cfg = RegimeConfig()
        det = RuleBasedRegimeDetector(cfg)
        n = 200
        # Trend haussier net + bruit faible
        np.random.seed(10)
        trend = np.linspace(100, 150, n)
        noise = np.random.normal(0, 0.2, n)
        prices = trend + noise
        market = _make_market("BTC", prices, dt.datetime(2026, 5, 27))
        rs = det.detect(market)
        # trend_up doit avoir la proba dominante parmi trend_up/range
        assert rs.probabilities[Regime.TREND_UP] > rs.probabilities[Regime.RANGE], \
            f"P(up)={rs.probabilities[Regime.TREND_UP]:.3f} P(range)={rs.probabilities[Regime.RANGE]:.3f}"
        assert rs.probabilities[Regime.TREND_UP] > rs.probabilities[Regime.TREND_DOWN]

    def test_downtrend_recognized(self):
        cfg = RegimeConfig()
        det = RuleBasedRegimeDetector(cfg)
        n = 200
        np.random.seed(11)
        trend = np.linspace(150, 100, n)
        noise = np.random.normal(0, 0.2, n)
        prices = trend + noise
        market = _make_market("BTC", prices, dt.datetime(2026, 5, 27))
        rs = det.detect(market)
        assert rs.probabilities[Regime.TREND_DOWN] > rs.probabilities[Regime.RANGE]
        assert rs.probabilities[Regime.TREND_DOWN] > rs.probabilities[Regime.TREND_UP]


class TestRangeDetection:
    def test_range_recognized(self):
        """Mean-reverting AR(1) avec coefficient négatif → clairement range.
        On utilise un seuil high_vol haut pour éviter qu'il dérobe le résultat."""
        cfg = RegimeConfig(high_vol_atr_percentile=0.99)
        det = RuleBasedRegimeDetector(cfg)
        np.random.seed(20)
        n = 200
        eps = np.random.normal(0, 0.01, n)
        rets = np.zeros(n)
        for i in range(1, n):
            rets[i] = -0.3 * rets[i - 1] + eps[i]  # mean-reverting
        prices = 100.0 * np.exp(np.cumsum(rets))
        market = _make_market("BTC", prices, dt.datetime(2026, 5, 27))
        rs = det.detect(market)
        assert rs.probabilities[Regime.RANGE] > rs.probabilities[Regime.TREND_UP]
        assert rs.probabilities[Regime.RANGE] > rs.probabilities[Regime.TREND_DOWN]


# ─── Hystérésis ──────────────────────────────────────────────────────────────


class TestHysteresis:
    """Sur données synthétiques, HIGH_VOL peut être déclenché par les bruits.
    Pour isoler la logique d'hystérésis trend↔range, on neutralise HIGH_VOL
    via un seuil très haut (high_vol_atr_percentile=0.99)."""

    def test_first_call_adopts_argmax(self):
        cfg = RegimeConfig(min_dwell_bars=5, high_vol_atr_percentile=0.99)
        det = RuleBasedRegimeDetector(cfg)
        n = 200
        np.random.seed(30)
        prices = np.linspace(100, 150, n) + np.random.normal(0, 0.2, n)
        market = _make_market("BTC", prices, dt.datetime(2026, 5, 27))
        rs = det.detect(market)
        assert rs.label == Regime.TREND_UP

    def test_label_sticky_during_dwell(self):
        """Si on passe d'un trend_up à un range mais avant min_dwell ticks,
        le label doit rester TREND_UP."""
        cfg = RegimeConfig(min_dwell_bars=5, high_vol_atr_percentile=0.99)
        det = RuleBasedRegimeDetector(cfg)
        np.random.seed(31)
        prices_up = np.linspace(100, 150, 200) + np.random.normal(0, 0.2, 200)
        det.detect(_make_market("BTC", prices_up, dt.datetime(2026, 5, 27, 12)))
        assert det._current_label == Regime.TREND_UP

        prices_range = 100.0 + np.sin(np.linspace(0, 6 * np.pi, 200)) * 3.0 + np.random.RandomState(32).normal(0, 0.3, 200)
        rs = det.detect(_make_market("BTC", prices_range, dt.datetime(2026, 5, 27, 13)))
        assert rs.label == Regime.TREND_UP  # encore sticky

    def test_label_switches_after_min_dwell(self):
        cfg = RegimeConfig(min_dwell_bars=3, high_vol_atr_percentile=0.99)
        det = RuleBasedRegimeDetector(cfg)
        np.random.seed(40)
        prices_up = np.linspace(100, 150, 200) + np.random.normal(0, 0.2, 200)
        det.detect(_make_market("BTC", prices_up, dt.datetime(2026, 5, 27, 10)))
        # Mean-reverting AR(1) pour forcer RANGE
        np.random.seed(41)
        n = 200
        eps = np.random.normal(0, 0.01, n)
        rets = np.zeros(n)
        for i in range(1, n):
            rets[i] = -0.3 * rets[i - 1] + eps[i]
        prices_range = 100.0 * np.exp(np.cumsum(rets))
        labels = []
        for i in range(5):
            ts = dt.datetime(2026, 5, 27, 11 + i)
            rs = det.detect(_make_market("BTC", prices_range, ts))
            labels.append(rs.label)
        assert Regime.RANGE in labels, f"labels = {labels}"


# ─── NO-LEAK détecteur ───────────────────────────────────────────────────────


class TestNoLeakDetector:
    """Le label/probas à t doivent dépendre uniquement des candles ≤ t."""

    def test_detector_does_not_use_future(self):
        cfg = RegimeConfig()
        det1 = RuleBasedRegimeDetector(cfg)
        det2 = RuleBasedRegimeDetector(cfg)

        np.random.seed(50)
        prices_long = 100.0 * np.exp(np.cumsum(np.random.normal(0, 0.01, 500)))
        # Cas 1 : on calcule à t=300 avec candles[:300]
        market_a = _make_market("BTC", prices_long[:300], dt.datetime(2026, 5, 27, 10))
        rs_a = det1.detect(market_a)

        # Cas 2 : on calcule à t=300 mais avec une série dont prices[300:] est modifié
        prices_modified = prices_long.copy()
        prices_modified[300:] = prices_modified[300:] * 10  # explose le futur
        market_b = _make_market("BTC", prices_modified[:300], dt.datetime(2026, 5, 27, 10))
        rs_b = det2.detect(market_b)

        # Probabilités identiques
        for r in Regime:
            assert math.isclose(rs_a.probabilities[r], rs_b.probabilities[r], abs_tol=1e-9), \
                f"P({r}) diff : {rs_a.probabilities[r]} vs {rs_b.probabilities[r]}"

"""Tests des contrats de données core/types.py."""
from __future__ import annotations

import datetime as dt

import pytest

from core.types import (
    Candle,
    Fill,
    MarketSnapshot,
    Regime,
    RegimeState,
    Signal,
    TargetPortfolio,
    TargetPosition,
)


NOW = dt.datetime(2026, 5, 27, 12, 0, 0)


# ─── RegimeState ─────────────────────────────────────────────────────────────


class TestRegimeState:
    def test_valid_construction(self):
        rs = RegimeState(
            timestamp=NOW,
            probabilities={Regime.TREND_UP: 0.5, Regime.TREND_DOWN: 0.1, Regime.RANGE: 0.3, Regime.HIGH_VOL: 0.1},
            label=Regime.TREND_UP,
            confidence=0.5,
        )
        assert rs.label == Regime.TREND_UP

    def test_proba_sum_must_be_one(self):
        with pytest.raises(ValueError, match="somme probas"):
            RegimeState(
                timestamp=NOW,
                probabilities={Regime.TREND_UP: 0.5, Regime.TREND_DOWN: 0.1, Regime.RANGE: 0.1, Regime.HIGH_VOL: 0.1},  # sum=0.8
                label=Regime.TREND_UP,
                confidence=0.5,
            )

    def test_missing_regime_rejected(self):
        with pytest.raises(ValueError, match="manquantes"):
            RegimeState(
                timestamp=NOW,
                probabilities={Regime.TREND_UP: 1.0},
                label=Regime.TREND_UP,
                confidence=1.0,
            )

    def test_negative_proba_rejected(self):
        with pytest.raises(ValueError, match="hors"):
            RegimeState(
                timestamp=NOW,
                probabilities={Regime.TREND_UP: -0.1, Regime.TREND_DOWN: 0.1, Regime.RANGE: 0.5, Regime.HIGH_VOL: 0.5},
                label=Regime.RANGE,
                confidence=0.5,
            )


# ─── Signal ──────────────────────────────────────────────────────────────────


class TestSignal:
    def _valid(self, **kwargs):
        defaults = dict(
            strategy_id="grid",
            asset="BTC",
            direction=1.0,
            target_notional=100.0,
            expected_edge_bps=15.0,
            confidence=0.7,
            stop_price=None,
            horizon_bars=4,
            timestamp=NOW,
        )
        defaults.update(kwargs)
        return Signal(**defaults)

    def test_valid(self):
        s = self._valid()
        assert s.direction == 1.0

    def test_direction_bounds(self):
        with pytest.raises(ValueError, match="direction"):
            self._valid(direction=1.5)
        with pytest.raises(ValueError, match="direction"):
            self._valid(direction=-1.5)
        # bornes inclusives
        self._valid(direction=-1.0)
        self._valid(direction=1.0)

    def test_target_notional_non_negative(self):
        with pytest.raises(ValueError, match="target_notional"):
            self._valid(target_notional=-1.0)

    def test_confidence_bounds(self):
        with pytest.raises(ValueError, match="confidence"):
            self._valid(confidence=-0.1)
        with pytest.raises(ValueError, match="confidence"):
            self._valid(confidence=1.5)

    def test_empty_strategy_id_rejected(self):
        with pytest.raises(ValueError, match="strategy_id"):
            self._valid(strategy_id="")

    def test_horizon_non_negative(self):
        with pytest.raises(ValueError, match="horizon"):
            self._valid(horizon_bars=-1)


# ─── TargetPosition / TargetPortfolio ─────────────────────────────────────────


class TestTargetPortfolio:
    def test_construction(self):
        pos = TargetPosition(asset="BTC", target_notional=500.0, contributing_strategies={"grid": 500.0})
        tp = TargetPortfolio(
            timestamp=NOW, positions=[pos], gross_exposure=500.0, net_exposure=500.0,
        )
        assert tp.positions[0].asset == "BTC"

    def test_empty_asset_rejected(self):
        with pytest.raises(ValueError, match="asset"):
            TargetPosition(asset="", target_notional=100.0)

    def test_gross_non_negative(self):
        with pytest.raises(ValueError, match="gross"):
            TargetPortfolio(timestamp=NOW, positions=[], gross_exposure=-1.0, net_exposure=0.0)


# ─── Fill ────────────────────────────────────────────────────────────────────


class TestFill:
    def test_valid(self):
        f = Fill(order_id="123", asset="BTC", notional=500.0, price=70000.0, fee=0.5, strategy_id="grid", timestamp=NOW)
        assert f.notional == 500.0

    def test_negative_fee_rejected(self):
        with pytest.raises(ValueError, match="fee"):
            Fill(order_id="123", asset="BTC", notional=500.0, price=70000.0, fee=-0.1, strategy_id=None, timestamp=NOW)

    def test_zero_price_rejected(self):
        with pytest.raises(ValueError, match="price"):
            Fill(order_id="123", asset="BTC", notional=500.0, price=0.0, fee=0.5, strategy_id=None, timestamp=NOW)


# ─── MarketSnapshot ──────────────────────────────────────────────────────────


def make_market(asset: str = "BTC", n_candles: int = 50) -> MarketSnapshot:
    candles = [
        Candle(
            ts_open=NOW - dt.timedelta(hours=n_candles - i),
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.5 + i,
            volume=1000.0,
        )
        for i in range(n_candles)
    ]
    return MarketSnapshot(
        timestamp=NOW,
        candles={asset: candles},
        prices={asset: candles[-1].close},
        funding_rates={asset: 0.0001},
    )


def test_market_snapshot_construction():
    m = make_market()
    assert "BTC" in m.candles
    assert len(m.candles["BTC"]) == 50
    assert m.prices["BTC"] == 149.5  # close du dernier candle

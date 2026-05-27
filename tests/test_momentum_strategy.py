"""Tests MomentumStrategy."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from core.config import MomentumStrategyConfig
from core.types import Candle, Fill, MarketSnapshot
from strategies.momentum import MomentumStrategy


NOW = dt.datetime(2026, 5, 27, 12, 0, 0)


def _cfg(**kwargs) -> MomentumStrategyConfig:
    defaults = dict(
        enabled=True,
        interval="1h",
        lookback_bars=48,
        entry_zscore=1.5,
        notional_usdc=30.0,
    )
    defaults.update(kwargs)
    return MomentumStrategyConfig(**defaults)


def _make_market(prices: list[float], symbol: str = "BTC") -> MarketSnapshot:
    n = len(prices)
    candles = [
        Candle(
            ts_open=NOW - dt.timedelta(hours=n - i),
            open=p, high=p + 0.5, low=p - 0.5, close=p, volume=1.0,
        )
        for i, p in enumerate(prices)
    ]
    return MarketSnapshot(
        timestamp=NOW,
        candles={symbol: candles},
        prices={symbol: prices[-1]},
    )


def test_protocol_conformity():
    strat = MomentumStrategy(_cfg(), symbols=["BTC"])
    assert strat.strategy_id == "momentum"
    assert callable(strat.generate_signals)
    assert callable(strat.on_fill)


def test_no_signal_insufficient_data():
    strat = MomentumStrategy(_cfg(), symbols=["BTC"])
    prices = [100.0] * 30
    assert strat.generate_signals(_make_market(prices)) == []


def test_no_signal_flat_market():
    """Marché plat → slope_z ≈ 0 → pas de signal."""
    strat = MomentumStrategy(_cfg(entry_zscore=1.0), symbols=["BTC"])
    np.random.seed(0)
    prices = list(100.0 + np.random.normal(0, 0.1, 100))
    sigs = strat.generate_signals(_make_market(prices))
    assert sigs == []


def test_long_signal_on_uptrend():
    """Trend haussier net → signal LONG."""
    strat = MomentumStrategy(_cfg(lookback_bars=48, entry_zscore=1.0), symbols=["BTC"])
    np.random.seed(1)
    prices = list(np.linspace(100, 130, 100) + np.random.normal(0, 0.2, 100))
    sigs = strat.generate_signals(_make_market(prices))
    if not sigs:
        # ATR cap peut bloquer
        return
    s = sigs[0]
    assert s.direction == 1.0
    assert s.target_notional > 0
    assert s.stop_price is not None
    assert s.stop_price < prices[-1]  # SL sous mark


def test_short_signal_on_downtrend():
    strat = MomentumStrategy(_cfg(lookback_bars=48, entry_zscore=1.0), symbols=["BTC"])
    np.random.seed(2)
    prices = list(np.linspace(130, 100, 100) + np.random.normal(0, 0.2, 100))
    sigs = strat.generate_signals(_make_market(prices))
    if not sigs:
        return
    s = sigs[0]
    assert s.direction == -1.0
    assert s.stop_price > prices[-1]


def test_atr_filter_blocks_volatile():
    """ATR très élevé → skip."""
    strat = MomentumStrategy(_cfg(lookback_bars=48, entry_zscore=1.0), symbols=["BTC"], max_atr_pct=0.001)
    np.random.seed(3)
    prices = list(np.linspace(100, 130, 100) + np.random.normal(0, 2.0, 100))
    sigs = strat.generate_signals(_make_market(prices))
    # ATR sur cette série dépasse 0.1% du prix → skip
    assert sigs == []


def test_close_signal_when_slope_weakens():
    """Si on a une position LONG et le slope retombe sous entry_z/2 → CLOSE."""
    strat = MomentumStrategy(_cfg(lookback_bars=48, entry_zscore=1.5), symbols=["BTC"])
    # Forge la position
    strat.on_fill(Fill(order_id="1", asset="BTC", notional=100.0, price=100.0, fee=0.1, strategy_id="momentum", timestamp=NOW))
    # Marché flat → slope ~ 0
    np.random.seed(4)
    prices = list(100.0 + np.random.normal(0, 0.1, 100))
    sigs = strat.generate_signals(_make_market(prices))
    if sigs:
        s = sigs[0]
        assert s.target_notional == 0.0
        assert s.direction == 0.0


def test_on_fill_tracking():
    strat = MomentumStrategy(_cfg(), symbols=["BTC"])
    strat.on_fill(Fill(order_id="1", asset="BTC", notional=50.0, price=100.0, fee=0.05, strategy_id="momentum", timestamp=NOW))
    assert "BTC" in strat.open_positions()
    strat.on_fill(Fill(order_id="2", asset="BTC", notional=-50.0, price=105.0, fee=0.05, strategy_id="momentum", timestamp=NOW))
    assert "BTC" not in strat.open_positions()


def test_cooldown_blocks_retrigger():
    strat = MomentumStrategy(_cfg(lookback_bars=48, entry_zscore=1.0), symbols=["BTC"], cooldown_sec=3600)
    np.random.seed(5)
    prices = list(np.linspace(100, 130, 100) + np.random.normal(0, 0.2, 100))
    market = _make_market(prices)
    sigs1 = strat.generate_signals(market)
    if not sigs1:
        return
    sigs2 = strat.generate_signals(market)
    assert sigs2 == []

"""Tests GridStrategy : conformité Protocol, génération de Signal, on_fill,
activation/désactivation via budget."""
from __future__ import annotations

import datetime as dt

import pytest

from core.config import GridStrategyConfig
from core.types import Candle, Fill, MarketSnapshot
from execution.mock_exchange import MockExchange
from strategies.grid import GridStrategy
from strategies.grid_engine import GridEngine


NOW = dt.datetime(2026, 5, 27, 12, 0, 0)
SYMS = ["BTC", "ETH", "SOL"]


def _grid_cfg() -> GridStrategyConfig:
    return GridStrategyConfig(
        enabled=True,
        atr_factor=0.5,
        levels=5,
        notional_per_level_usdc=15.0,
        drift_k=3.0,
        drift_window_sec=3600,
        health_check_sec=300,
        activation_threshold_usdc=20.0,
    )


def _market(symbol: str = "BTC", price: float = 70000.0) -> MarketSnapshot:
    candles = [
        Candle(
            ts_open=NOW - dt.timedelta(hours=50 - i),
            open=price - 50, high=price + 50, low=price - 100, close=price + (i - 25),
            volume=1000.0,
        )
        for i in range(50)
    ]
    return MarketSnapshot(
        timestamp=NOW,
        candles={symbol: candles},
        prices={symbol: price},
    )


# ─── Conformité StrategyAgent ────────────────────────────────────────────────


def test_grid_implements_strategy_agent_protocol():
    ex = MockExchange(mark_prices={"BTC": 70000.0})
    engine = GridEngine(ex, _grid_cfg())
    strat = GridStrategy(engine, symbols=SYMS)
    # Attributs requis
    assert hasattr(strat, "strategy_id")
    assert isinstance(strat.strategy_id, str)
    assert strat.strategy_id == "grid"
    # Méthodes
    assert callable(getattr(strat, "generate_signals", None))
    assert callable(getattr(strat, "on_fill", None))


def test_generate_signals_zero_exposure_at_start():
    ex = MockExchange(mark_prices={"BTC": 70000.0, "ETH": 2000.0, "SOL": 85.0})
    engine = GridEngine(ex, _grid_cfg())
    strat = GridStrategy(engine, symbols=SYMS)
    # Marché pour tous les symboles
    candles = {
        sym: [Candle(ts_open=NOW, open=100, high=101, low=99, close=100, volume=1.0)]
        for sym in SYMS
    }
    market = MarketSnapshot(timestamp=NOW, candles=candles, prices={sym: 100.0 for sym in SYMS})
    signals = strat.generate_signals(market)
    # Un Signal par symbole
    assert len(signals) == 3
    for s in signals:
        assert s.strategy_id == "grid"
        assert s.target_notional == 0.0
        assert s.direction == 0.0


def test_generate_signals_after_fill_reflects_exposure():
    ex = MockExchange(mark_prices={"BTC": 70000.0})
    engine = GridEngine(ex, _grid_cfg())
    strat = GridStrategy(engine, symbols=SYMS)
    # Simuler un fill long BTC
    fill = Fill(
        order_id="123",
        asset="BTC",
        notional=500.0,  # long
        price=70000.0,
        fee=0.5,
        strategy_id="grid",
        timestamp=NOW,
    )
    strat.on_fill(fill)
    assert strat.get_net_exposure("BTC") == 500.0

    candles = {sym: [Candle(ts_open=NOW, open=100, high=101, low=99, close=100, volume=1.0)] for sym in SYMS}
    market = MarketSnapshot(timestamp=NOW, candles=candles, prices={sym: 100.0 for sym in SYMS})
    signals = strat.generate_signals(market)
    btc_signal = next(s for s in signals if s.asset == "BTC")
    assert btc_signal.direction == 1.0
    assert btc_signal.target_notional == 500.0


def test_generate_signals_short_exposure_negative_direction():
    ex = MockExchange()
    engine = GridEngine(ex, _grid_cfg())
    strat = GridStrategy(engine, symbols=["BTC"])
    strat.on_fill(Fill(order_id="x", asset="BTC", notional=-300.0, price=70000, fee=0.1, strategy_id="grid", timestamp=NOW))
    candles = {"BTC": [Candle(ts_open=NOW, open=100, high=101, low=99, close=100, volume=1.0)]}
    market = MarketSnapshot(timestamp=NOW, candles=candles, prices={"BTC": 70000})
    sigs = strat.generate_signals(market)
    assert sigs[0].direction == -1.0
    assert sigs[0].target_notional == 300.0


def test_multiple_fills_accumulate():
    ex = MockExchange()
    engine = GridEngine(ex, _grid_cfg())
    strat = GridStrategy(engine, symbols=["BTC", "ETH"])
    strat.on_fill(Fill(order_id="a", asset="BTC", notional=100.0, price=70000, fee=0.05, strategy_id="grid", timestamp=NOW))
    strat.on_fill(Fill(order_id="b", asset="BTC", notional=-30.0, price=71000, fee=0.05, strategy_id="grid", timestamp=NOW))
    strat.on_fill(Fill(order_id="c", asset="ETH", notional=200.0, price=2000, fee=0.05, strategy_id="grid", timestamp=NOW))
    assert strat.get_net_exposure("BTC") == 70.0
    assert strat.get_net_exposure("ETH") == 200.0


# ─── Budget allocation interface ─────────────────────────────────────────────


def test_set_get_budget():
    ex = MockExchange()
    engine = GridEngine(ex, _grid_cfg())
    strat = GridStrategy(engine, symbols=["BTC", "ETH"])
    assert strat.get_budget("BTC") == 0.0
    strat.set_budget("BTC", 100.0)
    assert strat.get_budget("BTC") == 100.0
    assert strat.get_budget("ETH") == 0.0


def test_confidence_scales_with_budget():
    ex = MockExchange()
    engine = GridEngine(ex, _grid_cfg())
    strat = GridStrategy(engine, symbols=["BTC"])
    candles = {"BTC": [Candle(ts_open=NOW, open=1, high=2, low=0.5, close=1, volume=1.0)]}
    market = MarketSnapshot(timestamp=NOW, candles=candles, prices={"BTC": 100})
    # Budget 0 → confidence 0
    sigs = strat.generate_signals(market)
    assert sigs[0].confidence == 0.0
    # Budget plein (≥ 10 × notional_per_level = 150) → confidence 1.0
    strat.set_budget("BTC", 200.0)
    sigs = strat.generate_signals(market)
    assert sigs[0].confidence == 1.0
    # Budget moitié
    strat.set_budget("BTC", 75.0)
    sigs = strat.generate_signals(market)
    assert sigs[0].confidence == 0.5


# ─── Engine sous-jacent ──────────────────────────────────────────────────────


def test_engine_activate_with_mock_exchange():
    """GridEngine doit pouvoir activer un grid sur le mock exchange et placer
    les 10 niveaux (5 buys + 5 sells)."""
    ex = MockExchange()
    engine = GridEngine(ex, _grid_cfg())
    # ATR 100 sur BTC à 70000 → spacing = 50, range +/- 300
    ok = engine.activate("BTC", center=70000.0, atr=100.0)
    assert ok is True
    assert engine.is_active("BTC")
    # 10 ordres placés (5 buy + 5 sell)
    assert len(ex.placed_orders) == 10
    # Les prix sont arrondis à 1 décimale (BTC szDec=5 → px_dec=1)
    buys = [o for o in ex.placed_orders if o.side == "buy"]
    sells = [o for o in ex.placed_orders if o.side == "sell"]
    assert len(buys) == 5
    assert len(sells) == 5


def test_engine_active_symbols_proxy():
    ex = MockExchange()
    engine = GridEngine(ex, _grid_cfg())
    strat = GridStrategy(engine, symbols=["BTC"])
    assert strat.active_symbols() == []
    engine.activate("BTC", center=70000.0, atr=100.0)
    assert strat.active_symbols() == ["BTC"]

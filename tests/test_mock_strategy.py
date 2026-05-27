"""Test que MockStrategy se conforme au Protocol StrategyAgent."""
from __future__ import annotations

import datetime as dt

import pytest

from core.interfaces import StrategyAgent
from core.types import Fill
from strategies.mock import MockStrategy
from tests.test_types import make_market, NOW


def test_mock_is_strategy_agent():
    """Vérifie via isinstance() runtime que le Protocol est satisfait.

    Protocols Python supportent isinstance() seulement avec @runtime_checkable.
    À défaut, on vérifie la présence des attributs/méthodes manuellement.
    """
    m = MockStrategy()
    # attribut strategy_id
    assert hasattr(m, "strategy_id")
    assert isinstance(m.strategy_id, str)
    # méthodes
    assert callable(getattr(m, "generate_signals", None))
    assert callable(getattr(m, "on_fill", None))


def test_mock_generates_valid_signal():
    m = MockStrategy(strategy_id="mock", target_notional=200.0, confidence=0.8)
    market = make_market("BTC", n_candles=10)
    signals = m.generate_signals(market)
    assert len(signals) == 1
    s = signals[0]
    assert s.strategy_id == "mock"
    assert s.asset == "BTC"
    assert s.target_notional == 200.0
    assert s.confidence == 0.8
    assert s.direction == 1.0


def test_mock_empty_market_returns_empty():
    m = MockStrategy()
    from core.types import MarketSnapshot
    empty = MarketSnapshot(timestamp=NOW, candles={}, prices={})
    assert m.generate_signals(empty) == []


def test_mock_on_fill_accumulates():
    m = MockStrategy()
    f1 = Fill(order_id="1", asset="BTC", notional=100.0, price=70000.0, fee=0.1, strategy_id="mock", timestamp=NOW)
    f2 = Fill(order_id="2", asset="ETH", notional=-50.0, price=3000.0, fee=0.05, strategy_id="mock", timestamp=NOW)
    m.on_fill(f1)
    m.on_fill(f2)
    assert len(m.fills_received) == 2
    assert m.fills_received[0].order_id == "1"
    assert m.fills_received[1].order_id == "2"


def test_signal_invalid_direction_rejected_at_construction():
    """Si MockStrategy reçoit un mauvais paramètre, la construction du Signal échoue."""
    m = MockStrategy(direction=2.0)  # hors [-1, 1]
    market = make_market("BTC", n_candles=5)
    with pytest.raises(ValueError):
        m.generate_signals(market)

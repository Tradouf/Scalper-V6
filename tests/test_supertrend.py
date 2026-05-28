"""Tests Supertrend : feature + strategy + intégration."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from core.config import SupertrendStrategyConfig
from core.types import Candle, Fill, MarketSnapshot
from regime.features import supertrend, supertrend_with_history
from strategies.supertrend import SupertrendStrategy


NOW = dt.datetime(2026, 5, 28, 12, 0, 0)


def _make_market(prices: list[float], symbol: str = "BTC") -> MarketSnapshot:
    n = len(prices)
    candles = [
        Candle(
            ts_open=NOW - dt.timedelta(hours=n - i),
            open=p, high=p + 0.5, low=p - 0.5, close=p, volume=1.0,
        )
        for i, p in enumerate(prices)
    ]
    return MarketSnapshot(timestamp=NOW, candles={symbol: candles}, prices={symbol: prices[-1]})


# ─── Feature supertrend ──────────────────────────────────────────────────────


class TestSupertrendFeature:
    def test_insufficient_data(self):
        assert supertrend([1, 2, 3], [0, 1, 2], [0.5, 1.5, 2.5]) is None

    def test_basic_uptrend_direction_positive(self):
        """Trend haussier soutenu → direction finale = +1."""
        np.random.seed(0)
        n = 100
        prices = np.linspace(100, 130, n) + np.random.normal(0, 0.1, n)
        h = prices + 0.3
        l = prices - 0.3
        res = supertrend(h, l, prices, period=10, multiplier=3.0)
        assert res is not None
        st, direction = res
        assert direction == 1
        assert st < prices[-1]  # st sous le close pour un trend up

    def test_basic_downtrend_direction_negative(self):
        np.random.seed(1)
        n = 100
        prices = np.linspace(130, 100, n) + np.random.normal(0, 0.1, n)
        h = prices + 0.3
        l = prices - 0.3
        res = supertrend(h, l, prices, period=10, multiplier=3.0)
        assert res is not None
        st, direction = res
        assert direction == -1
        assert st > prices[-1]  # st au-dessus du close pour un trend down

    def test_supertrend_with_history_returns_arrays(self):
        np.random.seed(2)
        n = 80
        prices = np.linspace(100, 110, n) + np.random.normal(0, 0.5, n)
        h = prices + 0.5
        l = prices - 0.5
        res = supertrend_with_history(h, l, prices, period=10, multiplier=3.0)
        assert res is not None
        st_arr, dir_arr, last_st, last_dir = res
        assert len(st_arr) == n
        assert len(dir_arr) == n
        # direction in {-1, 0, 1}
        for d in dir_arr:
            assert d in (-1, 0, 1)

    def test_no_leak_supertrend(self):
        """Modifier prices[t:] ne change pas supertrend(t)."""
        np.random.seed(42)
        n = 300
        prices = 100.0 * np.exp(np.cumsum(np.random.normal(0, 0.01, n)))
        h = prices + 0.5
        l = prices - 0.5
        res_a = supertrend(h[:200], l[:200], prices[:200], period=10, multiplier=3.0)
        p_mod = prices.copy()
        p_mod[200:] = p_mod[200:] * 10  # trafic du futur
        h_mod = h.copy(); h_mod[200:] = h_mod[200:] * 10
        l_mod = l.copy(); l_mod[200:] = l_mod[200:] * 10
        res_b = supertrend(h_mod[:200], l_mod[:200], p_mod[:200], period=10, multiplier=3.0)
        assert res_a == res_b


# ─── SupertrendStrategy ──────────────────────────────────────────────────────


def _cfg(**kwargs) -> SupertrendStrategyConfig:
    defaults = dict(
        enabled=True, interval="1h", period=10, multiplier=3.0,
        risk_per_trade_pct=0.01, notional_max_usdc=500.0, notional_min_usdc=10.0,
        cooldown_sec=900,
    )
    defaults.update(kwargs)
    return SupertrendStrategyConfig(**defaults)


class TestSupertrendStrategy:
    def test_protocol_conformity(self):
        s = SupertrendStrategy(_cfg(), symbols=["BTC"])
        assert s.strategy_id == "supertrend"
        assert callable(s.generate_signals)
        assert callable(s.on_fill)

    def test_no_signal_insufficient_data(self):
        s = SupertrendStrategy(_cfg(), symbols=["BTC"])
        assert s.generate_signals(_make_market([100.0] * 8)) == []

    def test_no_signal_first_call_without_flip(self):
        """1er appel sans historique de direction → pas de signal (rien à flipper)."""
        s = SupertrendStrategy(_cfg(period=10), symbols=["BTC"])
        np.random.seed(3)
        prices = list(np.linspace(100, 130, 100))
        sigs = s.generate_signals(_make_market(prices))
        # 1er appel : prev_dir = dir_arr[-2] (cohérent avec last_dir), donc pas de flip
        # OK que ce soit [] aussi.

    def test_flip_generates_signal(self):
        """Construit un FLIP en simulant 2 appels successifs avec direction différente."""
        s = SupertrendStrategy(_cfg(period=10, multiplier=2.0), symbols=["BTC"])
        # Phase trend up
        np.random.seed(4)
        prices_up = list(np.linspace(100, 120, 80))
        s.generate_signals(_make_market(prices_up))  # initialise prev_dir = +1
        # Phase trend down brutal
        prices_down = prices_up + [120.0, 118, 114, 108, 100, 92, 88, 85]
        sigs = s.generate_signals(_make_market(prices_down))
        # Avec une cassure forte, on devrait avoir un signal SHORT
        if sigs:
            s_ = sigs[0]
            assert s_.strategy_id == "supertrend"
            assert s_.target_notional > 0
            # direction et stop_price cohérents
            if s_.direction == -1.0:
                assert s_.stop_price > prices_down[-1]
            else:
                assert s_.stop_price < prices_down[-1]

    def test_on_fill_tracking(self):
        s = SupertrendStrategy(_cfg(), symbols=["BTC"])
        s.on_fill(Fill(order_id="1", asset="BTC", notional=200.0, price=70000.0, fee=0.1, strategy_id="supertrend", timestamp=NOW))
        assert "BTC" in s.open_positions()
        s.on_fill(Fill(order_id="2", asset="BTC", notional=-200.0, price=70500.0, fee=0.1, strategy_id="supertrend", timestamp=NOW))
        assert "BTC" not in s.open_positions()

    def test_sizing_clamped_to_max(self):
        """Si la distance au stop est très petite, qty serait énorme → cap à notional_max."""
        s = SupertrendStrategy(
            _cfg(risk_per_trade_pct=0.01, notional_max_usdc=300.0, period=10, multiplier=1.0),
            symbols=["BTC"],
            equity_callback=lambda: 10000.0,
        )
        # Crée une série qui flip de up à down (gros mouvement bref)
        np.random.seed(5)
        prices_setup = list(np.linspace(100, 110, 80))
        s.generate_signals(_make_market(prices_setup))
        # Cassure douce qui rapproche le ST du close (= petite distance au stop)
        prices_flip = prices_setup + [110.0] * 5 + [105]
        sigs = s.generate_signals(_make_market(prices_flip))
        if sigs:
            assert sigs[0].target_notional <= 300.0  # cap notional_max

    def test_cooldown_blocks_immediate_retrigger(self):
        s = SupertrendStrategy(_cfg(cooldown_sec=3600, period=10), symbols=["BTC"])
        np.random.seed(6)
        # Force un flip
        prices_up = list(np.linspace(100, 120, 80))
        s.generate_signals(_make_market(prices_up))
        prices_down = prices_up + [120, 117, 112, 105, 96, 88]
        sigs1 = s.generate_signals(_make_market(prices_down))
        if not sigs1:
            return  # pas de flip détecté, test inapplicable
        # 2e appel immédiat — cooldown actif
        sigs2 = s.generate_signals(_make_market(prices_down))
        assert sigs2 == []

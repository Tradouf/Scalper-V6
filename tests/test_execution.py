"""Tests Execution : reconcile (diff + bande non-trade), PaperExchange (fills),
intégration engine+paper."""
from __future__ import annotations

import datetime as dt

import pytest

from core.config import ExecutionConfig
from core.types import TargetPortfolio, TargetPosition
from execution.engine import ExecutionEngine
from execution.order import OrderImpl
from execution.paper import PaperExchange
from execution.portfolio import PortfolioImpl


NOW = dt.datetime(2026, 5, 28, 12, 0, 0)


def _target(positions: list[TargetPosition]) -> TargetPortfolio:
    gross = sum(abs(p.target_notional) for p in positions)
    net = sum(p.target_notional for p in positions)
    return TargetPortfolio(timestamp=NOW, positions=positions, gross_exposure=gross, net_exposure=net)


# ─── OrderImpl ───────────────────────────────────────────────────────────────


class TestOrderImpl:
    def test_valid(self):
        o = OrderImpl(asset="BTC", side="buy", qty=0.01, order_type="market")
        assert o.qty == 0.01

    def test_negative_qty_rejected(self):
        with pytest.raises(ValueError):
            OrderImpl(asset="BTC", side="buy", qty=-1.0, order_type="market")

    def test_invalid_side_rejected(self):
        with pytest.raises(ValueError):
            OrderImpl(asset="BTC", side="long", qty=0.01, order_type="market")

    def test_limit_needs_price(self):
        with pytest.raises(ValueError):
            OrderImpl(asset="BTC", side="buy", qty=0.01, order_type="limit", price=None)


# ─── PortfolioImpl ───────────────────────────────────────────────────────────


class TestPortfolio:
    def test_empty(self):
        pf = PortfolioImpl()
        assert pf.positions == {}
        assert pf.equity == 0.0

    def test_set_get_position(self):
        pf = PortfolioImpl()
        pf.set_position("BTC", 500.0)
        assert pf.positions["BTC"] == 500.0

    def test_zero_position_removes(self):
        pf = PortfolioImpl()
        pf.set_position("BTC", 500.0)
        pf.set_position("BTC", 0.0)
        assert "BTC" not in pf.positions

    def test_adjust_position(self):
        pf = PortfolioImpl()
        pf.adjust_position("BTC", 100.0)
        pf.adjust_position("BTC", 50.0)
        assert pf.positions["BTC"] == 150.0
        pf.adjust_position("BTC", -150.0)
        assert "BTC" not in pf.positions


# ─── ExecutionEngine.reconcile ───────────────────────────────────────────────


class TestReconcile:
    def _engine(self, rebalance_threshold: float = 0.005) -> tuple[ExecutionEngine, PaperExchange]:
        ex = PaperExchange()
        ex.update_mark_price("BTC", 70000.0)
        ex.update_mark_price("ETH", 2000.0)
        cfg = ExecutionConfig(paper_mode=True, rebalance_threshold_pct=rebalance_threshold)
        return ExecutionEngine(ex, cfg), ex

    def test_empty_target_empty_current_no_orders(self):
        engine, ex = self._engine()
        pf = PortfolioImpl()
        pf.set_equity(1000.0)
        target = _target([])
        orders = engine.reconcile(target, pf)
        assert orders == []

    def test_open_new_position(self):
        engine, ex = self._engine()
        pf = PortfolioImpl(_equity=1000.0)
        target = _target([TargetPosition(asset="BTC", target_notional=500.0, contributing_strategies={"mr": 500.0})])
        orders = engine.reconcile(target, pf)
        assert len(orders) == 1
        o = orders[0]
        assert o.asset == "BTC"
        assert o.side == "buy"
        assert abs(o.qty - 500.0 / 70000.0) < 1e-9
        assert o.reduce_only is False
        assert o.strategy_id == "mr"

    def test_close_existing_position(self):
        engine, ex = self._engine()
        pf = PortfolioImpl(_positions={"BTC": 500.0}, _equity=1000.0)
        target = _target([])  # rien : on veut fermer
        orders = engine.reconcile(target, pf)
        assert len(orders) == 1
        o = orders[0]
        assert o.asset == "BTC"
        assert o.side == "sell"  # ferme un long
        assert o.reduce_only is True

    def test_reduce_position(self):
        engine, ex = self._engine()
        pf = PortfolioImpl(_positions={"BTC": 1000.0}, _equity=1000.0)
        target = _target([TargetPosition(asset="BTC", target_notional=400.0)])
        orders = engine.reconcile(target, pf)
        assert len(orders) == 1
        o = orders[0]
        assert o.side == "sell"
        assert o.reduce_only is True

    def test_flip_position(self):
        """1000 → -500 : on doit fermer le long ET ouvrir un short."""
        engine, ex = self._engine()
        pf = PortfolioImpl(_positions={"BTC": 1000.0}, _equity=1000.0)
        target = _target([TargetPosition(asset="BTC", target_notional=-500.0)])
        orders = engine.reconcile(target, pf)
        # 1 ordre sell de qty = (1000 + 500) / 70000
        assert len(orders) == 1
        o = orders[0]
        assert o.side == "sell"
        # reduce_only=False ici car on inverse (notion: notre code RO=True seulement si on reste du même côté)
        assert o.reduce_only is False  # le net cross over → pas RO

    def test_rebalance_threshold_skips_small_diff(self):
        """Diff < threshold × max(gross, equity) → pas d'ordre."""
        engine, ex = self._engine(rebalance_threshold=0.05)  # 5%
        pf = PortfolioImpl(_positions={"BTC": 500.0}, _equity=1000.0)
        # Diff = 510 - 500 = 10, threshold = 5% × max(510, 1000) = 50 → skip
        target = _target([TargetPosition(asset="BTC", target_notional=510.0)])
        orders = engine.reconcile(target, pf)
        assert orders == []

    def test_rebalance_threshold_keeps_big_diff(self):
        engine, ex = self._engine(rebalance_threshold=0.05)
        pf = PortfolioImpl(_positions={"BTC": 500.0}, _equity=1000.0)
        # Diff = 200, threshold = 5% × 1000 = 50 → garde
        target = _target([TargetPosition(asset="BTC", target_notional=700.0)])
        orders = engine.reconcile(target, pf)
        assert len(orders) == 1


# ─── PaperExchange ───────────────────────────────────────────────────────────


class TestPaperExchange:
    def test_place_market_fills_immediately(self):
        ex = PaperExchange()
        ex.update_mark_price("BTC", 70000.0)
        from execution.types import OrderRequest
        r = ex.place_order(OrderRequest(symbol="BTC", side="buy", qty=0.01, order_type="market"))
        assert r.status == "filled"
        # Position paper créée
        assert ex.position_notional("BTC") > 0

    def test_place_market_no_mark_rejected(self):
        ex = PaperExchange()
        from execution.types import OrderRequest
        # Pas de mark price pour BTC → rejet
        r = ex.place_order(OrderRequest(symbol="BTC", side="buy", qty=0.01, order_type="market"))
        assert r.status == "rejected"

    def test_market_slippage_applied(self):
        ex = PaperExchange(slippage_bps=10.0)  # 0.1%
        ex.update_mark_price("BTC", 70000.0)
        from execution.types import OrderRequest
        r = ex.place_order(OrderRequest(symbol="BTC", side="buy", qty=0.01, order_type="market"))
        # Fill price = 70000 × 1.001 = 70070
        assert abs(r.price - 70070.0) < 1e-6

    def test_short_then_close_via_reduce_only(self):
        ex = PaperExchange()
        ex.update_mark_price("BTC", 70000.0)
        from execution.types import OrderRequest
        # Ouvre short
        ex.place_order(OrderRequest(symbol="BTC", side="sell", qty=0.01, order_type="market"))
        assert ex.position_notional("BTC") < 0
        # Ferme via RO buy
        ex.place_order(OrderRequest(symbol="BTC", side="buy", qty=0.01, order_type="market", reduce_only=True))
        # Position fermée
        assert ex.position_notional("BTC") == 0.0

    def test_limit_crossing_fills(self):
        ex = PaperExchange()
        ex.update_mark_price("BTC", 70000.0)
        from execution.types import OrderRequest
        # buy limit 71000 (au-dessus du mark) → crossing → fill immédiat
        r = ex.place_order(OrderRequest(symbol="BTC", side="buy", qty=0.01, order_type="limit", price=71000.0))
        assert r.status == "filled"

    def test_limit_non_crossing_open(self):
        ex = PaperExchange()
        ex.update_mark_price("BTC", 70000.0)
        from execution.types import OrderRequest
        # buy limit 69000 (sous le mark) → resting
        r = ex.place_order(OrderRequest(symbol="BTC", side="buy", qty=0.01, order_type="limit", price=69000.0))
        assert r.status == "accepted"
        assert ex.position_notional("BTC") == 0.0
        # Vu dans get_open_orders
        oos = ex.get_open_orders("BTC")
        assert len(oos) == 1


# ─── Engine + Paper intégration ──────────────────────────────────────────────


class TestEngineSubmit:
    def test_submit_market_returns_fill(self):
        ex = PaperExchange()
        ex.update_mark_price("BTC", 70000.0)
        engine = ExecutionEngine(ex, ExecutionConfig(paper_mode=True))
        orders = [OrderImpl(asset="BTC", side="buy", qty=0.01, order_type="market", strategy_id="mr")]
        fills = engine.submit(orders)
        assert len(fills) == 1
        f = fills[0]
        assert f.asset == "BTC"
        assert f.notional > 0  # buy → positif
        assert f.strategy_id == "mr"
        assert f.fee > 0

    def test_submit_propagates_strategy_id(self):
        ex = PaperExchange()
        ex.update_mark_price("ETH", 2000.0)
        engine = ExecutionEngine(ex, ExecutionConfig(paper_mode=True))
        orders = [
            OrderImpl(asset="ETH", side="sell", qty=0.5, order_type="market", strategy_id="momentum"),
        ]
        fills = engine.submit(orders)
        assert fills[0].strategy_id == "momentum"
        assert fills[0].notional < 0  # sell → négatif

    def test_full_loop_reconcile_then_submit(self):
        """Cycle complet : target → reconcile → submit → portfolio mis à jour."""
        ex = PaperExchange()
        ex.update_mark_price("BTC", 70000.0)
        engine = ExecutionEngine(ex, ExecutionConfig(paper_mode=True))
        pf = PortfolioImpl(_equity=10000.0)
        target = _target([
            TargetPosition(asset="BTC", target_notional=1000.0, contributing_strategies={"mr": 1000.0}),
        ])
        orders = engine.reconcile(target, pf)
        fills = engine.submit(orders)
        assert len(fills) == 1
        assert fills[0].strategy_id == "mr"
        # Position paper mise à jour
        assert ex.position_notional("BTC") > 0

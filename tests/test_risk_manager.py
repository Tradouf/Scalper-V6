"""Tests RiskManager : projection caps, kill-switch."""
from __future__ import annotations

import datetime as dt

import pytest

from core.config import RiskConfig
from core.types import TargetPortfolio, TargetPosition
from risk.manager import RiskManager
from risk.state import RiskStateImpl


NOW = dt.datetime(2026, 5, 28, 12, 0, 0)


def _pos(asset: str, notional: float, contribs: dict = None) -> TargetPosition:
    if contribs is None:
        contribs = {"test_strat": notional}
    return TargetPosition(asset=asset, target_notional=notional, contributing_strategies=contribs)


def _portfolio(positions: list[TargetPosition]) -> TargetPortfolio:
    gross = sum(abs(p.target_notional) for p in positions)
    net = sum(p.target_notional for p in positions)
    return TargetPortfolio(timestamp=NOW, positions=positions, gross_exposure=gross, net_exposure=net)


class TestKillSwitch:
    def test_dd_above_threshold_kills(self):
        cfg = RiskConfig(kill_switch_dd_pct=0.10)
        rm = RiskManager(cfg)
        state = RiskStateImpl(equity=1000.0, current_drawdown=0.11)
        assert rm.kill_switch_triggered(state) is True

    def test_dd_below_threshold_ok(self):
        cfg = RiskConfig(kill_switch_dd_pct=0.10)
        rm = RiskManager(cfg)
        state = RiskStateImpl(equity=1000.0, current_drawdown=0.05)
        assert rm.kill_switch_triggered(state) is False

    def test_daily_loss_above_limit_kills(self):
        cfg = RiskConfig(daily_loss_limit_pct=0.03)
        rm = RiskManager(cfg)
        state = RiskStateImpl(equity=1000.0, daily_pnl_pct=-0.04)
        assert rm.kill_switch_triggered(state) is True

    def test_project_returns_empty_on_kill(self):
        cfg = RiskConfig(kill_switch_dd_pct=0.10)
        rm = RiskManager(cfg)
        state = RiskStateImpl(equity=1000.0, current_drawdown=0.15)
        target = _portfolio([_pos("BTC", 100.0)])
        projected = rm.project(target, state)
        assert projected.positions == []
        assert projected.gross_exposure == 0.0
        assert projected.net_exposure == 0.0


class TestCaps:
    def test_per_asset_cap_scales_down(self):
        """Position > max_per_asset → scale-down."""
        cfg = RiskConfig(max_per_asset_pct=0.4, max_gross_exposure_pct=10.0, kill_switch_dd_pct=0.5, daily_loss_limit_pct=0.1)
        rm = RiskManager(cfg)
        state = RiskStateImpl(equity=1000.0)
        # 500 > 0.4 × 1000 = 400 → cap à 400
        target = _portfolio([_pos("BTC", 500.0)])
        projected = rm.project(target, state)
        assert abs(projected.positions[0].target_notional - 400.0) < 1e-6
        # Attribution scalée
        for v in projected.positions[0].contributing_strategies.values():
            assert abs(v - 400.0) < 1e-6  # 500 × 0.8

    def test_per_asset_cap_short(self):
        cfg = RiskConfig(max_per_asset_pct=0.4, max_gross_exposure_pct=10.0, kill_switch_dd_pct=0.5, daily_loss_limit_pct=0.1)
        rm = RiskManager(cfg)
        state = RiskStateImpl(equity=1000.0)
        # -500 → cap à -400
        target = _portfolio([_pos("BTC", -500.0, contribs={"momentum": -500.0})])
        projected = rm.project(target, state)
        assert abs(projected.positions[0].target_notional - (-400.0)) < 1e-6

    def test_no_cap_when_under(self):
        cfg = RiskConfig(max_per_asset_pct=0.4, max_gross_exposure_pct=10.0, kill_switch_dd_pct=0.5, daily_loss_limit_pct=0.1)
        rm = RiskManager(cfg)
        state = RiskStateImpl(equity=1000.0)
        target = _portfolio([_pos("BTC", 200.0)])  # < 400
        projected = rm.project(target, state)
        assert abs(projected.positions[0].target_notional - 200.0) < 1e-6

    def test_gross_cap_scales_all(self):
        """Σ |notional| > max_gross → scale-down proportionnel."""
        cfg = RiskConfig(max_per_asset_pct=2.0, max_gross_exposure_pct=2.0, kill_switch_dd_pct=0.5, daily_loss_limit_pct=0.1)
        rm = RiskManager(cfg)
        state = RiskStateImpl(equity=1000.0)
        # 3 positions × 1000 = 3000 gross > 2 × 1000 = 2000 max
        target = _portfolio([_pos("BTC", 1000.0), _pos("ETH", 1000.0), _pos("SOL", 1000.0)])
        projected = rm.project(target, state)
        # Chaque position scalée par 2000/3000 = 2/3
        for p in projected.positions:
            assert abs(p.target_notional - 1000.0 * 2 / 3) < 1e-6
        assert abs(projected.gross_exposure - 2000.0) < 1e-6

    def test_combined_per_asset_and_gross_caps(self):
        cfg = RiskConfig(max_per_asset_pct=0.4, max_gross_exposure_pct=1.0, kill_switch_dd_pct=0.5, daily_loss_limit_pct=0.1)
        rm = RiskManager(cfg)
        state = RiskStateImpl(equity=1000.0)
        # 1500 → cap actif à 400. Puis 3 × 400 = 1200 > 1000 → scale 1000/1200
        target = _portfolio([_pos("BTC", 1500.0), _pos("ETH", 1500.0), _pos("SOL", 1500.0)])
        projected = rm.project(target, state)
        assert abs(projected.gross_exposure - 1000.0) < 1e-6
        # Toutes les positions ont le même notional (égalisées par le double cap)
        for p in projected.positions:
            assert abs(p.target_notional - 1000.0 / 3) < 1e-6

    def test_empty_portfolio_returns_empty(self):
        cfg = RiskConfig()
        rm = RiskManager(cfg)
        state = RiskStateImpl(equity=1000.0)
        target = _portfolio([])
        projected = rm.project(target, state)
        assert projected.positions == []
        assert projected.gross_exposure == 0.0


class TestAttributionPreserved:
    def test_contribs_scaled_proportionally(self):
        """Quand on scale une position, contributing_strategies doit suivre."""
        cfg = RiskConfig(max_per_asset_pct=0.4, max_gross_exposure_pct=10.0, kill_switch_dd_pct=0.5, daily_loss_limit_pct=0.1)
        rm = RiskManager(cfg)
        state = RiskStateImpl(equity=1000.0)
        contribs = {"grid": 300.0, "mean_reversion": 200.0}  # somme 500
        target = _portfolio([_pos("BTC", 500.0, contribs=contribs)])
        projected = rm.project(target, state)
        # Scale = 400/500 = 0.8
        out_contribs = projected.positions[0].contributing_strategies
        assert abs(out_contribs["grid"] - 240.0) < 1e-6  # 300 × 0.8
        assert abs(out_contribs["mean_reversion"] - 160.0) < 1e-6  # 200 × 0.8

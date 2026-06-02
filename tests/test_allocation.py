"""Tests allocation : performance scoring + allocator."""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass

import pytest

from allocation.allocator import RuleBasedAllocator
from allocation.performance import PerformanceScorer
from core.config import load_config
from core.types import (
    Fill,
    Regime,
    RegimeState,
    Signal,
    TargetPortfolio,
)


NOW = dt.datetime(2026, 5, 27, 12, 0, 0)


@dataclass
class FakePortfolio:
    positions: dict
    equity: float


def _regime(probas: dict[Regime, float], label: Regime = None) -> RegimeState:
    if label is None:
        label = max(probas, key=probas.get)
    return RegimeState(
        timestamp=NOW,
        probabilities=probas,
        label=label,
        confidence=probas[label],
    )


def _signal(strategy_id: str, asset: str, direction: float = 1.0, notional: float = 100.0, confidence: float = 0.8) -> Signal:
    return Signal(
        strategy_id=strategy_id,
        asset=asset,
        direction=direction,
        target_notional=notional,
        expected_edge_bps=30.0,
        confidence=confidence,
        stop_price=None,
        horizon_bars=4,
        timestamp=NOW,
    )


# ─── PerformanceScorer ───────────────────────────────────────────────────────


class TestPerformanceScorer:
    def test_no_fills_neutral_score(self):
        scorer = PerformanceScorer()
        assert scorer.scores() == {}

    def test_fill_with_no_strategy_id_ignored(self):
        scorer = PerformanceScorer()
        scorer.on_fill(Fill(order_id="x", asset="BTC", notional=100.0, price=70000.0, fee=0.1, strategy_id=None, timestamp=NOW))
        assert scorer.scores() == {}

    def test_score_in_bounds(self):
        """Quel que soit l'input, le score doit être ∈ [mult_min, mult_max]."""
        scorer = PerformanceScorer(mult_min=0.3, mult_max=1.5, min_days_for_score=2)
        # Inject 10 jours de PnL très positif
        for i in range(10):
            scorer.record_realized_pnl("grid", dt.date(2026, 5, 1) + dt.timedelta(days=i), 5.0)
        scores = scorer.scores()
        assert 0.3 <= scores["grid"] <= 1.5

    def test_positive_pnl_boosts_score(self):
        scorer = PerformanceScorer(mult_min=0.3, mult_max=1.5, min_days_for_score=2)
        # Grid : positif
        for i in range(15):
            scorer.record_realized_pnl("grid", dt.date(2026, 5, 1) + dt.timedelta(days=i), 2.0)
        # MR : négatif
        for i in range(15):
            scorer.record_realized_pnl("mean_reversion", dt.date(2026, 5, 1) + dt.timedelta(days=i), -2.0)
        scores = scorer.scores()
        assert scores["grid"] > scores["mean_reversion"]

    def test_insufficient_history_neutral(self):
        scorer = PerformanceScorer(min_days_for_score=10)
        # Seulement 3 jours
        for i in range(3):
            scorer.record_realized_pnl("grid", dt.date(2026, 5, 1) + dt.timedelta(days=i), 1.0)
        scores = scorer.scores()
        assert scores["grid"] == PerformanceScorer.NEUTRAL_SCORE


# ─── RuleBasedAllocator ──────────────────────────────────────────────────────


class TestAllocator:
    def _cfg(self):
        return load_config().allocation

    def _portfolio(self) -> FakePortfolio:
        return FakePortfolio(positions={}, equity=1000.0)

    def test_pure_range_regime_grid_mr_dominent(self):
        """En régime range pur, les poids grid + MR doivent dominer."""
        alloc = RuleBasedAllocator(self._cfg())
        regime = _regime({
            Regime.TREND_UP: 0.0,
            Regime.TREND_DOWN: 0.0,
            Regime.RANGE: 1.0,
            Regime.HIGH_VOL: 0.0,
        }, label=Regime.RANGE)
        weights = alloc.get_weights(regime, perf_scores={})
        # grid + mr > 0.8 ; momentum << 0.2
        assert weights["grid"] + weights["mean_reversion"] > 0.8
        assert weights["momentum"] < 0.2
        # somme = 1
        assert math.isclose(sum(weights.values()), 1.0, abs_tol=1e-9)

    def test_pure_trend_regime_supertrend_active(self):
        """Exclusif : en trend, seul supertrend a un poids (momentum off)."""
        alloc = RuleBasedAllocator(self._cfg())
        regime = _regime({
            Regime.TREND_UP: 1.0,
            Regime.TREND_DOWN: 0.0,
            Regime.RANGE: 0.0,
            Regime.HIGH_VOL: 0.0,
        }, label=Regime.TREND_UP)
        weights = alloc.get_weights(regime, perf_scores={})
        assert weights["supertrend"] == pytest.approx(1.0)
        assert weights["grid"] == 0.0
        assert weights["mean_reversion"] == 0.0
        assert weights["momentum"] == 0.0

    def test_label_selects_single_strategy(self):
        """Exclusif : c'est le LABEL dominant (pas le blend de probas) qui choisit
        la stratégie active. Probas mixtes mais label=RANGE → mean_reversion seul."""
        alloc = RuleBasedAllocator(self._cfg())
        regime = _regime({
            Regime.TREND_UP: 0.5,
            Regime.TREND_DOWN: 0.0,
            Regime.RANGE: 0.5,
            Regime.HIGH_VOL: 0.0,
        }, label=Regime.RANGE)
        weights = alloc.get_weights(regime, perf_scores={})
        nonzero = [k for k, v in weights.items() if v > 0]
        assert nonzero == ["mean_reversion"]
        assert math.isclose(sum(weights.values()), 1.0, abs_tol=1e-9)

    def test_each_regime_activates_mapped_strategy(self):
        """Mapping exclusif complet : range→MR, high_vol→grid, trends→supertrend."""
        alloc = RuleBasedAllocator(self._cfg())
        cases = {
            Regime.RANGE: "mean_reversion",
            Regime.HIGH_VOL: "grid",
            Regime.TREND_UP: "supertrend",
            Regime.TREND_DOWN: "supertrend",
        }
        for reg, strat in cases.items():
            regime = _regime({r: (1.0 if r == reg else 0.0) for r in Regime}, label=reg)
            w = alloc.get_weights(regime, perf_scores={})
            active = [k for k, v in w.items() if v > 0.0]
            assert active == [strat], f"{reg.value} → {active} (attendu {strat})"

    def test_exclusive_weight_is_one_regardless_of_perf(self):
        """Sous exclusivité, la strat active normalise à 1.0 quelle que soit la
        perf (le multiplicateur perf devient inerte avec une seule stratégie)."""
        alloc = RuleBasedAllocator(self._cfg())
        regime = _regime({
            Regime.TREND_UP: 0.0, Regime.TREND_DOWN: 0.0,
            Regime.RANGE: 0.0, Regime.HIGH_VOL: 1.0,
        }, label=Regime.HIGH_VOL)
        w_high = alloc.get_weights(regime, perf_scores={"grid": 100.0})
        w_low = alloc.get_weights(regime, perf_scores={"grid": -100.0})
        assert w_high["grid"] == pytest.approx(1.0)
        assert w_low["grid"] == pytest.approx(1.0)

    def test_no_signal_returns_empty_portfolio(self):
        alloc = RuleBasedAllocator(self._cfg())
        regime = _regime({Regime.TREND_UP: 0.25, Regime.TREND_DOWN: 0.25,
                          Regime.RANGE: 0.25, Regime.HIGH_VOL: 0.25})
        tp = alloc.allocate(signals=[], regime=regime, current_portfolio=self._portfolio(), perf_scores={})
        assert tp.positions == []
        assert tp.gross_exposure == 0.0
        assert tp.net_exposure == 0.0

    def test_signals_aggregated_by_asset(self):
        """Deux signaux de la stratégie ACTIVE sur le même asset → sommés."""
        alloc = RuleBasedAllocator(self._cfg())
        regime = _regime({Regime.TREND_UP: 0.0, Regime.TREND_DOWN: 0.0,
                          Regime.RANGE: 1.0, Regime.HIGH_VOL: 0.0})
        signals = [
            _signal("mean_reversion", "BTC", direction=1.0, notional=200.0, confidence=1.0),
            _signal("mean_reversion", "BTC", direction=1.0, notional=100.0, confidence=1.0),
        ]
        tp = alloc.allocate(signals, regime, self._portfolio(), perf_scores={})
        assert len(tp.positions) == 1
        pos = tp.positions[0]
        assert pos.asset == "BTC"
        assert pos.target_notional > 0
        assert "mean_reversion" in pos.contributing_strategies

    def test_inactive_strategy_signal_ignored(self):
        """Exclusif : un signal d'une stratégie NON active dans le régime courant
        ne produit aucune cible (grid n'est pas la strat du régime range)."""
        alloc = RuleBasedAllocator(self._cfg())
        regime = _regime({Regime.TREND_UP: 0.0, Regime.TREND_DOWN: 0.0,
                          Regime.RANGE: 1.0, Regime.HIGH_VOL: 0.0}, label=Regime.RANGE)
        signals = [_signal("grid", "BTC", direction=1.0, notional=200.0, confidence=1.0)]
        tp = alloc.allocate(signals, regime, self._portfolio(), perf_scores={})
        assert tp.positions == []

    def test_signed_direction_preserved(self):
        alloc = RuleBasedAllocator(self._cfg())
        regime = _regime({Regime.TREND_UP: 1.0, Regime.TREND_DOWN: 0.0,
                          Regime.RANGE: 0.0, Regime.HIGH_VOL: 0.0}, label=Regime.TREND_UP)
        signals = [
            _signal("supertrend", "BTC", direction=1.0, notional=100.0, confidence=1.0),
            _signal("supertrend", "ETH", direction=-1.0, notional=100.0, confidence=1.0),
        ]
        tp = alloc.allocate(signals, regime, self._portfolio(), perf_scores={})
        btc = next(p for p in tp.positions if p.asset == "BTC")
        eth = next(p for p in tp.positions if p.asset == "ETH")
        assert btc.target_notional > 0  # LONG
        assert eth.target_notional < 0  # SHORT

    def test_close_signal_emits_attributed_zero_target(self):
        """Fix (a) : un CLOSE produit une TargetPosition à 0 PORTANT son
        strategy_id (pour que le reconcile attribue le fill de clôture), sans
        contribuer à l'exposition."""
        alloc = RuleBasedAllocator(self._cfg())
        regime = _regime({Regime.RANGE: 1.0, Regime.TREND_UP: 0.0,
                          Regime.TREND_DOWN: 0.0, Regime.HIGH_VOL: 0.0})
        signals = [
            _signal("mean_reversion", "BTC", direction=0.0, notional=0.0, confidence=1.0),  # CLOSE
        ]
        tp = alloc.allocate(signals, regime, self._portfolio(), perf_scores={})
        assert len(tp.positions) == 1
        pos = tp.positions[0]
        assert pos.asset == "BTC"
        assert pos.target_notional == 0.0
        assert "mean_reversion" in pos.contributing_strategies
        # N'ajoute aucune exposition.
        assert tp.gross_exposure == 0.0

    def test_close_signal_does_not_override_directional(self):
        """Fix (a) : un CLOSE sur un actif qu'une autre stratégie veut tenir ne
        doit PAS écraser l'exposition directionnelle."""
        alloc = RuleBasedAllocator(self._cfg())
        regime = _regime({Regime.RANGE: 1.0, Regime.TREND_UP: 0.0,
                          Regime.TREND_DOWN: 0.0, Regime.HIGH_VOL: 0.0})
        signals = [
            _signal("mean_reversion", "BTC", direction=1.0, notional=100.0, confidence=1.0),  # LONG
            _signal("momentum", "BTC", direction=0.0, notional=0.0, confidence=1.0),          # CLOSE
        ]
        tp = alloc.allocate(signals, regime, self._portfolio(), perf_scores={})
        btc = [p for p in tp.positions if p.asset == "BTC"]
        assert len(btc) == 1  # pas de doublon close + directionnel
        assert btc[0].target_notional > 0  # l'exposition directionnelle gagne

    def test_gross_and_net_exposure_computed(self):
        alloc = RuleBasedAllocator(self._cfg())
        # grid est la stratégie active en HIGH_VOL.
        regime = _regime({Regime.RANGE: 0.0, Regime.TREND_UP: 0.0,
                          Regime.TREND_DOWN: 0.0, Regime.HIGH_VOL: 1.0}, label=Regime.HIGH_VOL)
        signals = [
            _signal("grid", "BTC", direction=1.0, notional=100.0, confidence=1.0),
            _signal("grid", "ETH", direction=-1.0, notional=100.0, confidence=1.0),
        ]
        tp = alloc.allocate(signals, regime, self._portfolio(), perf_scores={})
        # gross = |btc| + |eth|, net = btc + eth
        assert tp.gross_exposure > 0
        assert tp.gross_exposure >= abs(tp.net_exposure)

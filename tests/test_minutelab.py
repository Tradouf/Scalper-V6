"""Tests MinuteLab : indicateurs, backtester (sortie PnL/MA), sélecteur."""

import math
import unittest

from minutelab import config
from minutelab.backtester import LabResult, run_lab_backtest
from minutelab.champion import (
    ChampionState,
    count_near_misses,
    pick_champion,
    qualifies,
)
from minutelab.indicators import atr, ema, rsi, sma, stochastic, supertrend
from minutelab.selector import select
from minutelab.strategies import Strat, build_grid, compute_signals


def make_candles(closes, spread=0.5):
    """Bougies synthétiques 1 m à partir d'une liste de closes."""
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append({
            "ts": 1_700_000_000_000 + i * 60_000,
            "open": o,
            "high": max(o, c) + spread,
            "low": min(o, c) - spread,
            "close": c,
            "volume": 1.0,
        })
        prev = c
    return out


class TestIndicators(unittest.TestCase):
    def test_sma_ema_align(self):
        vals = [float(i) for i in range(1, 21)]
        s = sma(vals, 5)
        self.assertIsNone(s[3])
        self.assertAlmostEqual(s[4], 3.0)
        self.assertAlmostEqual(s[19], 18.0)
        e = ema(vals, 5)
        self.assertIsNone(e[3])
        self.assertAlmostEqual(e[4], 3.0)
        # EMA d'une suite croissante reste sous la dernière valeur
        self.assertLess(e[19], 20.0)

    def test_rsi_extremes(self):
        up = [100.0 + i for i in range(30)]
        r = rsi(up, 14)
        self.assertIsNone(r[13])
        self.assertAlmostEqual(r[-1], 100.0)
        down = [100.0 - i for i in range(30)]
        self.assertAlmostEqual(rsi(down, 14)[-1], 0.0)

    def test_stochastic_bounds(self):
        candles = make_candles([100 + math.sin(i / 3) * 5 for i in range(50)])
        k, d = stochastic(candles, 14, 3)
        for v in k + d:
            if v is not None:
                self.assertGreaterEqual(v, 0.0)
                self.assertLessEqual(v, 100.0)

    def test_atr_positive(self):
        candles = make_candles([100 + math.sin(i / 5) * 3 for i in range(40)])
        a = atr(candles, 14)
        self.assertIsNone(a[13])
        self.assertGreater(a[-1], 0.0)

    def test_supertrend_follows_trend(self):
        closes = [100.0 + i * 2 for i in range(40)] + [180.0 - i * 2 for i in range(40)]
        st = supertrend(make_candles(closes), 10, 2.0)
        self.assertEqual(st[35], 1)     # montée franche → haussier
        self.assertEqual(st[-1], -1)    # descente franche → baissier


class TestSignals(unittest.TestCase):
    def test_ema_cross_signal(self):
        # Plat puis montée franche → un cross haussier quelque part
        closes = [100.0] * 30 + [100.0 + i * 1.5 for i in range(1, 31)]
        sig = compute_signals(make_candles(closes), Strat("ema_cross", (3, 10)))
        self.assertIn(1, sig)

    def test_filter_blocks_countertrend(self):
        # Descente continue : un éventuel signal long rsi_revert doit être
        # bloqué par le filtre supertrend baissier.
        closes = [200.0 - i * 1.2 for i in range(80)]
        candles = make_candles(closes)
        s = Strat("rsi_revert", (7, 30, 70), "supertrend", (10, 3.0))
        sig = compute_signals(candles, s)
        self.assertNotIn(1, sig)

    def test_grid_builds(self):
        grid = build_grid()
        self.assertGreater(len(grid), 100)
        self.assertEqual(len(set(grid)), len(grid))  # pas de doublon


class TestBacktester(unittest.TestCase):
    def test_pnl_ma_exit_on_reversal(self):
        # Montée (le gain monte) puis retournement : la sortie PNL_MA doit
        # se déclencher, pas MAX_HOLD.
        closes = ([100.0] * 40
                  + [100.0 + i * 1.0 for i in range(1, 16)]
                  + [115.0 - i * 1.0 for i in range(1, 11)])
        candles = make_candles(closes, spread=0.01)
        s = Strat("ema_cross", (3, 10), exit_ma_bars=3)
        r = run_lab_backtest(candles, s, fee_pct=0.0, slippage_pct=0.0,
                             start_index=0, recent_index=0,
                             hard_sl_pct=0.5, max_hold_bars=500)
        self.assertGreaterEqual(r.n_trades, 1)
        self.assertIn(r.trades[0]["reason"], ("PNL_MA",))
        self.assertGreater(r.trades[0]["pnl_pct"], 0.0)

    def test_exit_gate_blocks_cross_below_min_gain(self):
        # Même scénario que test_pnl_ma_exit_on_reversal, mais avec un seuil
        # de gain inatteignable : le croisement PnL/MA ne doit PAS sortir,
        # seuls les garde-fous (MAX_HOLD/EOW/HARD_SL) coupent.
        closes = ([100.0] * 40
                  + [100.0 + i * 1.0 for i in range(1, 16)]
                  + [115.0 - i * 1.0 for i in range(1, 11)])
        candles = make_candles(closes, spread=0.01)
        s = Strat("ema_cross", (3, 10), exit_ma_bars=3)
        blocked = run_lab_backtest(candles, s, 0.0, 0.0, 0, 0,
                                   hard_sl_pct=0.5, max_hold_bars=500,
                                   exit_min_gain=10.0)
        self.assertGreaterEqual(blocked.n_trades, 1)
        self.assertNotIn("PNL_MA", [t["reason"] for t in blocked.trades])
        # Seuil bas (sous le gain au croisement) : comportement inchangé,
        # et le gain à la sortie couvre bien le seuil.
        passed = run_lab_backtest(candles, s, 0.0, 0.0, 0, 0,
                                  hard_sl_pct=0.5, max_hold_bars=500,
                                  exit_min_gain=0.0015)
        self.assertEqual(passed.trades[0]["reason"], "PNL_MA")
        self.assertGreater(passed.trades[0]["pnl_pct"], 0.0015)

    def test_costs_reduce_pnl(self):
        closes = [100.0] * 40 + [100.0 + i for i in range(1, 20)]
        candles = make_candles(closes, spread=0.01)
        s = Strat("ema_cross", (3, 10))
        free = run_lab_backtest(candles, s, 0.0, 0.0, 0, 0)
        paid = run_lab_backtest(candles, s, 0.00045, 0.0003, 0, 0)
        self.assertEqual(free.n_trades, paid.n_trades)
        if free.n_trades:
            self.assertAlmostEqual(free.pnl_pct - paid.pnl_pct,
                                   free.n_trades * 2 * 0.00075, places=9)

    def test_hard_sl(self):
        # Montée qui déclenche l'entrée puis krach : stop dur touché.
        closes = [100.0] * 40 + [101.0, 102.0, 103.0] + [80.0] * 5
        candles = make_candles(closes, spread=0.01)
        s = Strat("ema_cross", (3, 10), exit_ma_bars=5)
        r = run_lab_backtest(candles, s, 0.0, 0.0, 0, 0,
                             hard_sl_pct=0.004, max_hold_bars=500)
        self.assertTrue(any(t["reason"] == "HARD_SL" for t in r.trades))

    def test_start_index_gates_entries(self):
        closes = [100.0] * 30 + [100.0 + i * 1.5 for i in range(1, 31)]
        candles = make_candles(closes)
        s = Strat("ema_cross", (3, 10))
        r = run_lab_backtest(candles, s, 0.0, 0.0,
                             start_index=len(candles) - 1, recent_index=0)
        self.assertEqual(r.n_trades, 0)


class TestSelector(unittest.TestCase):
    def test_short_history_returns_flat(self):
        res = select(make_candles([100.0] * 50))
        self.assertIsNone(res["candidate"])

    def test_selector_runs_on_synthetic(self):
        # Sinusoïde ample : au moins le scan tourne et classe sans planter.
        closes = [30000 * (1 + 0.01 * math.sin(i / 8)) for i in range(400)]
        res = select(make_candles(closes, spread=1.0))
        self.assertGreater(res["scanned"], 100)
        if res["candidate"]:
            self.assertGreaterEqual(res["candidate"].n_trades, config.MIN_TRADES)
            self.assertGreaterEqual(res["candidate"].score, config.MIN_SCORE_PCT)


class TestQualification(unittest.TestCase):
    def _result(self, strat, n_trades=3, pnl=0.002, recent=0.002, pf=1.5):
        return LabResult(
            strat=strat, n_trades=n_trades, pnl_pct=pnl,
            pnl_recent_pct=recent, winrate=0.5, profit_factor=pf,
        )

    def test_pulse_mode_single_window(self):
        s = Strat("ema_cross", (3, 10))
        r = self._result(s, recent=config.MIN_PNL_RECENT_PCT + 0.0001,
                         pnl=config.MIN_PNL_RECENT_PCT + 0.0001)
        self.assertTrue(qualifies(r, 2, 20, 20))

    def test_min_edge_rejects_marginal(self):
        s = Strat("ema_cross", (3, 10))
        r = self._result(s, recent=0.0001, pnl=0.0001, pf=2.0)
        self.assertFalse(qualifies(r, 2, 20, 20))

    def test_legacy_mode_unchanged(self):
        old = config.QUAL_MODE
        try:
            config.QUAL_MODE = "legacy"
            s = Strat("ema_cross", (3, 10))
            ok = self._result(s, pnl=0.001, recent=0.001)
            bad = self._result(s, pnl=0.001, recent=-0.001)
            self.assertTrue(qualifies(ok, 2, 60, 20))
            self.assertFalse(qualifies(bad, 2, 60, 20))
        finally:
            config.QUAL_MODE = old

    def test_near_miss_count(self):
        s = Strat("ema_cross", (3, 10))
        ranked = [
            self._result(s, recent=0.0001, pnl=0.002, pf=2.0),
            self._result(Strat("sma_cross", (5, 20)), recent=-0.001, pnl=0.002),
        ]
        n = count_near_misses(ranked, 2, 20, 20, top_n=2)
        self.assertEqual(n, 1)


class TestChampionHysteresis(unittest.TestCase):
    def _lab(self, strat_spec, recent=0.002, pnl=None):
        s = (Strat(*strat_spec) if isinstance(strat_spec, tuple) else strat_spec)
        p = recent if pnl is None else pnl
        return LabResult(strat=s, n_trades=3, pnl_pct=p, pnl_recent_pct=recent,
                         winrate=0.6, profit_factor=1.5)

    def test_grace_period_holds_incumbent(self):
        inc = Strat("ema_cross", (3, 10))
        state = ChampionState(strat=inc, since=1000.0, entry_equity=0.0)
        strat, reason, new_state = pick_champion(
            None, [], [], state, equity_pct=0.0, now=1100.0)
        self.assertEqual(strat, inc)
        self.assertIn("GRACE_HOLD", reason)
        self.assertEqual(new_state.grace_misses, 1)

    def test_grace_expires_to_flat(self):
        inc = Strat("ema_cross", (3, 10))
        state = ChampionState(strat=inc, since=1000.0, entry_equity=0.0, grace_misses=1)
        strat, reason, _ = pick_champion(
            None, [], [], state, equity_pct=0.0, now=2000.0)
        self.assertIsNone(strat)
        self.assertEqual(reason, "GRACE_EXPIRED")

    def test_tenure_blocks_switch(self):
        inc = Strat("ema_cross", (3, 10))
        cand = self._lab(("sma_cross", (5, 20)), recent=0.01, pnl=0.0)
        state = ChampionState(strat=inc, since=1000.0, entry_equity=0.0)
        ranked = [cand, self._lab(inc, recent=0.001, pnl=0.0)]
        strat, reason, _ = pick_champion(
            cand, [cand], ranked, state, equity_pct=0.0, now=1300.0)
        self.assertEqual(strat, inc)
        self.assertEqual(reason, "TENURE_HOLD")

    def test_score_margin_blocks_small_gain(self):
        inc = Strat("ema_cross", (3, 10))
        cand = self._lab(("sma_cross", (5, 20)), recent=0.001, pnl=0.0008)
        state = ChampionState(strat=inc, since=1000.0, entry_equity=0.0)
        ranked = [cand, self._lab(inc, recent=0.001, pnl=0.0004)]
        strat, reason, _ = pick_champion(
            cand, [cand], ranked, state, equity_pct=0.0,
            now=1000.0 + config.CHAMPION_MIN_TENURE_MIN * 60 + 1)
        self.assertEqual(strat, inc)
        self.assertEqual(reason, "SCORE_MARGIN")

    def test_demote_on_bad_live_pnl(self):
        inc = Strat("ema_cross", (3, 10))
        cand = self._lab(("sma_cross", (5, 20)), recent=0.01)
        state = ChampionState(strat=inc, since=1000.0, entry_equity=0.0)
        strat, reason, _ = pick_champion(
            cand, [cand], [cand], state,
            equity_pct=config.CHAMPION_DEMOTE_PNL_PCT - 0.001, now=1100.0)
        self.assertEqual(strat, cand.strat)
        self.assertEqual(reason, "DEMOTE_BAD_LIVE")

    def test_incumbent_ok_when_still_qualified(self):
        inc = Strat("ema_cross", (3, 10))
        cand = self._lab(inc, recent=0.002)
        other = self._lab(("sma_cross", (5, 20)), recent=0.01)
        state = ChampionState(strat=inc, since=1000.0, entry_equity=0.0)
        qualified = [other, cand]
        strat, reason, _ = pick_champion(
            other, qualified, qualified, state, equity_pct=0.0,
            now=1000.0 + config.CHAMPION_MIN_TENURE_MIN * 60 + 1)
        self.assertEqual(strat, inc)
        self.assertEqual(reason, "INCUMBENT_OK")

    def test_new_champion_when_flat(self):
        cand = self._lab(("ema_cross", (3, 10)), recent=0.002)
        state = ChampionState()
        strat, reason, new_state = pick_champion(
            cand, [cand], [cand], state, equity_pct=0.0, now=1000.0)
        self.assertEqual(strat, cand.strat)
        self.assertEqual(reason, "NEW_CHAMPION")
        self.assertEqual(new_state.since, 1000.0)


if __name__ == "__main__":
    unittest.main()

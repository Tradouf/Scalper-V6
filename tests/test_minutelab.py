"""Tests MinuteLab : indicateurs, backtester (sortie PnL/MA), sélecteur."""

import math
import unittest

from minutelab.backtester import run_lab_backtest
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
        self.assertIsNone(res["champion"])

    def test_selector_runs_on_synthetic(self):
        # Sinusoïde ample : au moins le scan tourne et classe sans planter.
        closes = [30000 * (1 + 0.01 * math.sin(i / 8)) for i in range(400)]
        res = select(make_candles(closes, spread=1.0))
        self.assertGreater(res["scanned"], 100)
        # Un champion éventuel doit respecter les critères de qualification
        if res["champion"]:
            self.assertGreater(res["champion"].pnl_pct, 0)
            self.assertGreater(res["champion"].pnl_recent_pct, 0)
            self.assertGreaterEqual(res["champion"].n_trades, 2)


if __name__ == "__main__":
    unittest.main()

"""Tests funding fade à seuil + carry."""
from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.funding_strategy import _fade_symbol, funding_fade_backtest


def test_carry_credited_on_short_high_funding_flat_price():
    # Prix PLAT, funding très positif constant → SHORT (fade) doit gagner via le
    # carry (on reçoit le funding), pas via le prix.
    n = 400
    px = np.full(n, 100.0)
    f_ann = 0.50               # 50%/an
    f_hourly = np.full(n, f_ann / (24 * 365))
    trades = _fade_symbol(px, f_hourly, entry_thr=0.30, exit_thr=0.10,
                          max_hold=168, fee_pct=0.0)
    assert trades and trades[0] > 0   # carry positif, prix neutre


def test_long_side_on_negative_funding():
    n = 300
    px = np.full(n, 50.0)
    f_hourly = np.full(n, -0.40 / (24 * 365))   # funding négatif → LONG (fade)
    trades = _fade_symbol(px, f_hourly, entry_thr=0.30, exit_thr=0.10,
                          max_hold=168, fee_pct=0.0)
    assert trades and trades[0] > 0   # LONG reçoit le funding quand f<0


def test_no_trade_below_threshold():
    n = 300
    px = np.full(n, 100.0)
    f_hourly = np.full(n, 0.10 / (24 * 365))    # 10%/an < entry 30% → jamais d'entrée
    trades = _fade_symbol(px, f_hourly, entry_thr=0.30, exit_thr=0.10,
                          max_hold=168, fee_pct=0.0)
    assert trades == []


def test_fees_reduce_pnl_pooled():
    rng = np.random.default_rng(0)
    n, syms = 500, ["A", "B", "C"]
    closes = pd.DataFrame({s: 100 * np.exp(np.cumsum(rng.normal(0, 0.002, n))) for s in syms})
    f = pd.DataFrame({s: np.full(n, 0.4 / (24 * 365)) for s in syms})
    gross = funding_fade_backtest(closes, f, entry_thr=0.3, exit_thr=0.1, max_hold=100, fee_pct=0.0)
    net = funding_fade_backtest(closes, f, entry_thr=0.3, exit_thr=0.1, max_hold=100, fee_pct=0.001)
    assert net.nb_trades == gross.nb_trades and net.total_pnl < gross.total_pnl

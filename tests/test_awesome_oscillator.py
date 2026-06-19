"""Tests Awesome Oscillator : indicateur + règles d'entrée + sortie TP."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from core.config import AwesomeOscillatorStrategyConfig
from core.types import Candle, Fill, MarketSnapshot
from strategies.awesome_oscillator import AwesomeOscillatorStrategy, compute_ao


NOW = dt.datetime(2026, 6, 18, 12, 0, 0)


def _candles(ohlc: list[tuple[float, float, float, float]], symbol: str = "BTC") -> list[Candle]:
    n = len(ohlc)
    return [
        Candle(
            ts_open=NOW - dt.timedelta(minutes=5 * (n - i)),
            open=o, high=h, low=l, close=c, volume=1.0,
        )
        for i, (o, h, l, c) in enumerate(ohlc)
    ]


def _market(candles, mark, symbol="BTC") -> MarketSnapshot:
    return MarketSnapshot(timestamp=NOW, candles={symbol: candles}, prices={symbol: mark})


# ─── Indicateur ───────────────────────────────────────────────────────────────


def test_compute_ao_matches_definition():
    rng = np.random.default_rng(0)
    n = 60
    highs = np.cumsum(rng.normal(0, 1, n)) + 100
    lows = highs - 2.0
    ao = compute_ao(highs, lows, fast=5, slow=34)
    assert ao is not None
    # Avant slow-1 : NaN ; à partir de slow-1 : défini.
    assert np.all(np.isnan(ao[:33]))
    assert np.all(np.isfinite(ao[33:]))
    # Vérif manuelle sur le dernier indice.
    median = (highs + lows) / 2.0
    expected = median[-5:].mean() - median[-34:].mean()
    assert ao[-1] == pytest.approx(expected, rel=1e-9)


def test_compute_ao_insufficient():
    assert compute_ao(np.arange(10.0), np.arange(10.0), 5, 34) is None


# ─── Entrées ──────────────────────────────────────────────────────────────────


def _cfg(**kw):
    base = dict(enabled=True, fast=5, slow=34, x_long=65.0, x_short=60.0,
                notional_usdc=30.0, tp_pct=0.012)
    base.update(kw)
    return AwesomeOscillatorStrategyConfig(**base)


def _downtrend_then_green_bar():
    """Tendance baissière régulière (AO profondément négatif et décroissant) suivie
    d'une bougie VERTE de rebond intrabar à un niveau PLUS BAS (AO reste rouge) en
    position clôturée (-2), puis une bougie en formation (-1, ignorée)."""
    ohlc = []
    price = 1000.0
    for _ in range(44):
        # Bougie baissière : median décroît régulièrement → AO se creuse.
        ohlc.append((price, price + 1, price - 9, price - 8))
        price -= 8.0
    # Bougie clôturée (-2) : VERTE (close > open) mais à un niveau PLUS BAS (gap
    # down, median sous la précédente → AO continue de décroître = rouge). C'est
    # le setup de reversal visé : momentum encore baissier, prix qui rebondit.
    open_ = price - 12
    close = price - 10          # close > open → verte
    ohlc.append((open_, price - 8, price - 16, close))  # median = price-12 < précédente
    # Bougie en formation (-1, ignorée par la stratégie).
    ohlc.append((close, close + 1, close - 1, close))
    return ohlc, close


def test_long_entry_when_ao_deep_negative_and_green_candle():
    ohlc, mark = _downtrend_then_green_bar()
    strat = AwesomeOscillatorStrategy(_cfg(), symbols=["BTC"])
    sigs = strat.generate_signals(_market(_candles(ohlc), mark=mark))
    assert len(sigs) == 1
    assert sigs[0].direction == 1.0
    assert sigs[0].target_notional == 30.0
    assert sigs[0].stop_price is None  # TP seul


def test_no_entry_when_threshold_not_reached():
    # Marché plat → AO ≈ 0, jamais sous -x_long.
    ohlc = [(1000.0, 1001.0, 999.0, 1000.0)] * 40
    strat = AwesomeOscillatorStrategy(_cfg(), symbols=["BTC"])
    sigs = strat.generate_signals(_market(_candles(ohlc), mark=1000.0))
    assert sigs == []


def test_anti_rafale_one_entry_per_bar():
    ohlc, mark = _downtrend_then_green_bar()
    strat = AwesomeOscillatorStrategy(_cfg(), symbols=["BTC"])
    m = _market(_candles(ohlc), mark=mark)
    first = strat.generate_signals(m)
    assert len(first) == 1
    # Même barre clôturée → pas de seconde entrée (anti-rafale).
    second = strat.generate_signals(m)
    assert second == []


# ─── Sortie TP ────────────────────────────────────────────────────────────────


def test_tp_exit_emits_close():
    strat = AwesomeOscillatorStrategy(_cfg(tp_pct=0.01), symbols=["BTC"])
    # Simule une position LONG ouverte à 1000 via on_fill.
    strat.on_fill(Fill(order_id="1", asset="BTC", notional=30.0, price=1000.0,
                       fee=0.0, strategy_id="awesome_oscillator", timestamp=NOW))
    assert "BTC" in strat.open_positions()
    # Donne-lui un intent (maintain) pour que le HOLD soit piloté.
    strat._intent["BTC"] = {"direction": 1.0, "target_notional": 30.0, "confidence": 0.8}
    ohlc = [(1000.0, 1001.0, 999.0, 1000.0)] * 40

    # Mark sous le TP → maintain (pas de close).
    hold = strat.generate_signals(_market(_candles(ohlc), mark=1005.0))
    assert len(hold) == 1 and hold[0].target_notional == 30.0

    # Mark au-dessus du TP (1010 = +1%) → close.
    close = strat.generate_signals(_market(_candles(ohlc), mark=1010.0))
    assert len(close) == 1
    assert close[0].direction == 0.0
    assert close[0].target_notional == 0.0


def test_sl_exit_emits_close():
    # TP=2%, SL=1% (ratio 2). Position LONG à 1000, mark à 989 (−1.1%) → SL → close.
    strat = AwesomeOscillatorStrategy(_cfg(tp_pct=0.02, sl_pct=0.01), symbols=["BTC"])
    strat.on_fill(Fill(order_id="1", asset="BTC", notional=30.0, price=1000.0,
                       fee=0.0, strategy_id="awesome_oscillator", timestamp=NOW))
    strat._intent["BTC"] = {"direction": 1.0, "target_notional": 30.0, "confidence": 0.8}
    ohlc = [(1000.0, 1001.0, 999.0, 1000.0)] * 40
    # Mark à 995 (−0.5%) : ni TP ni SL → maintain.
    hold = strat.generate_signals(_market(_candles(ohlc), mark=995.0))
    assert len(hold) == 1 and hold[0].target_notional == 30.0
    # Mark à 989 (−1.1% < −1% SL) → close.
    close = strat.generate_signals(_market(_candles(ohlc), mark=989.0))
    assert len(close) == 1 and close[0].direction == 0.0 and close[0].target_notional == 0.0


def test_sl_disabled_when_zero():
    # sl_pct=0 → pas de SL : même un mark très bas reste en maintain (TP seul).
    strat = AwesomeOscillatorStrategy(_cfg(tp_pct=0.02, sl_pct=0.0), symbols=["BTC"])
    strat.on_fill(Fill(order_id="1", asset="BTC", notional=30.0, price=1000.0,
                       fee=0.0, strategy_id="awesome_oscillator", timestamp=NOW))
    strat._intent["BTC"] = {"direction": 1.0, "target_notional": 30.0, "confidence": 0.8}
    ohlc = [(1000.0, 1001.0, 999.0, 1000.0)] * 40
    hold = strat.generate_signals(_market(_candles(ohlc), mark=900.0))  # −10%
    assert len(hold) == 1 and hold[0].target_notional == 30.0  # maintenu (pas de SL)


def test_sync_positions_purges_closed():
    strat = AwesomeOscillatorStrategy(_cfg(), symbols=["BTC"])
    strat.on_fill(Fill(order_id="1", asset="BTC", notional=30.0, price=1000.0,
                       fee=0.0, strategy_id="awesome_oscillator", timestamp=NOW))
    assert "BTC" in strat.open_positions()
    strat.sync_positions({"BTC": 0.0})  # fermée hors stratégie
    assert "BTC" not in strat.open_positions()

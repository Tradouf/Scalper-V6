"""Tests RSI-MR paper — fetch injecté, état isolé, aucun réseau."""

import json
import math
import time

import pytest

from rsimr.paper import (FEE_SIDE, H_BARS, HOUR_MS, MIN_BARS, NOTIONAL,
                         RSIMRPaperTrader, SYMBOLS, rsi_series)

BASE_PX = 100.0


def make_candles(n, end_ms, closes=None):
    """n bougies 1h clôturées se terminant à end_ms (ts de la dernière =
    end_ms - 1h). closes optionnel pour piloter la fin de série."""
    out = []
    for i in range(n):
        ts = end_ms - (n - i) * HOUR_MS
        px = BASE_PX if closes is None else closes[i]
        out.append({"ts": ts, "open": px, "high": px, "low": px,
                    "close": px, "volume": 1.0})
    return out


def flat_then_crash_recover(n, crash_at_from_end=6):
    """Série qui force un RSI en survente puis un croisement 30↑ sur la
    dernière bougie : longue baisse régulière, puis un rebond final."""
    closes = []
    px = BASE_PX
    for i in range(n):
        remaining = n - i
        if remaining > crash_at_from_end:
            px *= 1.0 + (0.0001 if i % 3 else -0.0001)   # plat bruité
        elif remaining > 1:
            px *= 0.98                                    # chute → RSI < 30
        else:
            px *= 1.05                                    # rebond → croise 30↑
        closes.append(px)
    return closes


@pytest.fixture()
def trader(tmp_path):
    calls = {}

    def fake_fetch(sym, interval, days, **kw):
        assert interval == "1h"
        return calls.get(sym, [])

    t = RSIMRPaperTrader(fetch=fake_fetch,
                         state_file=tmp_path / "state.json")
    return t, calls


def test_rsi_series_matches_reference():
    # série descendante : RSI doit finir bas ; montante : haut
    down = [100 * (0.99 ** i) for i in range(50)]
    up = [100 * (1.01 ** i) for i in range(50)]
    assert rsi_series(down)[-1] < 10
    assert rsi_series(up)[-1] > 90
    flat = [100.0] * 50
    assert all(abs(x - 50) < 1e-9 or x == 50.0 for x in rsi_series(flat))


def test_first_sweep_bootstraps_without_opening(trader):
    t, calls = trader
    now = (int(time.time()) // 3600) * 3600
    closes = flat_then_crash_recover(MIN_BARS + 60)
    calls["BTC"] = make_candles(MIN_BARS + 60, now * 1000, closes)
    t.sweep_if_due(now + 130)
    # signal présent sur la dernière bougie, mais premier passage = amorçage
    assert t.state["open"] == []
    assert "BTC" in t.state["last_seen"]


def test_signal_opens_then_closes_with_fees(trader):
    t, calls = trader
    hour0 = (int(time.time()) // 3600) * 3600
    n = MIN_BARS + 60

    # sweep 1 : série plate → amorçage sans signal
    flat = [BASE_PX] * n
    calls["BTC"] = make_candles(n, hour0 * 1000, flat)
    t.sweep_if_due(hour0 + 130)
    assert t.state["open"] == []

    # sweep 2 (une heure plus tard) : le croisement 30↑ arrive sur la nouvelle bougie
    closes = flat_then_crash_recover(n)
    candles = make_candles(n, (hour0 + 3600) * 1000, closes)
    calls["BTC"] = candles
    t.sweep_if_due(hour0 + 3600 + 130)
    assert len(t.state["open"]) == 1
    pos = t.state["open"][0]
    assert pos["sym"] == "BTC"
    entry_px = candles[-1]["close"]
    assert pos["entry_px"] == pytest.approx(entry_px)
    assert pos["exit_ts"] == pos["entry_ts"] + H_BARS * HOUR_MS

    # avance de H_BARS heures : la bougie de sortie clôture, +2 % de hausse
    exit_px = entry_px * 1.02
    extra = [{"ts": candles[-1]["ts"] + (i + 1) * HOUR_MS,
              "open": exit_px, "high": exit_px, "low": exit_px,
              "close": exit_px, "volume": 1.0} for i in range(H_BARS)]
    calls["BTC"] = (candles + extra)[H_BARS:]
    t_close = hour0 + 3600 * (1 + H_BARS) + 130
    t.sweep_if_due(t_close)
    assert t.state["open"] == []
    assert t.state["n_trades"] == 1
    gross = (exit_px - entry_px) / entry_px
    net = gross - 2 * FEE_SIDE
    assert t.state["realized_usd"] == pytest.approx(NOTIONAL * net)
    assert t.state["n_wins"] == 1
    # trade journalisé
    lines = (t.trades_file).read_text().strip().splitlines()
    rec = json.loads(lines[-1])
    assert rec["sym"] == "BTC"
    assert rec["net_bps"] == pytest.approx(1e4 * net, abs=0.01)


def test_no_duplicate_signals_across_sweeps(trader):
    t, calls = trader
    hour0 = (int(time.time()) // 3600) * 3600
    n = MIN_BARS + 60
    flat = [BASE_PX] * n
    calls["BTC"] = make_candles(n, hour0 * 1000, flat)
    t.sweep_if_due(hour0 + 130)

    closes = flat_then_crash_recover(n)
    candles = make_candles(n, (hour0 + 3600) * 1000, closes)
    calls["BTC"] = candles
    t.sweep_if_due(hour0 + 3600 + 130)
    assert len(t.state["open"]) == 1

    # re-sweep de la même heure (redémarrage) : refusé par last_sweep_hour
    assert t.sweep_if_due(hour0 + 3600 + 200) is False
    # heure suivante avec les mêmes bougies signal déjà vues : pas de doublon
    calls["BTC"] = candles  # mêmes données (bougie suivante pas encore là)
    t.state["last_sweep_hour"] = None
    t.sweep_if_due(hour0 + 2 * 3600 + 130)
    assert len(t.state["open"]) == 1


def test_state_persists_across_instances(trader, tmp_path):
    t, calls = trader
    hour0 = (int(time.time()) // 3600) * 3600
    n = MIN_BARS + 60
    calls["ETH"] = make_candles(n, hour0 * 1000, flat_then_crash_recover(n))
    t.sweep_if_due(hour0 + 130)
    t2 = RSIMRPaperTrader(fetch=lambda *a, **k: [],
                          state_file=tmp_path / "state.json")
    assert "ETH" in t2.state["last_seen"]


def test_universe_is_frozen_48():
    assert len(SYMBOLS) == 48
    assert len(set(SYMBOLS)) == 48
    for m in ("BTC", "ETH", "SOL"):
        assert m in SYMBOLS

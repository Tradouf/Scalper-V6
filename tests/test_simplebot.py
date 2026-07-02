"""
Tests SimpleBot — stratégie, backtester, optimiseur, live (dry-run).
Aucun accès réseau : bougies synthétiques uniquement.

    python -m pytest tests/test_simplebot.py -v
"""

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simplebot import config
from simplebot.backtester import run_backtest
from simplebot.data import closed_candles
from simplebot.optimizer import BacktestOptimizerAgent
from simplebot.strategy import (
    StrategyParams,
    compute_signals,
    ema,
    latest_signal,
    param_grid,
)

PARAMS = StrategyParams(ema_fast=9, ema_slow=26, tp_atr=2.5, sl_atr=1.5)


def make_candles(closes, ts0=1_700_000_000_000, interval_ms=900_000, spread=0.5):
    """Bougies synthétiques autour d'une série de closes."""
    candles = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        candles.append({
            "ts": ts0 + i * interval_ms,
            "open": o,
            "high": max(o, c) + spread,
            "low": min(o, c) - spread,
            "close": c,
            "volume": 100.0,
        })
        prev = c
    return candles


def wave_closes(n=400, base=100.0, amplitude=10.0, period=80):
    """Série cyclique : alternance de tendances haussières et baissières."""
    return [base + amplitude * math.sin(2 * math.pi * i / period) for i in range(n)]


def vshape_closes(n=450, turn=220, down=0.30, up=0.40):
    """
    Série en V : baisse, retournement au bar `turn`, hausse — avec un bruit
    déterministe qui garde le RSI loin des extrêmes. Calibrée pour produire
    exactement UN cross haussier (bar ~233 avec les PARAMS de test).
    """
    out = []
    for i in range(n):
        base = 100 + (-down * i if i < turn else -down * turn + up * (i - turn))
        noise = 1.2 * math.sin(i / 2.9) + 0.8 * math.sin(i / 6.1)
        out.append(base + noise)
    return out


# ── Stratégie ────────────────────────────────────────────────────────────────

def test_ema_converges_to_constant():
    assert ema([5.0] * 100, 10)[-1] == pytest.approx(5.0)


def test_signals_detect_cross():
    # V : baisse puis retournement haussier → exactement un signal long, après le turn
    signals = compute_signals(make_candles(vshape_closes()), PARAMS)
    longs = [i for i, s in enumerate(signals) if s == 1]
    shorts = [i for i, s in enumerate(signals) if s == -1]
    assert len(longs) == 1
    assert longs[0] > 220
    assert not shorts


def test_signals_symmetric_short():
    # miroir du V → exactement un signal short
    closes = [200.0 - c for c in vshape_closes()]
    signals = compute_signals(make_candles(closes), PARAMS)
    assert any(s == -1 for s in signals)
    assert not any(s == 1 for s in signals)


def test_no_signal_during_warmup():
    closes = wave_closes(n=PARAMS.warmup_bars)
    signals = compute_signals(make_candles(closes), PARAMS)
    assert all(s == 0 for s in signals)


def test_latest_signal_shape():
    candles = make_candles(wave_closes())
    sig = latest_signal(candles, PARAMS)
    assert set(sig) == {"signal", "atr", "close", "ts"}
    assert sig["atr"] > 0
    assert sig["ts"] == candles[-1]["ts"]


def test_param_grid_valid():
    grid = param_grid()
    assert len(grid) > 20
    assert all(p.ema_slow >= p.ema_fast * 2 for p in grid)


# ── Backtester ───────────────────────────────────────────────────────────────

def test_backtest_generates_trades_and_metrics():
    candles = make_candles(wave_closes(n=600))
    result = run_backtest(candles, PARAMS, fee_pct=0.00045, slippage_pct=0.0003)
    assert result.n_trades >= 2
    assert 0.0 <= result.winrate <= 1.0
    assert result.max_drawdown_pct >= 0.0
    # cohérence PnL total = somme des trades
    assert result.total_pnl_pct == pytest.approx(sum(t["pnl_pct"] for t in result.trades))


def test_backtest_entry_at_next_open():
    candles = make_candles(wave_closes(n=600))
    result = run_backtest(candles, PARAMS, fee_pct=0.0, slippage_pct=0.0)
    for t in result.trades:
        assert t["exit_bar"] >= t["entry_bar"]
        assert t["entry"] == candles[t["entry_bar"]]["open"]


def test_backtest_costs_reduce_pnl():
    candles = make_candles(wave_closes(n=600))
    free = run_backtest(candles, PARAMS, fee_pct=0.0, slippage_pct=0.0)
    paid = run_backtest(candles, PARAMS, fee_pct=0.00045, slippage_pct=0.0003)
    assert paid.total_pnl_pct < free.total_pnl_pct


def test_backtest_start_index_filters_trades():
    candles = make_candles(wave_closes(n=600))
    full = run_backtest(candles, PARAMS, 0.0, 0.0)
    late = run_backtest(candles, PARAMS, 0.0, 0.0, start_index=400)
    assert late.n_trades < full.n_trades
    assert all(t["entry_bar"] > 400 for t in late.trades)


def test_backtest_sl_pessimistic_when_both_touchable():
    # Bougie géante après l'entrée : high > TP et low < SL → le SL doit primer
    candles = make_candles(vshape_closes())
    signals = compute_signals(candles, PARAMS)
    sig_bar = next(i for i, s in enumerate(signals) if s == 1)
    big = candles[sig_bar + 2]
    big["high"] = 200.0
    big["low"] = 1.0
    result = run_backtest(candles, PARAMS, 0.0, 0.0)
    first = result.trades[0]
    assert first["reason"] == "SL"
    assert first["pnl_pct"] < 0


# ── Optimiseur ───────────────────────────────────────────────────────────────

def _fake_fetch_factory(closes):
    def fake_fetch(symbol, interval, days, **kwargs):
        return make_candles(closes)
    return fake_fetch


def test_optimizer_publishes_best_params(tmp_path):
    state_file = tmp_path / "best_params.json"
    # marché cyclique lisible → au moins un set devrait confirmer en validation
    agent = BacktestOptimizerAgent(
        symbols=["TEST"],
        fetch=_fake_fetch_factory(wave_closes(n=1200, period=100)),
        state_file=state_file,
    )
    state = agent.run_once()
    assert state_file.exists()
    on_disk = json.loads(state_file.read_text())
    assert on_disk["symbols"].keys() == {"TEST"}
    entry = on_disk["symbols"]["TEST"]
    if entry["active"]:
        p = StrategyParams.from_dict(entry["params"])
        assert p in param_grid()
        assert entry["valid"]["profit_factor"] >= config.MIN_VALID_PF
        assert entry["valid"]["total_pnl_pct"] > 0
    else:
        assert "reason" in entry


def test_optimizer_inactive_on_insufficient_data(tmp_path):
    agent = BacktestOptimizerAgent(
        symbols=["TEST"],
        fetch=_fake_fetch_factory([100.0] * 50),
        state_file=tmp_path / "best_params.json",
    )
    state = agent.run_once()
    assert state["symbols"]["TEST"]["active"] is False


def test_optimizer_inactive_on_flat_market(tmp_path):
    # marché plat : aucun trade → aucun set ne doit être publié actif
    agent = BacktestOptimizerAgent(
        symbols=["TEST"],
        fetch=_fake_fetch_factory([100.0] * 1200),
        state_file=tmp_path / "best_params.json",
    )
    state = agent.run_once()
    assert state["symbols"]["TEST"]["active"] is False


# ── Données ──────────────────────────────────────────────────────────────────

def test_closed_candles_drops_running_candle():
    interval_ms = 900_000
    candles = make_candles([100.0] * 10, ts0=0, interval_ms=interval_ms)
    now_ms = candles[-1]["ts"] + 1  # dernière bougie encore en cours
    closed = closed_candles(candles, interval_ms, now_ms=now_ms)
    assert len(closed) == 9


# ── Live (dry-run) ───────────────────────────────────────────────────────────

def test_live_trader_dry_run_acts_once_per_candle(tmp_path, monkeypatch):
    from simplebot.live_trader import ParamStore, SimpleLiveTrader

    monkeypatch.setattr(config, "LIVE_STATE_FILE", tmp_path / "live_state.json")

    best = {
        "updated_at": "test",
        "symbols": {"TEST": {"active": True, "params": PARAMS.to_dict()}},
    }
    best_file = tmp_path / "best_params.json"
    best_file.write_text(json.dumps(best))

    # V haussier → la dernière bougie clôturée de la fenêtre porte le signal long
    candles = make_candles(vshape_closes())
    signals = compute_signals(candles, PARAMS)
    sig_bar = max(i for i, s in enumerate(signals) if s == 1)
    window = candles[: sig_bar + 1]

    calls = []

    class SpyTrader(SimpleLiveTrader):
        def _open_position(self, symbol, direction, ref_price, atr_val, **kw):
            calls.append((symbol, direction))

    trader = SpyTrader(
        store=ParamStore(best_file),
        dry_run=True,
        fetch=lambda s, i, d, **kw: window + [dict(window[-1], ts=window[-1]["ts"] + 10**12)],
    )
    # le fetch renvoie une fausse bougie "en cours" tout au bout → closed_candles la retire
    trader.tick()
    assert calls == [("TEST", 1)]
    trader.tick()  # même bougie → pas de double ordre
    assert calls == [("TEST", 1)]


def test_dry_run_tracks_paper_positions(tmp_path, monkeypatch):
    """Le dry-run simule la position : OPEN papier, puis EXIT TP sur la
    bougie suivante, avec PnL enregistré."""
    from simplebot.live_trader import ParamStore, SimpleLiveTrader

    monkeypatch.setattr(config, "LIVE_STATE_FILE", tmp_path / "live_state.json")

    best_file = tmp_path / "best_params.json"
    best_file.write_text(json.dumps({
        "updated_at": "test",
        "symbols": {"TEST": {"active": True, "params": PARAMS.to_dict()}},
    }))

    candles = make_candles(vshape_closes())
    signals = compute_signals(candles, PARAMS)
    sig_bar = max(i for i, s in enumerate(signals) if s == 1)
    window = candles[: sig_bar + 1]
    interval_ms = 900_000

    feed = {"candles": window}

    def fake_fetch(symbol, interval, days, **kw):
        # + une bougie "en cours" que closed_candles doit retirer
        running = dict(feed["candles"][-1], ts=feed["candles"][-1]["ts"] + 10**12)
        return feed["candles"] + [running]

    trader = SimpleLiveTrader(store=ParamStore(best_file), dry_run=True, fetch=fake_fetch)
    trader.tick()

    paper = trader._live_state["paper"]
    assert "TEST" in paper["positions"]
    pos = paper["positions"]["TEST"]
    assert pos["dir"] == 1
    assert pos["sl"] < pos["entry"] < pos["tp"]
    assert trader._current_position("TEST") == 1.0
    assert trader._open_positions_count() == 1

    # bougie suivante : high au-dessus du TP, low au-dessus du SL → sortie TP
    last = window[-1]
    tp_candle = {
        "ts": last["ts"] + interval_ms,
        "open": last["close"],
        "high": pos["tp"] * 1.05,
        "low": last["close"],
        "close": pos["tp"],
        "volume": 100.0,
    }
    feed["candles"] = window + [tp_candle]
    trader.tick()

    assert paper["positions"] == {}
    assert len(paper["trades"]) == 1
    trade = paper["trades"][0]
    assert trade["reason"] == "TP"
    assert trade["pnl_pct"] > 0


class FakeClient:
    """Client Hyperliquid minimal pour tester réconciliation et kill-switch."""

    def __init__(self, positions=None, orders=None, account_value=1000.0):
        self.positions = positions or []
        self.orders = orders or []
        self.account_value = account_value
        self.tpsl_calls = []
        self.closed = []
        self.cancelled = []

    def get_positions(self, coin=None):
        return [p for p in self.positions if coin is None or p["coin"] == coin]

    def get_open_orders(self, coin=None):
        return [o for o in self.orders if coin is None or o["coin"] == coin]

    def cancel_all_orders(self, coin=None):
        self.cancelled.append(coin)
        return 0

    def place_position_tpsl(self, **kw):
        self.tpsl_calls.append(kw)
        return {"status": "ok"}

    def market_close(self, coin):
        self.closed.append(coin)
        self.positions = [p for p in self.positions if p["coin"] != coin]
        return {"status": "ok"}

    def get_account_value(self):
        return self.account_value


def _live_trader_with(client, tmp_path, monkeypatch, active_symbols=None):
    from simplebot.live_trader import ParamStore, SimpleLiveTrader

    monkeypatch.setattr(config, "LIVE_STATE_FILE", tmp_path / "live_state.json")
    best_file = tmp_path / "best_params.json"
    best_file.write_text(json.dumps({
        "updated_at": "test",
        "symbols": {s: {"active": True, "params": PARAMS.to_dict()}
                    for s in (active_symbols or [])},
    }))
    return SimpleLiveTrader(
        client=client,
        store=ParamStore(best_file),
        dry_run=False,
        fetch=lambda s, i, d, **kw: make_candles(wave_closes(n=200)),
    )


def test_reconcile_reprotects_naked_position(tmp_path, monkeypatch):
    client = FakeClient(positions=[{"coin": "TEST", "szi": 0.5, "entry_px": 100.0}])
    trader = _live_trader_with(client, tmp_path, monkeypatch, active_symbols=["TEST"])
    trader.reconcile_positions()

    assert len(client.tpsl_calls) == 1
    call = client.tpsl_calls[0]
    assert call["coin"] == "TEST"
    assert call["is_long"] is True
    assert call["sz"] == 0.5
    assert call["sl_price"] < 100.0 < call["tp_price"]
    assert client.closed == []


def test_reconcile_skips_protected_position(tmp_path, monkeypatch):
    client = FakeClient(
        positions=[{"coin": "TEST", "szi": 0.5, "entry_px": 100.0}],
        orders=[{"coin": "TEST", "isTrigger": True, "tpsl": "sl", "reduceOnly": True}],
    )
    trader = _live_trader_with(client, tmp_path, monkeypatch, active_symbols=["TEST"])
    trader.reconcile_positions()
    assert client.tpsl_calls == []
    assert client.closed == []


def test_reconcile_closes_position_without_params(tmp_path, monkeypatch):
    # symbole plus actif dans best_params → impossible de recalculer un SL
    client = FakeClient(positions=[{"coin": "ORPHAN", "szi": -0.5, "entry_px": 100.0}])
    trader = _live_trader_with(client, tmp_path, monkeypatch, active_symbols=[])
    trader.reconcile_positions()
    assert client.tpsl_calls == []
    assert client.closed == ["ORPHAN"]


def test_kill_switch_flattens_and_pauses(tmp_path, monkeypatch):
    import time as _time

    client = FakeClient(
        positions=[{"coin": "TEST", "szi": 0.5, "entry_px": 100.0}],
        account_value=900.0,
    )
    trader = _live_trader_with(client, tmp_path, monkeypatch, active_symbols=["TEST"])
    # pic à 1000 il y a 10 min → 900 = -10% > KILL_LOSS_PCT (5%)
    trader._live_state["equity_history"] = [[_time.time() - 600, 1000.0]]

    assert trader._kill_switch_engaged() is True
    assert client.closed == ["TEST"]
    assert trader._live_state["paused_until"] > _time.time()
    # toujours en pause au tick suivant, sans re-fermer quoi que ce soit
    client.closed.clear()
    assert trader._kill_switch_engaged() is True
    assert client.closed == []


def test_kill_switch_inactive_below_threshold(tmp_path, monkeypatch):
    import time as _time

    client = FakeClient(account_value=980.0)
    trader = _live_trader_with(client, tmp_path, monkeypatch)
    trader._live_state["equity_history"] = [[_time.time() - 600, 1000.0]]
    # -2% < seuil de 5% → pas de pause
    assert trader._kill_switch_engaged() is False
    assert trader._live_state["paused_until"] == 0


def test_optimizer_validation_is_binary_filter(tmp_path, monkeypatch):
    """La validation filtre mais ne classe pas : le 1er set du classement
    train qui confirme doit gagner, pas celui au meilleur PnL de validation."""
    import simplebot.optimizer as opt_mod
    from simplebot.backtester import BacktestResult

    grid = param_grid()
    p_a, p_b, p_c = grid[0], grid[1], grid[2]

    def fake_result(params, pnl, pf, n):
        return BacktestResult(
            params=params, n_trades=n, total_pnl_pct=pnl,
            winrate=0.5, profit_factor=pf, max_drawdown_pct=0.01,
        )

    def fake_run_backtest(candles, params, fee, slip, start_index=0):
        is_valid = start_index > 0
        if params == p_a:
            # meilleur train, mais échoue en validation
            return fake_result(params, 0.02, 0.5, 10) if is_valid \
                else fake_result(params, 0.30, 3.0, 20)
        if params == p_b:
            # 2e du train, confirme en validation (PnL valid modeste)
            return fake_result(params, 0.01, 1.5, 10) if is_valid \
                else fake_result(params, 0.20, 2.5, 20)
        if params == p_c:
            # 3e du train, PnL de validation énorme — ne doit PAS être choisi
            return fake_result(params, 0.50, 5.0, 10) if is_valid \
                else fake_result(params, 0.10, 2.0, 20)
        return BacktestResult(params=params)  # 0 trade → filtré

    monkeypatch.setattr(opt_mod, "run_backtest", fake_run_backtest)

    agent = BacktestOptimizerAgent(
        symbols=["TEST"],
        fetch=_fake_fetch_factory([100.0] * 1200),
        state_file=tmp_path / "best_params.json",
    )
    state = agent.run_once()
    entry = state["symbols"]["TEST"]
    assert entry["active"] is True
    assert StrategyParams.from_dict(entry["params"]) == p_b


def test_second_wallet_refuses_main_wallet(monkeypatch):
    from simplebot.live_trader import _assert_not_main_wallet

    monkeypatch.setenv("HL_ACCOUNT_ADDRESS", "0xABCDEF")
    with pytest.raises(RuntimeError):
        _assert_not_main_wallet("0xkey2", "0xabcdef")

    monkeypatch.setenv("HL_PRIVATE_KEY", "0xSAMEKEY")
    with pytest.raises(RuntimeError):
        _assert_not_main_wallet("0xsamekey", "0xother")

    # wallet distinct → OK
    _assert_not_main_wallet("0xkey2", "0x123456")

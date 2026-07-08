# -*- coding: utf-8 -*-
"""Tests du MomentumPaperTrader (stratégie momentum 4h paper, params figés)."""

import json
import math

import pytest

from simplebot import config
from simplebot.momentum import MomentumPaperTrader, momentum_signal

H4 = config.MOMENTUM_INTERVAL_MS
T0 = 1_700_000_000_000


def mk_candles(closes, ts0=T0, spread=0.5):
    out = []
    for i, c in enumerate(closes):
        out.append({
            "ts": ts0 + i * H4,
            "open": c, "high": c + spread, "low": c - spread,
            "close": c, "volume": 1.0,
        })
    return out


def rising(n=40, start=100.0, step=0.3):
    return [start + i * step for i in range(n)]      # ROC(12) ≈ +3.6% > +2%


def falling(n=40, start=100.0, step=0.3):
    return [start - i * step for i in range(n)]


def flat(n=40, base=100.0):
    return [base + 0.01 * math.sin(i) for i in range(n)]


def make_trader(tmp_path, candles_by_symbol, funding=None, symbols=None):
    def fetch(symbol, interval, days, **kw):
        assert interval == config.MOMENTUM_INTERVAL
        return candles_by_symbol[symbol]
    return MomentumPaperTrader(
        symbols=symbols or list(candles_by_symbol),
        fetch=fetch,
        funding_fetch=lambda: funding or {},
        state_file=tmp_path / "momentum_state.json",
    )


# ── Signal ───────────────────────────────────────────────────────────────────

def test_signal_long_on_rise():
    sig = momentum_signal(mk_candles(rising()))
    assert sig["signal"] == 1
    assert sig["roc"] > config.MOMENTUM_THR


def test_signal_short_on_fall():
    assert momentum_signal(mk_candles(falling()))["signal"] == -1


def test_signal_flat_is_zero():
    assert momentum_signal(mk_candles(flat()))["signal"] == 0


def test_signal_needs_warmup():
    assert momentum_signal(mk_candles(rising(n=5)))["signal"] == 0


# ── Paper : entrées / sorties ────────────────────────────────────────────────

def test_enter_long_and_persist(tmp_path):
    t = make_trader(tmp_path, {"AAA": mk_candles(rising())})
    t.sweep()
    pos = t.state["positions"]["AAA"]
    assert pos["dir"] == 1
    assert pos["sl"] < pos["entry"]
    # état persisté et rechargeable
    st = json.loads((tmp_path / "momentum_state.json").read_text())
    assert st["positions"]["AAA"]["dir"] == 1


def test_no_reentry_same_candle(tmp_path):
    candles = {"AAA": mk_candles(rising())}
    t = make_trader(tmp_path, candles)
    t.sweep()
    t.sweep()   # même bougie -> pas de double entrée ni erreur
    assert len(t.state["positions"]) == 1
    assert len(t.state["trades"]) == 0


def test_sl_exit(tmp_path):
    closes = rising()
    candles = {"AAA": mk_candles(closes)}
    t = make_trader(tmp_path, candles)
    t.sweep()
    pos = t.state["positions"]["AAA"]
    # bougie suivante : plonge sous le SL
    crash = dict(candles["AAA"][-1])
    crash = {"ts": crash["ts"] + H4, "open": pos["sl"] + 0.2, "high": pos["sl"] + 0.3,
             "low": pos["sl"] - 5.0, "close": pos["sl"] - 4.0, "volume": 1.0}
    candles["AAA"] = candles["AAA"] + [crash]
    t.sweep()
    # le long a été stoppé (trade SL enregistré) ; la bougie de crash peut
    # légitimement déclencher une ré-entrée SHORT (ROC < -2%) — comportement voulu
    tr = t.state["trades"][-1]
    assert tr["reason"] == "SL"
    assert tr["dir"] == 1
    assert tr["pnl_pct"] < 0
    assert t.state["equity"] < config.MOMENTUM_PAPER_EQUITY
    residual = t.state["positions"].get("AAA")
    assert residual is None or residual["dir"] == -1


def test_time_exit_after_72_bars(tmp_path):
    closes = rising()
    candles = {"AAA": mk_candles(closes)}
    t = make_trader(tmp_path, candles)
    t.sweep()
    entry = t.state["positions"]["AAA"]["entry"]
    # 73 bougies de plus, plates ET hautes (aucun SL touché), signal éteint (ROC≈0)
    last = closes[-1]
    more = [last + 0.001 * i for i in range(73)]
    candles["AAA"] = mk_candles(closes + more, spread=0.01)
    t.sweep()
    assert "AAA" not in t.state["positions"]
    tr = t.state["trades"][-1]
    assert tr["reason"] == "TIME"
    # PnL ≈ (exit-entry)/entry - coût (funding nul ici)
    assert tr["pnl_pct"] == pytest.approx((tr["exit"] - entry) / entry - 2 * (config.FEE_PCT + config.SLIPPAGE_PCT), abs=1e-9)


def test_flip_on_opposite_signal(tmp_path):
    closes = rising()
    candles = {"AAA": mk_candles(closes)}
    t = make_trader(tmp_path, candles)
    t.sweep()
    assert t.state["positions"]["AAA"]["dir"] == 1
    # retournement violent : ROC(12) passe sous -2% (SL long intouché : spread fin
    # et descente en marches au-dessus du SL ≈ entry-2×ATR avec ATR≈2×spread petit)
    # -> on force un SL très bas pour isoler le FLIP
    t.state["positions"]["AAA"]["sl"] = 0.01
    down = [closes[-1] * (1 - 0.004 * i) for i in range(1, 14)]
    candles["AAA"] = mk_candles(closes + down, spread=0.01)
    t.sweep()
    trades = t.state["trades"]
    assert trades and trades[-1]["reason"] == "FLIP"
    assert t.state["positions"]["AAA"]["dir"] == -1


def test_max_open_respected(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MOMENTUM_MAX_OPEN", 1)
    candles = {"AAA": mk_candles(rising()), "BBB": mk_candles(rising())}
    t = make_trader(tmp_path, candles)
    t.sweep()
    assert len(t.state["positions"]) == 1


def test_funding_accrual_long_pays_positive_rate(tmp_path):
    closes = rising()
    candles = {"AAA": mk_candles(closes)}
    t = make_trader(tmp_path, candles, funding={"AAA": 0.0001})  # 0.01%/h
    t.sweep()
    # 8 bougies de plus (32 h), hautes (pas de SL), signal maintenu
    more = [closes[-1] + 0.3 * i for i in range(1, 9)]
    candles["AAA"] = mk_candles(closes + more)
    t.sweep()
    pos = t.state["positions"]["AAA"]
    assert pos["funding_pct"] == pytest.approx(-0.0001 * 32, rel=0.3)  # long PAIE


def test_no_exchange_client_anywhere():
    """Garde-fou : le module momentum ne référence aucun client d'exchange."""
    import inspect
    import simplebot.momentum as m
    src = inspect.getsource(m)
    for forbidden in ("place_order", "market_close", "HyperliquidClient",
                      "make_second_wallet_client", "usd_class_transfer"):
        assert forbidden not in src, f"momentum.py ne doit pas contenir {forbidden}"


# ── Cap de positions (2026-07-08 : défaut 0 = illimité) ─────────────────────

def test_no_cap_by_default(tmp_path, monkeypatch):
    # Le cap 15 saturait en production et censurait tous les nouveaux signaux.
    # Défaut MOMENTUM_MAX_OPEN=0 → toutes les entrées passent.
    monkeypatch.setattr(config, "MOMENTUM_MAX_OPEN", 0)
    candles = {f"S{i:02d}": mk_candles(rising()) for i in range(20)}
    t = make_trader(tmp_path, candles)
    t.sweep()
    assert len(t.state["positions"]) == 20


def test_cap_still_enforced_when_set(tmp_path, monkeypatch):
    # SIMPLEBOT_MOMENTUM_MAX_OPEN > 0 reste respecté (opt-in via env).
    monkeypatch.setattr(config, "MOMENTUM_MAX_OPEN", 2)
    candles = {f"S{i:02d}": mk_candles(rising()) for i in range(5)}
    t = make_trader(tmp_path, candles)
    t.sweep()
    assert len(t.state["positions"]) == 2

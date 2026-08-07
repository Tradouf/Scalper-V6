# -*- coding: utf-8 -*-
"""Tests de l'exécution maker-first (simplebot/execution.py) et du mode
entry_mode=maker du backtester. Aucun réseau : client scripté, horloge injectée."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simplebot.backtester import run_backtest
from simplebot.execution import smart_close, smart_entry
from simplebot.strategy import StrategyParams

from tests.test_simplebot import make_candles, vshape_closes

PARAMS = StrategyParams(ema_fast=9, ema_slow=26, tp_atr=2.5, sl_atr=1.5)


class FakeExecClient:
    """Client scripté : book L2 fixe, ordres/positions contrôlés par le test."""

    def __init__(self, best_bid=99.0, best_ask=101.0, alo_ok=True,
                 open_orders_sequence=None, position_after_fill=0.0):
        self.best_bid = best_bid
        self.best_ask = best_ask
        self.alo_ok = alo_ok
        # séquence de listes d'oids encore au book, consommée à chaque poll
        self._oo_seq = list(open_orders_sequence or [])
        self.position_szi = 0.0
        self.position_after_fill = position_after_fill
        self.placed = []          # (order_type, px, sz, tif)
        self.cancelled = []

    def get_l2_snapshot(self, coin):
        return {"levels": [[{"px": str(self.best_bid), "sz": "10"}],
                           [{"px": str(self.best_ask), "sz": "10"}]]}

    def place_order(self, coin, is_buy, sz, limit_px, order_type="limit",
                    reduce_only=False, tif="Alo"):
        self.placed.append((order_type, limit_px, sz, tif, reduce_only))
        if order_type == "market":
            return {"status": "ok", "filled": True, "avg_px": limit_px, "total_sz": sz}
        if not self.alo_ok:
            raise RuntimeError("Post only order would have immediately matched")
        return {"status": "ok", "oid": 42, "filled": False}

    def get_open_orders(self, coin=None):
        if self._oo_seq:
            oids = self._oo_seq.pop(0)
        else:
            oids = []
        if not oids:                      # l'ordre a quitté le book → position remplie
            self.position_szi = self.position_after_fill
        return [{"oid": o} for o in oids]

    def get_positions(self, coin=None):
        if self.position_szi:
            return [{"coin": coin, "szi": self.position_szi}]
        return []

    def cancel_order(self, coin, oid):
        self.cancelled.append(oid)
        return True


def _run(client, **kw):
    return smart_entry(client, "TEST", True, 1.0, 100.0,
                       timeout_sec=10.0, poll_sec=1.0,
                       sleep=lambda s: None,
                       monotonic=_ticker(), **kw)


def _ticker():
    t = {"v": 0.0}
    def mono():
        t["v"] += 1.0
        return t["v"]
    return mono


# ── smart_entry ──────────────────────────────────────────────────────────────

def test_maker_fill_before_timeout():
    # l'ordre reste 1 poll au book puis disparaît → fill maker au mid
    client = FakeExecClient(open_orders_sequence=[[42], []], position_after_fill=1.0)
    res = _run(client)
    assert res["mode"] == "maker"
    assert res["avg_px"] == pytest.approx(100.0)   # mid (99+101)/2
    assert res["total_sz"] == 1.0
    assert client.cancelled == []
    assert client.placed[0][0] == "limit" and client.placed[0][3] == "Alo"


def test_timeout_falls_back_to_market_when_enabled():
    # l'ordre reste au book jusqu'au timeout → cancel + market si fallback ON
    client = FakeExecClient(open_orders_sequence=[[42]] * 50)
    res = _run(client, market_fallback=True)
    assert res["mode"] == "taker"
    assert client.cancelled == [42]
    assert client.placed[-1][0] == "market"


def test_timeout_skips_without_market_fallback():
    # Phase 1 : défaut = skip, pas de market
    client = FakeExecClient(open_orders_sequence=[[42]] * 50)
    res = _run(client, market_fallback=False)
    assert res["mode"] == "skip"
    assert res["total_sz"] == 0.0
    assert client.cancelled == [42]
    assert not any(p[0] == "market" for p in client.placed)


def test_alo_rejected_goes_market_when_enabled():
    # post-only rejeté sur mid ET best bid → market si fallback ON
    client = FakeExecClient(alo_ok=False)
    res = _run(client, market_fallback=True)
    assert res["mode"] == "taker"
    assert client.placed[-1][0] == "market"
    assert [p[0] for p in client.placed] == ["limit", "limit", "market"]


def test_alo_rejected_skips_without_market_fallback():
    client = FakeExecClient(alo_ok=False)
    res = _run(client, market_fallback=False)
    assert res["mode"] == "skip"
    assert res["total_sz"] == 0.0
    assert not any(p[0] == "market" for p in client.placed)


def test_no_book_uses_ref_price_limit():
    client = FakeExecClient(open_orders_sequence=[[]], position_after_fill=1.0)
    client.get_l2_snapshot = lambda coin: {"levels": []}
    res = _run(client)
    assert res["mode"] == "maker"
    assert res["avg_px"] == pytest.approx(100.0)   # ref_price faute de book


# ── smart_close (sorties maker reduce-only, P1 2026-08-07) ───────────────────

def _run_close(client, **kw):
    return smart_close(client, "TEST", False, 1.0, 100.0,   # ferme un LONG
                       timeout_sec=10.0, poll_sec=1.0,
                       sleep=lambda s: None,
                       monotonic=_ticker(), **kw)


def test_close_maker_fill_is_reduce_only():
    client = FakeExecClient(open_orders_sequence=[[42], []], position_after_fill=0.0)
    client.position_szi = 1.0                      # position ouverte au départ
    res = _run_close(client)
    assert res["mode"] == "maker"
    assert res["avg_px"] == pytest.approx(100.0)   # mid
    assert res["total_sz"] == pytest.approx(1.0)
    order_type, _, _, tif, reduce_only = client.placed[0]
    assert order_type == "limit" and tif == "Alo" and reduce_only is True


def test_close_timeout_market_fallback_default_on():
    """Contrairement à smart_entry, le défaut d'une SORTIE est le fallback
    market : un skip laisserait de l'exposition non désirée."""
    client = FakeExecClient(open_orders_sequence=[[42]] * 50)
    client.position_szi = 1.0
    client.position_after_fill = 1.0               # jamais rempli
    res = _run_close(client)
    assert res["mode"] == "taker"
    assert client.cancelled == [42]
    assert client.placed[-1][0] == "market"
    assert client.placed[-1][4] is True            # market reduce-only aussi


def test_close_alo_rejected_goes_market_reduce_only():
    client = FakeExecClient(alo_ok=False)
    client.position_szi = 1.0
    res = _run_close(client)
    assert res["mode"] == "taker"
    assert all(ro is True for (_, _, _, _, ro) in client.placed)


def test_close_no_fallback_keeps_position():
    client = FakeExecClient(open_orders_sequence=[[42]] * 50)
    client.position_szi = 1.0
    client.position_after_fill = 1.0
    res = _run_close(client, market_fallback=False)
    assert res["mode"] == "skip" and res["total_sz"] == 0.0
    assert not any(p[0] == "market" for p in client.placed)


def test_close_partial_then_market_is_mixed():
    """Fill partiel avant le timeout → cancel + market du reliquat = mixed."""
    class PartialClient(FakeExecClient):
        def cancel_order(self, coin, oid):
            self.position_szi = 0.6                # 0.4 fermé pendant le poll
            return super().cancel_order(coin, oid)
    client = PartialClient(open_orders_sequence=[[42]] * 50)
    client.position_szi = 1.0
    client.position_after_fill = 1.0
    res = _run_close(client)
    assert res["mode"] == "mixed"
    assert res["total_sz"] == pytest.approx(1.0)
    assert client.placed[-1][0] == "market"
    assert client.placed[-1][2] == pytest.approx(0.6)   # reliquat seulement


# ── backtester entry_mode=maker ──────────────────────────────────────────────

def _bt(candles, **kw):
    return run_backtest(candles, PARAMS, fee_pct=0.00045, slippage_pct=0.0003, **kw)


def test_taker_mode_unchanged():
    """Non-régression : entry_mode par défaut = résultats historiques exacts."""
    candles = make_candles(vshape_closes())
    a = _bt(candles)
    b = _bt(candles, entry_mode="taker")
    assert a.total_pnl_pct == b.total_pnl_pct
    assert a.n_trades == b.n_trades


def test_maker_mode_cheaper_when_limit_filled():
    """Bougie d'exécution qui revient sur le close du signal → fill maker,
    coût d'entrée réduit → PnL total ≥ taker sur les mêmes trades."""
    candles = make_candles(vshape_closes())
    taker = _bt(candles)
    maker = _bt(candles, entry_mode="maker")
    assert maker.n_trades == taker.n_trades
    # les entrées maker ne peuvent qu'améliorer le coût (fill au limit ou
    # fallback identique au taker) — jamais le dégrader
    assert maker.total_pnl_pct >= taker.total_pnl_pct - 1e-12


def test_maker_no_same_bar_tp_on_midbar_fill():
    """Fill mid-bar (low perce le limit) : le TP ne peut PAS être crédité dans
    la même bougie même si le high le touche — la sortie arrive plus tard."""
    import math
    interval = 900_000
    # montée douce + bruit sinusoïdal : cross EMA9/26 à ~bar 40, RSI ~68 (< 75)
    up = [100.0 + max(0, i - 35) * 0.08 + 0.4 * math.sin(i / 3.1) for i in range(80)]
    candles = make_candles(up, interval_ms=interval, spread=0.2)
    from simplebot.strategy import compute_signals
    signals = compute_signals(candles, PARAMS)
    sig_bars = [i for i, s in enumerate(signals) if s == 1]
    assert sig_bars, "la série calibrée doit produire un cross long"
    sb = sig_bars[0]
    lim = candles[sb]["close"]
    nxt = candles[sb + 1]
    # bougie d'exécution : ouvre AU-DESSUS du limit, plonge dessous (fill maker
    # mid-bar), puis high énorme (TP touchable) — le TP même-bougie doit être ignoré
    nxt["open"] = lim * 1.001
    nxt["low"] = lim * 0.999
    nxt["high"] = lim * 1.2
    nxt["close"] = lim * 1.0005
    # bougies suivantes plates sous le TP → la sortie ne peut pas être TP bar sb+1
    for c in candles[sb + 2:]:
        c["open"] = c["close"] = lim
        c["high"] = lim * 1.001
        c["low"] = lim * 0.999
    res = _bt(candles, entry_mode="maker")
    target = [t for t in res.trades if t["entry_bar"] == sb + 1 and t["dir"] == 1]
    assert target, "le cross long calibré doit produire un trade à sb+1"
    tr = target[0]
    assert tr["entry"] == pytest.approx(lim)          # fill maker au limit
    assert not (tr["reason"] == "TP" and tr["exit_bar"] == tr["entry_bar"])

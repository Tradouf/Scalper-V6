"""Tests NativeStopManager : réconciliation des SL natifs HL (2026-06-17)."""
from __future__ import annotations

from execution.stop_manager import NativeStopManager
from execution.types import CancelResult, OrderResult


class FakeExchange:
    def __init__(self, mark: float = 100.0):
        self._orders: list[dict] = []
        self._next = 1
        self._mark = mark
        self.placed: list = []
        self.cancelled: list = []

    def set_mark(self, m):
        self._mark = m

    def seed_sl(self, coin, side, sz, trigger):
        self._orders.append({
            "coin": coin, "oid": self._next, "side": side, "sz": sz,
            "triggerPx": trigger, "isTrigger": True, "reduceOnly": True, "tpsl": "sl",
        })
        self._next += 1

    def get_open_orders(self, coin=None):
        return list(self._orders)

    def get_mark_price(self, coin):
        return self._mark

    def place_order(self, req):
        self.placed.append(req)
        oid = self._next; self._next += 1
        if req.is_stop:
            self._orders.append({
                "coin": req.symbol, "oid": oid,
                "side": "A" if req.side == "sell" else "B",
                "sz": req.qty, "triggerPx": req.trigger_px,
                "isTrigger": True, "reduceOnly": True, "tpsl": "sl",
            })
        return OrderResult(order_id=str(oid), symbol=req.symbol, side=req.side,
                           qty=req.qty, price=req.trigger_px, status="accepted")

    def cancel_order(self, oid):
        self.cancelled.append(str(oid))
        self._orders = [o for o in self._orders if str(o["oid"]) != str(oid)]
        return CancelResult(order_id=str(oid), success=True)


def _mgr(ex, symbols=("BTC", "ETH"), manual=()):
    return NativeStopManager(ex, symbols=symbols, manual_symbols=manual)


def test_places_sl_when_absent():
    ex = FakeExchange(mark=100.0)
    out = _mgr(ex).reconcile({"BTC": {"stop_px": 95.0, "side": "buy", "qty": 0.3}})
    assert out["placed"] == 1
    assert ex.placed[0].is_stop and ex.placed[0].side == "sell"  # stop d'un long = sell
    assert ex.placed[0].reduce_only is True
    assert abs(ex.placed[0].trigger_px - 95.0) < 1e-9


def test_short_position_stop_is_buy():
    ex = FakeExchange(mark=100.0)
    _mgr(ex).reconcile({"ETH": {"stop_px": 105.0, "side": "sell", "qty": 0.5}})
    assert ex.placed[0].side == "buy"  # stop d'un short = buy


def test_keeps_existing_matching_sl():
    ex = FakeExchange(mark=100.0)
    ex.seed_sl("BTC", "A", 0.3, 95.0)
    out = _mgr(ex).reconcile({"BTC": {"stop_px": 95.0, "side": "buy", "qty": 0.3}})
    assert out["kept"] == 1 and out["placed"] == 0 and not ex.placed


def test_replaces_sl_when_level_moved():
    ex = FakeExchange(mark=100.0)
    ex.seed_sl("BTC", "A", 0.3, 90.0)  # ancien niveau loin du désiré 95
    out = _mgr(ex).reconcile({"BTC": {"stop_px": 95.0, "side": "buy", "qty": 0.3}})
    assert out["cancelled"] == 1 and out["placed"] == 1


def test_cancels_sl_when_no_position():
    ex = FakeExchange(mark=100.0)
    ex.seed_sl("BTC", "A", 0.3, 95.0)
    out = _mgr(ex).reconcile({})  # plus de position MR
    assert out["cancelled"] == 1 and not ex._orders


def test_dedups_extra_sls():
    ex = FakeExchange(mark=100.0)
    ex.seed_sl("BTC", "A", 0.3, 95.0)   # conforme
    ex.seed_sl("BTC", "A", 0.3, 95.0)   # doublon
    out = _mgr(ex).reconcile({"BTC": {"stop_px": 95.0, "side": "buy", "qty": 0.3}})
    assert out["kept"] == 1 and out["cancelled"] == 1


def test_never_touches_manual_symbols():
    ex = FakeExchange(mark=100.0)
    ex.seed_sl("HYPE", "A", 5.0, 28.0)  # SL manuel de francois
    out = _mgr(ex, symbols=("BTC", "HYPE"), manual=("HYPE",)).reconcile(
        {"HYPE": {"stop_px": 28.0, "side": "buy", "qty": 5.0}}
    )
    assert out == {"placed": 0, "cancelled": 0, "kept": 0, "skipped": 0, "errors": 0}
    assert not ex.cancelled and not ex.placed


def test_skips_when_stop_already_breached():
    ex = FakeExchange(mark=90.0)  # mark sous le stop d'un long → déjà franchi
    out = _mgr(ex).reconcile({"BTC": {"stop_px": 95.0, "side": "buy", "qty": 0.3}})
    assert out["skipped"] == 1 and out["placed"] == 0 and not ex.placed

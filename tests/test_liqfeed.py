"""Tests LIQFEED — logique d'agrégation et d'étiquetage (aucun réseau)."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def collector(monkeypatch):
    tmp = Path(tempfile.mkdtemp()) / "liq.db"
    monkeypatch.setenv("LIQFEED_DB", str(tmp))
    for m in list(sys.modules):
        if m.startswith("rsimr.liqfeed"):
            del sys.modules[m]
    from rsimr import liqfeed
    liqfeed.DB_PATH = tmp
    c = liqfeed.Collector(["AAA"])
    yield c, liqfeed
    c.con.close()


def test_trade_side_aggregation(collector):
    c, _ = collector
    c.on_trade({"coin": "AAA", "px": "10", "sz": "2", "side": "B",
                "time": 5_000, "users": ["0xa", "0xb"]})
    c.on_trade({"coin": "AAA", "px": "10", "sz": "3", "side": "A",
                "time": 5_400, "users": ["0xc", "0xd"]})
    b = c.buckets[(5, "AAA")]
    assert b["buy"] == 20.0 and b["sell"] == 30.0
    assert b["n"] == 2 and b["max"] == 30.0
    # contreparties mémorisées AVEC la taille du trade : c'est elle qui
    # sert à sonder les plus grosses d'abord (les liquidations sont grosses)
    assert {u for _, u, _ in c.recent_addr["AAA"]} == {"0xa", "0xb", "0xc", "0xd"}
    assert {n for _, _, n in c.recent_addr["AAA"]} == {20.0, 30.0}


def test_ctx_ignores_malformed(collector):
    c, _ = collector
    c.on_ctx({"coin": "AAA", "ctx": {"openInterest": None}})
    assert "AAA" not in c.ctx
    c.on_ctx({"coin": "AAA", "ctx": {"openInterest": "100", "markPx": "3",
                                     "funding": "0.0001", "premium": "0"}})
    assert c.ctx["AAA"]["oi"] == 100.0


def test_record_fills_only_liquidations(collector):
    c, _ = collector
    fills = [
        {"coin": "AAA", "px": "2", "sz": "5", "side": "A", "time": 1, "tid": 11,
         "dir": "Close Long"},  # pas une liquidation
        {"coin": "AAA", "px": "2", "sz": "7", "side": "A", "time": 2, "tid": 12,
         "dir": "Liquidated Cross Long",
         "liquidation": {"liquidatedUser": "0xvictim", "method": "market"}},
        {"coin": "BBB", "px": "2", "sz": "7", "side": "A", "time": 3, "tid": 13,
         "dir": "Liquidated Cross Long",
         "liquidation": {"liquidatedUser": "0xother", "method": "market"}},
    ]
    assert c.record_fills(fills, coin_filter="AAA") == (1, 0)
    rows = c.con.execute("SELECT coin, ntl, liquidated_user FROM liq").fetchall()
    assert rows == [("AAA", 14.0, "0xvictim")]
    # idempotence : même tid ne double pas
    c.record_fills(fills, coin_filter="AAA")
    assert c.con.execute("SELECT COUNT(*) FROM liq").fetchone()[0] == 1


def test_record_fills_without_filter_keeps_all(collector):
    c, _ = collector
    fills = [{"coin": "BBB", "px": "1", "sz": "1", "side": "B", "time": 9,
              "tid": 21, "dir": "Liquidated Isolated Short",
              "liquidation": {"liquidatedUser": "0xv", "method": "backstop"}}]
    assert c.record_fills(fills, source="backstop") == (1, 0)
    assert c.con.execute(
        "SELECT source FROM liq").fetchone()[0] == "backstop"


def test_universe_excludes_majors(monkeypatch, tmp_path):
    import json
    cache = tmp_path / "state" / "ohlcv_cache"
    cache.mkdir(parents=True)
    for sym in ("BTC", "ETH", "SOL", "WLD", "TINY"):
        n = 4200 if sym != "TINY" else 10
        (cache / f"{sym}__15m.json").write_text(
            json.dumps({"candles": [{"close": 1.0}] * n}))
    from rsimr import liqfeed
    monkeypatch.setattr(liqfeed, "REPO", tmp_path)
    monkeypatch.delenv("LIQFEED_SYMBOLS", raising=False)
    assert liqfeed.universe() == ["WLD"]


def test_universe_env_override(monkeypatch):
    from rsimr import liqfeed
    monkeypatch.setenv("LIQFEED_SYMBOLS", "AAA, BBB ,")
    assert liqfeed.universe() == ["AAA", "BBB"]


def test_record_fills_separates_recent_from_history(collector):
    """Sonder une adresse ramène tout son historique : ne pas l'imputer à la rafale."""
    c, _ = collector
    fills = [
        {"coin": "AAA", "px": "1", "sz": "1", "side": "A", "time": 1_000,
         "tid": 31, "dir": "Liquidated Cross Long",
         "liquidation": {"liquidatedUser": "0xv", "method": "market"}},
        {"coin": "AAA", "px": "1", "sz": "1", "side": "A", "time": 9_000,
         "tid": 32, "dir": "Liquidated Cross Long",
         "liquidation": {"liquidatedUser": "0xv", "method": "market"}},
    ]
    total, recent = c.record_fills(fills, coin_filter="AAA",
                                   recent_after_ms=5_000)
    assert (total, recent) == (2, 1)

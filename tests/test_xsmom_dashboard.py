"""Tests du dashboard XSMom — agrégation des tranches, neutralité, sans réseau."""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xsmom import dashboard as D  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "PAPER_STATE", tmp_path / "paper.json")
    monkeypatch.setattr(D, "LIVE_STATE", tmp_path / "live.json")
    monkeypatch.setattr(D, "all_mids", lambda: {})
    yield


def pos(dir_, entry, ntl):
    return {"dir": dir_, "entry": entry, "notional": ntl}


def write_paper(tranches, **kw):
    st = {"equity": 1000.0, "started_at": time.time() - 10 * 86_400,
          "equity_history": [[0, 1000.0]], "tranches": tranches,
          "fees_paid": 0.5, "funding_net": 0.6, "rebalances": []}
    st.update(kw)
    D.PAPER_STATE.write_text(json.dumps(st))


def test_same_symbol_across_tranches_is_netted(monkeypatch):
    """Un symbole long dans une tranche et short dans une autre : le risque
    réel est le NET, pas deux lignes contradictoires."""
    tr = [{} for _ in range(7)]
    tr[0]["ETH"] = pos(1, 100.0, 30.0)
    tr[1]["ETH"] = pos(-1, 100.0, 10.0)
    tr[2]["ETH"] = pos(1, 100.0, 5.0)
    write_paper(tr)
    s = D.build_state()
    eth = next(x for x in s["symbols"] if x["sym"] == "ETH")
    assert eth["net"] == pytest.approx(25.0)     # 30 − 10 + 5
    assert eth["gross"] == pytest.approx(45.0)
    assert sorted(eth["tranches"]) == [0, 1, 2]
    assert s["n_positions"] == 3


def test_market_neutrality_measures(monkeypatch):
    tr = [{} for _ in range(7)]
    tr[0]["AAA"] = pos(1, 10.0, 60.0)
    tr[0]["BBB"] = pos(-1, 10.0, 40.0)
    write_paper(tr, equity=1000.0)
    s = D.build_state()
    assert s["gross_long"] == 60.0 and s["gross_short"] == 40.0
    assert s["net_exposure"] == 20.0
    assert s["net_pct_equity"] == pytest.approx(2.0)


def test_unrealized_uses_live_prices(monkeypatch):
    monkeypatch.setattr(D, "all_mids", lambda: {"AAA": 11.0, "BBB": 9.0})
    tr = [{} for _ in range(7)]
    tr[0]["AAA"] = pos(1, 10.0, 100.0)     # long +10 % → +10 $
    tr[0]["BBB"] = pos(-1, 10.0, 100.0)    # short, prix −10 % → +10 $
    write_paper(tr)
    s = D.build_state()
    assert s["unrealized"] == pytest.approx(20.0)
    aaa = next(x for x in s["symbols"] if x["sym"] == "AAA")
    assert aaa["upnl"] == pytest.approx(10.0) and aaa["has_upnl"]


def test_missing_price_does_not_fake_a_pnl(monkeypatch):
    """Sans prix, on affiche « — » plutôt qu'un zéro trompeur."""
    monkeypatch.setattr(D, "all_mids", lambda: {})
    tr = [{} for _ in range(7)]
    tr[0]["AAA"] = pos(1, 10.0, 100.0)
    write_paper(tr)
    s = D.build_state()
    aaa = next(x for x in s["symbols"] if x["sym"] == "AAA")
    assert aaa["has_upnl"] is False
    assert s["unrealized"] == 0.0


def test_performance_against_fixed_criterion():
    tr = [{} for _ in range(7)]
    write_paper(tr, equity=1010.0,
                started_at=time.time() - 10 * 86_400,
                equity_history=[[0, 1000.0], [1, 1010.0]])
    s = D.build_state()
    assert s["pnl"] == pytest.approx(10.0)
    # +1 % en 10 j = 100 bps / 10 j = 10 bps/j
    assert s["bps_day"] == pytest.approx(10.0, rel=1e-3)
    assert s["target_bps"] == [5.0, 10.0]


def test_mode_is_paper_until_live_state_says_otherwise():
    write_paper([{} for _ in range(7)])
    assert D.build_state()["mode"] == "paper"
    assert D.build_state()["armed"] is False
    D.LIVE_STATE.write_text(json.dumps({"dry_run": False}))
    s = D.build_state()
    assert s["mode"] == "live" and s["armed"] is True


def test_dry_run_live_state_is_not_armed():
    write_paper([{} for _ in range(7)])
    D.LIVE_STATE.write_text(json.dumps({"dry_run": True}))
    assert D.build_state()["armed"] is False


def test_tranche_fill_reported():
    tr = [{} for _ in range(7)]
    tr[3] = {f"S{i}": pos(1, 1.0, 1.0) for i in range(16)}
    tr[4] = {f"T{i}": pos(-1, 1.0, 1.0) for i in range(9)}   # incomplète
    write_paper(tr)
    s = D.build_state()
    assert s["tranche_fill"] == [0, 0, 0, 16, 9, 0, 0]


def test_missing_state_does_not_crash():
    s = D.build_state()
    assert s["symbols"] == [] and s["n_positions"] == 0


# ── Contexte de marché (diagnostic, jamais un filtre de trading) ────────────

def _fake_meta(rows):
    """rows = [(nom, markPx, prevDayPx, volume)]"""
    meta = {"universe": [{"name": n} for n, _, _, _ in rows]}
    ctxs = [{"markPx": str(px), "prevDayPx": str(prev), "dayNtlVlm": str(v)}
            for _, px, prev, v in rows]
    return [meta, ctxs]


@pytest.fixture()
def no_mkt_cache():
    D._MKT["ts"] = 0.0
    D._MKT["data"] = {}
    yield
    D._MKT["ts"] = 0.0
    D._MKT["data"] = {}


def test_market_return_is_volume_weighted(monkeypatch, no_mkt_cache):
    """Un gros volume doit peser plus qu'un petit — sinon l'indice est faux."""
    rows = [("BIG", 110.0, 100.0, 900.0),    # +10 %, 90 % du volume
            ("SMALL", 90.0, 100.0, 100.0)]   # −10 %, 10 % du volume
    monkeypatch.setattr(D.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp(_fake_meta(rows)))
    m = D.market_context()
    assert m["available"] is True
    assert m["ret24h_weighted"] == pytest.approx(0.08)   # .9*.1 + .1*(-.1)
    assert m["ret24h_equal"] == pytest.approx(0.0)
    assert m["breadth_up_pct"] == pytest.approx(50.0)


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def read(self):
        return json.dumps(self._p).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_market_ignores_malformed_rows(monkeypatch, no_mkt_cache):
    rows = [("OK", 110.0, 100.0, 50.0), ("NOPX", 0.0, 100.0, 50.0),
            ("NOVOL", 110.0, 100.0, 0.0)]
    monkeypatch.setattr(D.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp(_fake_meta(rows)))
    m = D.market_context()
    assert m["n"] == 1


def test_market_failure_is_reported_not_faked(monkeypatch, no_mkt_cache):
    def boom(*a, **k):
        raise RuntimeError("API muette")
    monkeypatch.setattr(D.urllib.request, "urlopen", boom)
    m = D.market_context()
    assert m["available"] is False and "API muette" in m.get("error", "")


def test_market_is_cached(monkeypatch, no_mkt_cache):
    calls = []
    rows = [("A", 110.0, 100.0, 10.0)]

    def once(*a, **k):
        calls.append(1)
        return _FakeResp(_fake_meta(rows))
    monkeypatch.setattr(D.urllib.request, "urlopen", once)
    D.market_context()
    D.market_context()
    assert len(calls) == 1          # un seul appel réseau grâce au cache


def test_build_state_includes_market(monkeypatch, no_mkt_cache):
    write_paper([{} for _ in range(7)])
    monkeypatch.setattr(D, "market_context", lambda: {"available": False})
    assert D.build_state()["market"] == {"available": False}

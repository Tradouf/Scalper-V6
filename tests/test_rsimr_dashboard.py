"""Tests du dashboard Ricochet — l'affichage des positions, sans réseau."""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rsimr import dashboard as D  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "LIVE_STATE", tmp_path / "live.json")
    monkeypatch.setattr(D, "PAPER_STATE", tmp_path / "paper.json")
    monkeypatch.setattr(D, "LIQ_DB", tmp_path / "absent.db")
    D._CACHE["hl"] = None
    D._CACHE["ts"] = 0.0
    yield


def write_live(tmp, positions=None, **kw):
    st = {"positions": positions or {}, "dry_run": False, "n_trades": 0,
          "realized_usd": 0.0, "skipped": {}, "exec_stats": {}}
    st.update(kw)
    D.LIVE_STATE.write_text(json.dumps(st))


def fake_hl(monkeypatch, positions=(), equity=207.9):
    monkeypatch.setattr(D, "_hl_snapshot", lambda: {
        "address": "0xMASTER", "equity": equity, "error": None,
        "positions": list(positions)})


def test_position_tracked_by_bot_and_exchange(monkeypatch):
    now_ms = int(time.time() * 1000)
    write_live(D.LIVE_STATE, {"ENA": {"dir": 1, "entry": 0.5, "sz": 50.0,
                                      "notional": 25.0,
                                      "opened_ms": now_ms - 3_600_000,
                                      "regime": 1}})
    fake_hl(monkeypatch, [{"coin": "ENA", "szi": 50.0, "notional": 25.4,
                           "upnl": 0.4, "entry": 0.5}])
    s = D.build_state()
    assert len(s["positions"]) == 1
    p = s["positions"][0]
    assert p["sym"] == "ENA" and p["known_by_bot"] and p["on_exchange"]
    assert p["upnl"] == 0.4
    assert p["regime_label"] == "normal"
    # sortie à 4 h, ouverte depuis 1 h → ~3 h restantes
    assert 2.9 < p["remaining_h"] < 3.1
    assert s["divergence"] == {"bot_seul": [], "exchange_seul": []}


def test_position_on_exchange_unknown_to_bot_is_displayed(monkeypatch):
    """Le cas qui compte : une position que le bot ignore doit APPARAÎTRE."""
    write_live(D.LIVE_STATE, {})
    fake_hl(monkeypatch, [{"coin": "JTO", "szi": 94.0, "notional": 46.0,
                           "upnl": -1.2, "entry": 0.49}])
    s = D.build_state()
    assert [p["sym"] for p in s["positions"]] == ["JTO"]
    p = s["positions"][0]
    assert p["known_by_bot"] is False and p["on_exchange"] is True
    assert p["upnl"] == -1.2 and p["remaining_h"] is None
    assert s["divergence"]["exchange_seul"] == ["JTO"]


def test_position_known_by_bot_but_missing_on_exchange(monkeypatch):
    now_ms = int(time.time() * 1000)
    write_live(D.LIVE_STATE, {"WLD": {"dir": 1, "entry": 1.0, "sz": 25.0,
                                      "notional": 25.0, "opened_ms": now_ms,
                                      "regime": 2}})
    fake_hl(monkeypatch, [])
    s = D.build_state()
    p = s["positions"][0]
    assert p["known_by_bot"] and not p["on_exchange"]
    assert p["regime_label"] == "tempête"
    assert s["divergence"]["bot_seul"] == ["WLD"]


def test_positions_sorted_by_time_to_exit(monkeypatch):
    now_ms = int(time.time() * 1000)
    write_live(D.LIVE_STATE, {
        "AAA": {"dir": 1, "entry": 1, "sz": 1, "notional": 10,
                "opened_ms": now_ms - 3_600_000, "regime": 1},       # 3 h restantes
        "BBB": {"dir": 1, "entry": 1, "sz": 1, "notional": 10,
                "opened_ms": now_ms - 3 * 3_600_000, "regime": 1},   # 1 h restante
    })
    fake_hl(monkeypatch, [{"coin": "CCC", "szi": 1.0, "notional": 5.0,
                           "upnl": 0.0, "entry": 1.0}])
    s = D.build_state()
    # la plus proche de la sortie d'abord ; l'inconnue du bot en dernier
    assert [p["sym"] for p in s["positions"]] == ["BBB", "AAA", "CCC"]


def test_mode_is_read_never_guessed(monkeypatch):
    write_live(D.LIVE_STATE, {}, dry_run=True)
    fake_hl(monkeypatch)
    assert D.build_state()["mode"] == "dry"
    write_live(D.LIVE_STATE, {}, dry_run=False)
    fake_hl(monkeypatch)
    assert D.build_state()["mode"] == "live"


def test_paper_average_net_bps(monkeypatch):
    write_live(D.LIVE_STATE, {})
    fake_hl(monkeypatch)
    D.PAPER_STATE.write_text(json.dumps(
        {"n_trades": 4, "n_wins": 1, "realized_usd": -2.0,
         "sum_net_bps": -400.0, "open": []}))
    p = D.build_state()["paper"]
    assert p["avg_net_bps"] == -100.0


def test_missing_state_files_do_not_crash(monkeypatch):
    fake_hl(monkeypatch)
    s = D.build_state()
    assert s["positions"] == [] and s["paper"]["n_trades"] == 0

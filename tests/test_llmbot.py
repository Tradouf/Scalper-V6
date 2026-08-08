"""Tests LLMBot."""

from __future__ import annotations

from llmbot.indicators import compute_snapshot
from llmbot.quant_scanner import rank_candidates, scan_symbol, score_setup


def _candles(n=50, trend_up=True):
    out = []
    p = 100.0
    for i in range(n):
        d = 0.3 if trend_up else -0.3
        p += d
        out.append({"ts": i, "open": p - 0.1, "high": p + 0.5, "low": p - 0.5, "close": p, "volume": 1000})
    return out


def test_compute_snapshot():
    s = compute_snapshot(_candles())
    assert s["price"] > 0
    assert "rsi" in s
    assert s["trend"] in ("bull", "bear", "range")


def test_quant_score_long_setup():
    tech = compute_snapshot(_candles(trend_up=True))
    ob = {"spread_pct": 0.03, "bid_ask_imbalance": 0.15, "is_liquid_enough": True}
    score, direction, _ = score_setup(tech, ob, "any")
    assert score >= 0
    assert direction in ("long", "short", "none")


def test_scan_eligible_high_score():
    scan = scan_symbol(_candles(), {"spread_pct": 0.02, "bid_ask_imbalance": 0.2, "is_liquid_enough": True})
    assert "quant_score" in scan
    assert "direction" in scan


def test_rank_candidates_limits():
    cands = [
        {"eligible": True, "quant_score": 80, "symbol": "A"},
        {"eligible": True, "quant_score": 90, "symbol": "B"},
        {"eligible": False, "quant_score": 50, "symbol": "C"},
    ]
    ranked = rank_candidates(cands)
    assert len(ranked) <= 3
    assert ranked[0]["symbol"] == "B"


def test_paper_sl_updates_equity():
    """Paper dry-run doit clôturer au SL ROE et débiter l'equity $."""
    import time
    from llmbot.live import LLMLiveTrader

    t = LLMLiveTrader(client=None, dry_run=True)
    t.state = {
        "trades": [],
        "paper_positions": {
            "BTC": {
                "side": "long",
                "entry": 100.0,
                "tp_roe": 0.03,
                "sl_roe": 0.015,
                "ts": time.time(),
            }
        },
        "equity": 200.0,
        "equity_history": [],
        "paused_until": 0,
    }
    # move ≈ −0.6 % prix → ROE 3× ≈ −1.8 % < −1.5 % SL
    t._paper_mark_exits({"BTC": 99.4})
    assert "BTC" not in t.state["paper_positions"]
    assert len(t.state["trades"]) == 1
    assert t.state["trades"][0]["reason"] == "SL"
    assert t.state["equity"] < 200.0


def test_agent_trader_news_block(monkeypatch):
    from llmbot import agent_trader

    def fake_llm(*a, **k):
        return None

    monkeypatch.setattr(agent_trader.llm, "chat_json", fake_llm)
    scan = {
        "technical": {"price": 100, "atr_pct": 0.01, "rsi": 55, "trend": "bull"},
        "orderbook": {},
        "direction": "long",
        "quant_score": 70,
    }
    news = {"block_longs": True, "block_shorts": False, "sentiment": "bearish", "confidence": 0.8}
    d = agent_trader.decide("BTC", scan, news, 0)
    assert d["action"] == "WAIT"
    assert "news" in d["reason"]
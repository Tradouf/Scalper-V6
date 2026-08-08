"""Tests GrokWatch — parser, store, ingestion, évaluation (tout hors-ligne)."""

import time

import pytest

from grokwatch import evaluate as ev
from grokwatch import ingest as ing
from grokwatch import store
from grokwatch.parser import normalize, parse_signal

SAMPLE = """BTC Quid is ready
=================

Crypto Short Position Analysis

Position recommandée : SHORT BTC-PERP sur Hyperliquid
(Levier 2x-3x max, 2-5 % du capital maximum, marge isolée de préférence).

Ceci n'est pas un conseil financier. Les perps sont extrêmement risqués.

### Contexte marché (25 juillet 2026)

-   BTC ~64 100–64 160 $ (Hyperliquid BTC-USD ~64 055–64 090 $ récemment).
"""


# ----------------------------------------------------------------- parser

def test_parse_sample_email():
    sig = parse_signal(SAMPLE)
    assert sig is not None
    assert sig["direction"] == "SHORT"
    assert sig["symbol"] == "BTC"
    assert sig["leverage"] == 2
    assert sig["size_pct_range"] == [2.0, 5.0]
    assert len(sig["content_hash"]) == 40


def test_parse_long_english_and_html():
    text = "<html><body><p>Recommended position: LONG ETH-PERP on Hyperliquid</p></body></html>"
    sig = parse_signal(text)
    assert sig is not None
    assert sig["direction"] == "LONG"
    assert sig["symbol"] == "ETH"
    assert sig["leverage"] is None


def test_parse_no_signal():
    assert parse_signal("Bonjour, voici la météo du jour.") is None
    assert parse_signal("") is None
    assert parse_signal(None) is None


def test_normalize_strips_html_and_whitespace():
    assert normalize("<b>a</b>\n\n  b&amp;c") == "a b&c"


def test_hash_stable_across_html_wrapping():
    plain = parse_signal("Position recommandée : SHORT BTC-PERP")
    wrapped = parse_signal("<div>Position   recommandée : SHORT BTC-PERP</div>")
    assert plain["content_hash"] == wrapped["content_hash"]


# ------------------------------------------------------------------ store

def test_store_dedup(tmp_path, monkeypatch):
    monkeypatch.setenv("GROKWATCH_STATE_DIR", str(tmp_path))
    sig = {"content_hash": "abc", "direction": "SHORT", "symbol": "BTC"}
    assert store.record_signal(sig) is True
    assert store.record_signal(dict(sig)) is False
    assert len(store.load_signals()) == 1


# ----------------------------------------------------------------- ingest

def test_ingest_records_mid_and_dedups(tmp_path, monkeypatch):
    monkeypatch.setenv("GROKWATCH_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(ing, "fetch_mid", lambda symbol: 64100.0)
    sig = ing.ingest_text(SAMPLE, received_ts=1_753_000_000.0, source="test")
    assert sig is not None
    assert sig["mid_at_receipt"] == 64100.0
    assert sig["ts"] == 1_753_000_000.0
    # ré-ingestion du même mail → doublon
    assert ing.ingest_text(SAMPLE, received_ts=1_753_100_000.0) is None
    assert len(store.load_signals()) == 1


def test_ingest_garbage_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("GROKWATCH_STATE_DIR", str(tmp_path))
    assert ing.ingest_text("pas un signal") is None
    assert store.load_signals() == []


# --------------------------------------------------------------- evaluate

def _candles(t0_ms, closes, step_ms=900_000):
    return [{"ts": t0_ms + i * step_ms, "open": c, "high": c, "low": c,
             "close": c, "volume": 1.0} for i, c in enumerate(closes)]


def test_price_at():
    candles = _candles(0, [100.0, 101.0, 102.0])
    assert ev.price_at(candles, 0) == 100.0
    assert ev.price_at(candles, 900_000 + 1) == 101.0
    assert ev.price_at(candles, 10_000_000) == 102.0
    assert ev.price_at(candles, -1) is None
    assert ev.price_at([], 0) is None


def test_signal_returns_short_wins_when_price_drops():
    t0 = 1_000_000_000.0  # secondes
    # prix 100 → 98 sur 24 h de bougies 15 m
    n = 24 * 4 + 1
    closes = [100.0 - 2.0 * i / (n - 1) for i in range(n)]
    candles = _candles(int(t0 * 1000), closes)
    sig = {"ts": t0, "direction": "SHORT", "symbol": "BTC",
           "mid_at_receipt": 100.0}
    rets = ev.signal_returns(sig, candles, now_ts=t0 + 25 * 3600)
    assert set(rets) == {"1h", "4h", "24h"}
    assert rets["24h"]["gross"] == pytest.approx(0.02, abs=1e-6)
    assert rets["24h"]["net"] == pytest.approx(0.02 - ev.FEE_ROUNDTRIP, abs=1e-6)
    # un LONG sur la même trajectoire perd
    sig_long = dict(sig, direction="LONG")
    assert ev.signal_returns(sig_long, candles,
                             now_ts=t0 + 25 * 3600)["24h"]["gross"] < 0


def test_signal_returns_skips_unexpired_horizons():
    t0 = 1_000_000_000.0
    candles = _candles(int(t0 * 1000), [100.0] * 20)
    sig = {"ts": t0, "direction": "SHORT", "symbol": "BTC",
           "mid_at_receipt": 100.0}
    rets = ev.signal_returns(sig, candles, now_ts=t0 + 2 * 3600)
    assert "1h" in rets and "4h" not in rets and "24h" not in rets


def test_evaluate_aggregates(tmp_path, monkeypatch):
    monkeypatch.setenv("GROKWATCH_STATE_DIR", str(tmp_path))
    t0 = time.time() - 26 * 3600
    store.record_signal({"content_hash": "h1", "ts": t0, "iso": "x",
                         "direction": "SHORT", "symbol": "BTC",
                         "mid_at_receipt": 100.0})
    n = 27 * 4
    closes = [100.0 - 2.0 * i / (n - 1) for i in range(n)]
    monkeypatch.setattr(ev, "fetch_ohlcv",
                        lambda symbol, interval, days: _candles(int(t0 * 1000), closes))
    monkeypatch.setattr(ev, "closed_candles", lambda c, ms: c)
    res = ev.evaluate()
    assert len(res["signals"]) == 1
    agg = res["aggregate"]
    assert agg["24h"]["n"] == 1
    assert agg["24h"]["hit_rate"] == 1.0
    assert agg["24h"]["mean_net"] > 0

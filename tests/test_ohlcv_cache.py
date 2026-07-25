"""Tests du cache OHLCV disque partagé (hl_ohlcv_cache)."""

import hl_ohlcv_cache as hoc


HOUR = 3_600_000


def _candles(start_ms, n, step=HOUR):
    return [
        {"ts": start_ms + i * step, "open": 1.0, "high": 2.0,
         "low": 0.5, "close": 1.5, "volume": 10.0}
        for i in range(n)
    ]


def test_miss_avant_tout_fetch():
    assert hoc.cache_get("BTC", "1h", 0, HOUR, now_ms=HOUR) is None


def test_hit_apres_put_meme_bougie():
    now = 100 * HOUR + 120_000            # 2 min après la frontière
    cs = _candles(50 * HOUR, 51)          # couvre 50h → 100h
    hoc.cache_put("BTC", "1h", cs, fetched_at_ms=now)
    got = hoc.cache_get("BTC", "1h", 60 * HOUR, now, now_ms=now)
    assert got is not None
    assert got[0]["ts"] >= 60 * HOUR      # découpage au start demandé
    assert got[-1]["ts"] == cs[-1]["ts"]


def test_invalide_apres_cloture_de_bougie():
    fetched = 100 * HOUR + 120_000
    hoc.cache_put("ETH", "1h", _candles(90 * HOUR, 11), fetched_at_ms=fetched)
    later = 101 * HOUR + 60_000           # une bougie a clôturé depuis le fetch
    assert hoc.cache_get("ETH", "1h", 95 * HOUR, later, now_ms=later) is None


def test_fenetre_non_couverte_refusee():
    now = 100 * HOUR + 60_000
    hoc.cache_put("SOL", "1h", _candles(95 * HOUR, 6), fetched_at_ms=now)
    # demande qui commence bien avant le début du cache → miss
    assert hoc.cache_get("SOL", "1h", 20 * HOUR, now, now_ms=now) is None


def test_fenetre_historique_jamais_cachee():
    now = 100 * HOUR + 60_000
    hoc.cache_put("BTC", "1h", _candles(50 * HOUR, 51), fetched_at_ms=now)
    # end_ms très antérieur à maintenant → bypass
    assert hoc.cache_get("BTC", "1h", 40 * HOUR, 60 * HOUR, now_ms=now) is None


def test_merge_conserve_le_prefixe_ancien():
    now = 100 * HOUR + 60_000
    hoc.cache_put("AVAX", "1h", _candles(40 * HOUR, 61), fetched_at_ms=now)   # 40→100
    hoc.cache_put("AVAX", "1h", _candles(80 * HOUR, 21), fetched_at_ms=now)   # 80→100
    got = hoc.cache_get("AVAX", "1h", 45 * HOUR, now, now_ms=now)
    assert got is not None and got[0]["ts"] == 45 * HOUR


def test_desactivation_env(monkeypatch):
    monkeypatch.setenv("HL_OHLCV_CACHE", "0")
    now = 100 * HOUR
    hoc.cache_put("BTC", "1h", _candles(90 * HOUR, 11), fetched_at_ms=now)
    assert hoc.cache_get("BTC", "1h", 95 * HOUR, now, now_ms=now) is None

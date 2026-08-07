"""Tests du gate placebo (placebo_gate.py, racine repo)."""

import math
import random

import pytest

from placebo_gate import PlaceboReport, run_gate, shuffle_candles


# ── Fabriques de données synthétiques ────────────────────────────────────────

def _mk_candles(closes, ts0=1_000_000, step=900_000):
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append({
            "ts": ts0 + i * step,
            "open": o, "high": max(o, c) * 1.001, "low": min(o, c) * 0.999,
            "close": c, "volume": 100.0 + i,
        })
    return out


def _trending(n=400, drift=0.002, seed=1):
    """Marche avec drift + persistance : autocorrélation réelle."""
    rng = random.Random(seed)
    px, closes, mom = 100.0, [], 0.0
    for _ in range(n):
        mom = 0.9 * mom + rng.gauss(drift, 0.004)
        px *= math.exp(mom)
        closes.append(px)
    return _mk_candles(closes)


def _noise(n=400, seed=2):
    rng = random.Random(seed)
    px, closes = 100.0, []
    for _ in range(n):
        px *= math.exp(rng.gauss(0, 0.004))
        closes.append(px)
    return _mk_candles(closes)


# ── shuffle_candles : le null préserve ce qu'il doit préserver ───────────────

def test_shuffle_preserves_returns_and_shapes():
    candles = _noise(200)
    out = shuffle_candles(candles, random.Random(0))
    assert out is not None and len(out) == len(candles)
    # même multiset de rendements close-à-close (l'ordre change)
    rets = sorted(round(candles[i]["close"] / candles[i - 1]["close"], 12)
                  for i in range(1, len(candles)))
    rets2 = sorted(round(out[i]["close"] / out[i - 1]["close"], 12)
                   for i in range(1, len(out)))
    assert rets == pytest.approx(rets2)
    # même multiset de formes de barres
    shapes = sorted((round(c["high"] / c["close"], 12), round(c["low"] / c["close"], 12))
                    for c in candles[1:])
    shapes2 = sorted((round(c["high"] / c["close"], 12), round(c["low"] / c["close"], 12))
                     for c in out[1:])
    assert shapes == shapes2
    # timestamps et volumes d'origine, position par position
    assert [c["ts"] for c in out] == [c["ts"] for c in candles]
    assert [c["volume"] for c in out] == [c["volume"] for c in candles]
    # cohérence OHLC
    for c in out:
        assert c["low"] <= min(c["open"], c["close"]) + 1e-9
        assert c["high"] >= max(c["open"], c["close"]) - 1e-9


def test_shuffle_changes_order_and_is_seed_deterministic():
    candles = _noise(200)
    a = shuffle_candles(candles, random.Random(7))
    b = shuffle_candles(candles, random.Random(7))
    c = shuffle_candles(candles, random.Random(8))
    assert [x["close"] for x in a] == [x["close"] for x in b]
    assert [x["close"] for x in a] != [x["close"] for x in c]
    assert [x["close"] for x in a] != [x["close"] for x in candles]


def test_shuffle_rejects_degenerate_series():
    assert shuffle_candles([], random.Random(0)) is None
    assert shuffle_candles(_mk_candles([1.0, 2.0]), random.Random(0)) is None
    bad = _mk_candles([1.0, -1.0, 2.0, 3.0])
    assert shuffle_candles(bad, random.Random(0)) is None


# ── run_gate : sépare un vrai signal du bruit ────────────────────────────────

def _autocorr_selector(candles_by_symbol):
    """Sélectionne les symboles à autocorrélation lag-1 positive marquée —
    exactement ce que le shuffle détruit."""
    kept = set()
    for sym, candles in candles_by_symbol.items():
        rets = [candles[i]["close"] / candles[i - 1]["close"] - 1.0
                for i in range(1, len(candles))]
        m = sum(rets) / len(rets)
        num = sum((rets[i] - m) * (rets[i - 1] - m) for i in range(1, len(rets)))
        den = sum((r - m) ** 2 for r in rets)
        if den > 0 and num / den > 0.3:
            kept.add(sym)
    return kept


def _lucky_selector(candles_by_symbol):
    """Sélecteur arbitraire insensible à l'ordre des barres (pur bruit)."""
    return {s for s, c in candles_by_symbol.items()
            if int(c[-1]["close"] * 1e6) % 2 == 0}


def test_gate_passes_on_real_autocorrelation():
    data = {f"S{i}": _trending(seed=i) for i in range(8)}
    report = run_gate(data, _autocorr_selector, n_placebo=25, seed=3)
    assert report.real_count >= 6            # le signal est bien détecté en réel
    assert report.p_value < 0.05 and report.passed


def test_gate_warns_when_n_placebo_too_small_for_alpha():
    """Avec n < 1/α − 1, p min = 1/(n+1) ≥ α : le gate ne peut pas passer."""
    data = {f"S{i}": _trending(seed=i) for i in range(4)}
    r = run_gate(data, _autocorr_selector, n_placebo=10, alpha=0.05, seed=9)
    assert any("trop faible" in n for n in r.notes)


def test_gate_fails_on_noise_selector():
    data = {f"N{i}": _noise(seed=100 + i) for i in range(8)}
    report = run_gate(data, _lucky_selector, n_placebo=19, seed=4)
    assert not report.passed and report.p_value >= 0.05


def test_gate_deterministic_and_report_fields():
    data = {f"S{i}": _trending(seed=i) for i in range(4)}
    r1 = run_gate(data, _autocorr_selector, n_placebo=7, seed=5)
    r2 = run_gate(data, _autocorr_selector, n_placebo=7, seed=5)
    assert r1.null_counts == r2.null_counts and r1.p_value == r2.p_value
    assert r1.n_symbols == 4
    assert isinstance(r1.real_selection, list)
    assert "p=" in r1.summary()


def test_gate_int_selection_and_empty_data():
    data = {f"S{i}": _trending(seed=i) for i in range(3)}
    r = run_gate(data, lambda d: 2, n_placebo=5, seed=6)   # sélection = int
    assert r.real_count == 2 and not r.passed              # constant ⇒ p=1
    r_empty = run_gate({}, _lucky_selector, n_placebo=5)
    assert not r_empty.passed and r_empty.n_symbols == 0


def test_gate_never_returns_p_zero():
    """Correction +1/+1 : même un réel écrasant garde p > 0 sur peu de tirages."""
    data = {f"S{i}": _trending(seed=i) for i in range(8)}
    r = run_gate(data, _autocorr_selector, n_placebo=9, seed=7)
    assert r.p_value >= 1.0 / 10.0

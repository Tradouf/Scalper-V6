"""Tests XSMom paper — état isolé via state_file tmp_path (jamais l'état réel)."""

import math
import time

import pytest

from xsmom.paper import (
    N_LEG, N_TRANCHES, RET_DAYS, VOL_DAYS,
    XSMomentumPaperTrader, score_symbol,
)

DAY_MS = 86_400_000


def make_closes(n, drift=0.0, start=100.0):
    out = [start]
    for i in range(n - 1):
        out.append(out[-1] * (1.0 + drift + 0.001 * ((i % 5) - 2)))
    return out


def test_score_symbol_signe_et_seuils():
    assert score_symbol(make_closes(10)) is None          # trop court
    up = score_symbol(make_closes(40, drift=0.01))
    dn = score_symbol(make_closes(40, drift=-0.01))
    assert up is not None and dn is not None
    assert up > 0 > dn


def test_score_symbol_vol_scaling():
    """À rendement 14j identique, plus de vol → score plus petit."""
    calm = make_closes(40, drift=0.005)
    noisy = list(calm)
    for i in range(len(noisy) - VOL_DAYS, len(noisy) - 1):
        noisy[i] *= 1.03 if i % 2 else 0.97
    s_calm, s_noisy = score_symbol(calm), score_symbol(noisy)
    assert s_calm is not None and s_noisy is not None
    assert abs(s_noisy) < abs(s_calm)


def _mk_trader(tmp_path, n_syms=30, drifts=None):
    syms = [f"S{i}" for i in range(n_syms)]
    drifts = drifts or {s: (i - n_syms / 2) * 0.002 for i, s in enumerate(syms)}
    now_ms = int(time.time() * 1000)

    def fetch(sym, interval, days, **kw):
        assert interval == "1d"
        closes = make_closes(45, drift=drifts[sym])
        t0 = now_ms - 45 * DAY_MS
        return [{"ts": t0 + i * DAY_MS, "open": c, "high": c, "low": c,
                 "close": c, "volume": 1.0} for i, c in enumerate(closes)]

    return XSMomentumPaperTrader(
        fetch=fetch,
        funding_fetch=lambda: {},
        universe_fetch=lambda top_n: syms,
        state_file=tmp_path / "xsmom_state.json",
    ), syms, drifts


def test_rebalance_remplit_une_tranche(tmp_path, monkeypatch):
    monkeypatch.setattr("xsmom.paper.FETCH_THROTTLE_SEC", 0)
    tr, syms, drifts = _mk_trader(tmp_path)
    assert tr.rebalance_if_due() is True
    filled = [t for t in tr.state["tranches"] if t]
    assert len(filled) == 1
    book = filled[0]
    assert len(book) == 2 * N_LEG
    longs = {s for s, p in book.items() if p["dir"] == 1}
    shorts = {s for s, p in book.items() if p["dir"] == -1}
    # les longs ont les drifts les plus forts, les shorts les plus faibles
    assert max(drifts[s] for s in shorts) < min(drifts[s] for s in longs)


def test_un_seul_rebalance_par_jour(tmp_path, monkeypatch):
    monkeypatch.setattr("xsmom.paper.FETCH_THROTTLE_SEC", 0)
    tr, _, _ = _mk_trader(tmp_path)
    assert tr.rebalance_if_due() is True
    assert tr.rebalance_if_due() is False


def test_frais_compte_a_l_entree(tmp_path, monkeypatch):
    monkeypatch.setattr("xsmom.paper.FETCH_THROTTLE_SEC", 0)
    tr, _, _ = _mk_trader(tmp_path)
    eq0 = tr.state["equity"]
    tr.rebalance_if_due()
    # 16 positions × (equity/7/16) × 1.5 bps
    expected_fee = (eq0 / N_TRANCHES) * 0.00015
    assert tr.state["equity"] == pytest.approx(eq0 - expected_fee, rel=1e-6)
    assert tr.state["fees_paid"] == pytest.approx(expected_fee, rel=1e-6)


def test_paper_only_aucun_client():
    """Garantie structurelle : le module ne référence aucun client d'exchange."""
    import xsmom.paper as m
    src = open(m.__file__, encoding="utf-8").read()
    for interdit in ("place_order", "HyperliquidClient", "PRIVATE_KEY",
                     "hyperliquid_client", "make_third_wallet"):
        assert interdit not in src


def test_etat_persiste_et_recharge(tmp_path, monkeypatch):
    monkeypatch.setattr("xsmom.paper.FETCH_THROTTLE_SEC", 0)
    tr, syms, _ = _mk_trader(tmp_path)
    tr.rebalance_if_due()
    eq = tr.state["equity"]
    tr2 = XSMomentumPaperTrader(
        fetch=tr._fetch, funding_fetch=lambda: {},
        universe_fetch=lambda top_n: syms,
        state_file=tmp_path / "xsmom_state.json",
    )
    assert tr2.state["equity"] == pytest.approx(eq)
    assert sum(len(t) for t in tr2.state["tranches"]) == 2 * N_LEG

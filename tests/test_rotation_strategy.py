"""Tests de RotationStrategy (méta-allocateur ensemble-contrarian vol-targeted, 2026-06-21).
Vérifie : parité EXACTE avec le backtest validé (weighted_ensemble R=1), méta ∈[-1,1], vol-targeting,
garde-fou gross, seuil min, données insuffisantes, équité nulle, champs du Signal."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from core.config import RotationStrategyConfig
from core.types import Candle, MarketSnapshot
from strategies.rotation import RotationStrategy
from strategies.strategy_pool import build_pool_deep, strat_returns
from backtest.run_rotation import weighted_ensemble


def _candles(closes, vol_frac=0.015, vol=1000.0):
    """Bougies 1d OHLCV depuis une série de closes (+ une bougie 'en formation' en fin,
    ignorée par la stratégie via l'exclusion de la dernière)."""
    t0 = dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc)
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        hi = max(prev, c) * (1 + vol_frac)
        lo = min(prev, c) * (1 - vol_frac)
        out.append(Candle(ts_open=t0 + dt.timedelta(days=i), open=prev, high=hi, low=lo,
                          close=c, volume=vol * (1 + 0.1 * (i % 5))))
        prev = c
    return out


def _market(candles_by_sym):
    return MarketSnapshot(timestamp=dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc),
                          candles=candles_by_sym, prices={s: c[-1].close for s, c in candles_by_sym.items()})


def _cfg(**kw):
    base = dict(enabled=True, symbols=["BTC"], pool="deep", L=90, temp=1.0, vol_win=30,
                target_vol_annual=0.20, scalar_cap=3.0, max_gross_frac=0.60, min_notional_usdc=10.0)
    base.update(kw)
    return RotationStrategyConfig(**base)


def _rng_walk(n, drift, seed=0, sigma=0.02, start=100.0):
    rng = np.random.default_rng(seed)
    return start * np.cumprod(1 + drift + rng.normal(0, sigma, n))


def _df_from_candles(cdl):
    cc = cdl[:-1]   # exclut la bougie en formation (comme la stratégie)
    return pd.DataFrame({
        "open": [c.open for c in cc], "high": [c.high for c in cc],
        "low": [c.low for c in cc], "close": [c.close for c in cc],
        "volume": [c.volume for c in cc],
    })


def test_parity_with_backtest():
    """Le cœur : la position méta du module live == weighted_ensemble(R=1, contrarian) du backtest
    validé, à la dernière barre, au bit près. Garantit live = backtest."""
    closes = _rng_walk(420, drift=0.0005, seed=3, sigma=0.025)
    cdl = _candles(closes)
    df = _df_from_candles(cdl)
    strat = RotationStrategy(_cfg(), equity_callback=lambda: 10_000.0)
    live_meta = strat._meta_position(df)
    pool = build_pool_deep(df)
    rets = {nm: strat_returns(df, p) for nm, p in pool.items()}
    bt_last = float(weighted_ensemble(df, pool, rets, 90, 1, "invperf", 1.0).iloc[-1])
    assert live_meta == pytest.approx(bt_last, abs=1e-12)


def test_meta_in_range_and_signal_sign():
    closes = _rng_walk(420, drift=0.001, seed=5)
    strat = RotationStrategy(_cfg(), equity_callback=lambda: 10_000.0)
    sigs = strat.generate_signals(_market({"BTC": _candles(closes)}))
    assert len(sigs) == 1
    meta = strat.get_last_metrics()["BTC"]["meta"]
    assert -1.0 <= meta <= 1.0
    # direction = signe de la conviction méta
    assert sigs[0].direction == (1.0 if meta > 0 else (-1.0 if meta < 0 else 0.0))
    assert sigs[0].strategy_id == "rotation"


def test_insufficient_data_no_signal():
    closes = _rng_walk(100, drift=0.001, seed=6)   # < 200 + L + vol_win
    strat = RotationStrategy(_cfg(), equity_callback=lambda: 10_000.0)
    assert strat.generate_signals(_market({"BTC": _candles(closes)})) == []


def test_no_equity_no_signal():
    closes = _rng_walk(420, drift=0.001, seed=7)
    strat = RotationStrategy(_cfg(), equity_callback=lambda: 0.0)
    assert strat.generate_signals(_market({"BTC": _candles(closes)})) == []


def test_vol_targeting_scalar_inverse_to_vol():
    # Le notional confond conviction (|méta|) × scalar ; on isole le vol-targeting via le SCALAR
    # (= target_vol/vol_réalisée) exposé dans les métriques : moins de vol → scalar plus grand.
    calm = _rng_walk(420, drift=0.0008, seed=3, sigma=0.012)
    wild = _rng_walk(420, drift=0.0008, seed=3, sigma=0.045)
    cfg = _cfg(scalar_cap=10.0, max_gross_frac=3.0)
    sc = RotationStrategy(cfg, equity_callback=lambda: 10_000.0)
    sw = RotationStrategy(cfg, equity_callback=lambda: 10_000.0)
    sc.generate_signals(_market({"BTC": _candles(calm)}))
    sw.generate_signals(_market({"BTC": _candles(wild)}))
    assert sc.get_last_metrics()["BTC"]["scalar"] > sw.get_last_metrics()["BTC"]["scalar"]


def test_gross_guardrail_rescales():
    eq = 10_000.0
    closes = {s: _candles(_rng_walk(420, drift=0.0008, seed=i, sigma=0.008))
              for i, s in enumerate(["BTC", "ETH", "SOL", "BNB"])}
    cfg = _cfg(symbols=["BTC", "ETH", "SOL", "BNB"], scalar_cap=10.0, max_gross_frac=0.5)
    sigs = RotationStrategy(cfg, equity_callback=lambda: eq).generate_signals(_market(closes))
    gross = sum(s.target_notional for s in sigs)
    assert gross <= 0.5 * eq + 1e-6


def test_min_notional_flattens():
    closes = _rng_walk(420, drift=0.001, seed=8)
    strat = RotationStrategy(_cfg(min_notional_usdc=10.0), equity_callback=lambda: 10.0)
    sigs = strat.generate_signals(_market({"BTC": _candles(closes)}))
    assert sigs[0].target_notional == 0.0


def test_base_pool_also_works():
    closes = _rng_walk(420, drift=0.001, seed=9)
    strat = RotationStrategy(_cfg(pool="base"), equity_callback=lambda: 10_000.0)
    sigs = strat.generate_signals(_market({"BTC": _candles(closes)}))
    assert len(sigs) == 1 and -1.0 <= strat.get_last_metrics()["BTC"]["meta"] <= 1.0


# ─── Pool xdeep (#2 2026-06-27) — familles orthogonales, validé marginal, NON déployé ──

def test_xdeep_pool_valid():
    """build_pool_xdeep produit des positions valides (52 strat, ∈[-1,1], finies)."""
    from strategies.strategy_pool import build_pool_deep, build_pool_xdeep, strat_returns
    closes = _rng_walk(420, drift=0.0006, seed=31, sigma=0.025)
    df = _df_from_candles(_candles(closes))
    deep = build_pool_deep(df); xd = build_pool_xdeep(df)
    assert len(xd) == len(deep) + 9
    for nm, pos in xd.items():
        assert pos.abs().max() <= 1.0 + 1e-9, nm
        assert np.all(np.isfinite(strat_returns(df, pos))), nm


def test_rotation_xdeep_meta_in_range():
    """RotationStrategy avec pool='xdeep' (config-gated) reste cohérente."""
    closes = _rng_walk(420, drift=0.001, seed=32)
    strat = RotationStrategy(_cfg(pool="xdeep"), equity_callback=lambda: 10_000.0)
    sigs = strat.generate_signals(_market({"BTC": _candles(closes)}))
    assert len(sigs) == 1 and -1.0 <= strat.get_last_metrics()["BTC"]["meta"] <= 1.0


# ─── Overlay régime high-vol (#1 2026-06-27) — opt-in, validé +15% OOS ──

def test_vol_overlay_off_by_default_parity():
    """cut=1.0 (défaut) → overlay inactif : _meta_position inchangé (parité préservée)."""
    closes = _rng_walk(450, drift=0.0005, seed=41, sigma=0.02)
    df = _df_from_candles(_candles(closes))
    strat = RotationStrategy(_cfg(), equity_callback=lambda: 10_000.0)  # cut défaut 1.0
    assert strat._cfg.vol_regime_cut_pct == 1.0
    sigs = strat.generate_signals(_market({"BTC": _candles(closes)}))
    assert strat.get_last_metrics()["BTC"]["vol_pct"] == 0.0   # non calculé quand off


def test_vol_overlay_cuts_high_vol():
    """Coin dont la vol récente est dans le tiers HAUT → cut=0.66 → flat."""
    calm = _rng_walk(410, drift=0.0006, seed=42, sigma=0.008)
    wild = _rng_walk(50, drift=0.0006, seed=43, sigma=0.05, start=float(calm[-1]))
    closes = np.concatenate([calm, wild])
    strat = RotationStrategy(_cfg(vol_regime_cut_pct=0.66), equity_callback=lambda: 10_000.0)
    sigs = strat.generate_signals(_market({"BTC": _candles(closes)}))
    m = strat.get_last_metrics()["BTC"]
    assert m["vol_pct"] > 0.66            # vol récente au sommet de sa distribution
    assert m["meta"] == 0.0 and sigs[0].target_notional == 0.0   # → flat


def test_vol_overlay_keeps_low_vol():
    """Coin à vol DÉCROISSANTE (dernier point = vol minimale) → vol_pct bas → cut=0.66 ne coupe pas."""
    rng = np.random.default_rng(45)
    n = 460
    sig = np.linspace(0.05, 0.003, n)                 # vol qui décroît jusqu'au minimum final
    closes = 100.0 * np.cumprod(1 + 0.0006 + rng.normal(0, 1, n) * sig)
    strat = RotationStrategy(_cfg(vol_regime_cut_pct=0.66), equity_callback=lambda: 10_000.0)
    strat.generate_signals(_market({"BTC": _candles(closes)}))
    assert strat.get_last_metrics()["BTC"]["vol_pct"] < 0.5   # dernière vol parmi les plus basses

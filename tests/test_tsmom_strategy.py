"""Tests de TsmomStrategy (intégration live du trend following 1d, 2026-06-20).
Vérifie : direction = signe de tendance, sizing vol-targeting equal-risk, garde-fou de
gross dur, zone morte, seuil min, données insuffisantes, parité de signe avec le backtest."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from core.config import TsmomStrategyConfig
from core.types import Candle, MarketSnapshot
from strategies.tsmom import TsmomStrategy


def _candles(closes, vol_frac=0.01):
    """Construit des bougies 1d depuis une série de closes (+ une bougie 'en formation'
    en fin, que la stratégie ignore via l'index -2)."""
    t0 = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    out = []
    for i, c in enumerate(closes):
        hi = c * (1 + vol_frac); lo = c * (1 - vol_frac)
        out.append(Candle(ts_open=t0 + dt.timedelta(days=i), open=c, high=hi, low=lo,
                          close=c, volume=1000.0))
    return out


def _market(candles_by_sym):
    return MarketSnapshot(timestamp=dt.datetime(2024, 6, 1, tzinfo=dt.timezone.utc),
                          candles=candles_by_sym, prices={s: c[-1].close for s, c in candles_by_sym.items()})


def _cfg(**kw):
    # lookbacks=[] → chemin single-lookback (rétro-compat) : ces tests valident le signe/sizing
    # sur un horizon unique. Les tests d'ensemble passent lookbacks=[...] explicitement.
    base = dict(enabled=True, symbols=["BTC"], lookback=20, lookbacks=[], vol_win=20,
                target_vol_annual=0.20, scalar_cap=3.0, max_gross_frac=0.60,
                min_notional_usdc=10.0)
    base.update(kw)
    return TsmomStrategyConfig(**base)


def _rng_walk(n, drift, seed=0, sigma=0.02, start=100.0):
    rng = np.random.default_rng(seed)
    rets = drift + rng.normal(0, sigma, n)
    return start * np.cumprod(1 + rets)


def test_long_in_uptrend():
    closes = _rng_walk(80, drift=+0.01, seed=1)
    strat = TsmomStrategy(_cfg(), equity_callback=lambda: 10_000.0)
    sigs = strat.generate_signals(_market({"BTC": _candles(closes)}))
    assert len(sigs) == 1 and sigs[0].direction == 1.0
    assert sigs[0].target_notional > 0


def test_short_in_downtrend():
    closes = _rng_walk(80, drift=-0.01, seed=2)
    strat = TsmomStrategy(_cfg(), equity_callback=lambda: 10_000.0)
    sigs = strat.generate_signals(_market({"BTC": _candles(closes)}))
    assert sigs[0].direction == -1.0


def test_vol_targeting_sizes_inverse_to_vol():
    # Même tendance, deux vols réalisées différentes → la moins volatile a un notional
    # plus grand (scalar = target_vol/realized), à equity égale.
    calm = _rng_walk(80, drift=0.005, seed=3, sigma=0.01)
    wild = _rng_walk(80, drift=0.005, seed=3, sigma=0.04)
    eq = 10_000.0
    # max_gross_frac haut pour isoler le vol-targeting (pas de rescale parasite).
    cfg = _cfg(scalar_cap=10.0, max_gross_frac=3.0)
    s_calm = TsmomStrategy(cfg, equity_callback=lambda: eq).generate_signals(
        _market({"BTC": _candles(calm)}))[0]
    s_wild = TsmomStrategy(cfg, equity_callback=lambda: eq).generate_signals(
        _market({"BTC": _candles(wild)}))[0]
    assert s_calm.target_notional > s_wild.target_notional


def test_gross_guardrail_rescales():
    # 4 coins très peu volatils → scalars élevés → gross dépasse le cap → rescale.
    closes = {s: _candles(_rng_walk(80, drift=0.005, seed=i, sigma=0.005))
              for i, s in enumerate(["BTC", "ETH", "SOL", "BNB"])}
    cfg = _cfg(symbols=["BTC", "ETH", "SOL", "BNB"], scalar_cap=10.0, max_gross_frac=0.5)
    eq = 10_000.0
    sigs = TsmomStrategy(cfg, equity_callback=lambda: eq).generate_signals(_market(closes))
    gross = sum(s.target_notional for s in sigs)
    assert gross <= 0.5 * eq + 1e-6


def test_deadzone_band_flat():
    # Série quasi plate + grande band → direction 0, target 0.
    closes = _rng_walk(80, drift=0.0, seed=5, sigma=0.001)
    strat = TsmomStrategy(_cfg(band=0.20), equity_callback=lambda: 10_000.0)
    sigs = strat.generate_signals(_market({"BTC": _candles(closes)}))
    assert sigs[0].direction == 0.0 and sigs[0].target_notional == 0.0


def test_min_notional_flattens():
    # Equity minuscule → notional sous le minimum HL → flat.
    closes = _rng_walk(80, drift=0.01, seed=6)
    strat = TsmomStrategy(_cfg(min_notional_usdc=10.0), equity_callback=lambda: 10.0)
    sigs = strat.generate_signals(_market({"BTC": _candles(closes)}))
    assert sigs[0].target_notional == 0.0


def test_insufficient_data_no_signal():
    closes = _rng_walk(10, drift=0.01, seed=7)  # < lookback+vol_win+3
    strat = TsmomStrategy(_cfg(), equity_callback=lambda: 10_000.0)
    sigs = strat.generate_signals(_market({"BTC": _candles(closes)}))
    assert sigs == []


def test_no_equity_no_signal():
    closes = _rng_walk(80, drift=0.01, seed=8)
    strat = TsmomStrategy(_cfg(), equity_callback=lambda: 0.0)
    assert strat.generate_signals(_market({"BTC": _candles(closes)})) == []


def test_signal_sign_matches_backtester():
    # Parité de SIGNE avec backtest.backtester._signals_tsmom sur la dernière barre fermée.
    import pandas as pd
    from backtest.backtester import Backtester
    closes = _rng_walk(120, drift=0.0, seed=11, sigma=0.03)
    cdl = _candles(closes)
    strat = TsmomStrategy(_cfg(lookback=30, vol_win=20), equity_callback=lambda: 10_000.0)
    live_dir = strat.generate_signals(_market({"BTC": cdl}))[0].direction
    # Backtest sur les MÊMES closes fermées (sans la bougie -1 en formation).
    df = pd.DataFrame({"close": [c.close for c in cdl[:-1]]})
    bt_sig = Backtester(None)._signals_tsmom(df, lookback=30, band=0.0)
    assert live_dir == float(bt_sig.iloc[-1])


# ─── Ensemble multi-lookback (recherche 2026-06-20, run_tsmom_ensemble.py) ──────

def test_ensemble_conviction_scales_notional():
    """Conviction partielle → notional = (notional pleine conviction) × |conviction|. Prix
    DÉTERMINISTE : longue baisse (100→50) puis rebond récent (50→65). Horizons courts (10/30/60)
    voient la hausse récente (+), le plus long (120) reste sous son niveau passé (−) → conviction
    = (+,+,+,−)/4 = +0,5. Comparé au single-lookback sur les MÊMES closes (même vol → même
    scalar) : l'ensemble pèse exactement la moitié."""
    down = np.linspace(100.0, 50.0, 150)
    up = np.linspace(50.0, 65.0, 60)
    closes = np.concatenate([down, up])
    cdl = _candles(closes)
    # max_gross_frac haut : isole la proportionnalité (sinon les deux saturent le cap de gross).
    ens = TsmomStrategy(_cfg(lookbacks=[10, 30, 60, 120], max_gross_frac=3.0),
                        equity_callback=lambda: 10_000.0)
    single = TsmomStrategy(_cfg(lookback=30, lookbacks=[], max_gross_frac=3.0),
                           equity_callback=lambda: 10_000.0)
    se = ens.generate_signals(_market({"BTC": cdl}))[0]
    ss = single.generate_signals(_market({"BTC": cdl}))[0]
    conv = ens.get_last_metrics()["BTC"]["conviction"]

    assert conv == pytest.approx(0.5)                    # (+,+,+,−)/4
    assert se.direction == 1.0
    # Même closes/vol_win ⇒ même scalar ⇒ notional ensemble = notional single × |conviction|.
    assert se.target_notional == pytest.approx(ss.target_notional * abs(conv), rel=1e-9)


def test_ensemble_all_agree_matches_single():
    """Quand tous les horizons s'accordent (conviction=±1), l'ensemble donne la MÊME direction
    et le MÊME notional que le single-lookback (|conviction|=1 ne réduit rien)."""
    closes = _rng_walk(200, drift=+0.012, seed=24, sigma=0.008)
    cdl = _candles(closes)
    ens = TsmomStrategy(_cfg(lookbacks=[10, 30, 60]), equity_callback=lambda: 10_000.0)
    single = TsmomStrategy(_cfg(lookback=30, lookbacks=[]), equity_callback=lambda: 10_000.0)
    se = ens.generate_signals(_market({"BTC": cdl}))[0]
    ss = single.generate_signals(_market({"BTC": cdl}))[0]
    assert se.direction == ss.direction == 1.0
    assert ens.get_last_metrics()["BTC"]["conviction"] == 1.0
    # Notional identique : conviction=1 → pas de réduction (même vol_win/scalar).
    assert se.target_notional == pytest.approx(ss.target_notional, rel=1e-9)


def test_ensemble_split_opinion_goes_flat():
    """Conviction nulle (autant d'horizons long que short) → direction 0 → flat. Prix
    DÉTERMINISTE : baisse (100→60) puis remontée (60→100) symétriques. Horizons courts (20/40)
    voient la remontée (+), horizons longs (180/220) remontent à leur niveau de départ → ~0
    → conviction = (+,+,0/−,0/−) qui s'annule. On vérifie direction=0 / flat."""
    down = np.linspace(100.0, 50.0, 200)         # long déclin (les horizons longs restent −)
    up = np.linspace(50.0, 55.0, 60)             # petit rebond récent (les horizons courts +)
    closes = np.concatenate([down, up])
    strat = TsmomStrategy(_cfg(lookbacks=[20, 40, 180, 220]), equity_callback=lambda: 10_000.0)
    sigs = strat.generate_signals(_market({"BTC": _candles(closes)}))
    conv = strat.get_last_metrics()["BTC"]["conviction"]
    assert conv == pytest.approx(0.0)            # (+,+,−,−)/4 = 0
    assert sigs == [] or sigs[0].direction == 0.0 or sigs[0].target_notional == 0.0


def test_ensemble_needs_longest_lookback_history():
    """`need` est dimensionné sur le PLUS LONG horizon : trop peu de bougies → pas de signal."""
    closes = _rng_walk(90, drift=+0.01, seed=27)
    strat = TsmomStrategy(_cfg(lookbacks=[10, 30, 120], vol_win=20), equity_callback=lambda: 10_000.0)
    # need = 120 + 20 + 3 = 143 > 90 → données insuffisantes → aucun signal.
    assert strat.generate_signals(_market({"BTC": _candles(closes)})) == []

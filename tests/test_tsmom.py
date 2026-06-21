"""Tests du signal time-series momentum / Donchian (trend following, ajouté 2026-06-20).
Contrairement aux 10 signaux rejetés, le TSMOM 1d est POSITIF OOS net de frais sur 9/12
symboles (côté short positif partout → pas du beta long déguisé). Ces tests verrouillent
la CAUSALITÉ (pas de look-ahead), la PERSISTANCE de l'état (≠ croisement) et le dispatch."""
from __future__ import annotations

import numpy as np
import pandas as pd

from backtest.backtester import Backtester


# ── Time-series momentum ─────────────────────────────────────────────────────
def test_tsmom_state_follows_trend():
    bt = Backtester(None)
    # Montée nette puis chute nette → état LONG sur la montée, SHORT sur la chute.
    close = np.concatenate([np.linspace(100, 150, 60), np.linspace(150, 80, 60)])
    df = pd.DataFrame({"open": close, "high": close + 0.1, "low": close - 0.1, "close": close})
    sig = bt._signals_tsmom(df, lookback=20, band=0.0)
    assert sig.iloc[55] == 1      # encore en pleine montée
    assert sig.iloc[-1] == -1     # bien retourné short au bout de la chute


def test_tsmom_is_persistent_not_a_crossing():
    # L'état doit être porté à CHAQUE barre (pas seulement au franchissement) :
    # sur une montée monotone, presque toutes les barres après lookback valent +1.
    bt = Backtester(None)
    close = np.linspace(100, 200, 100)
    df = pd.DataFrame({"open": close, "high": close + 0.1, "low": close - 0.1, "close": close})
    sig = bt._signals_tsmom(df, lookback=20, band=0.0)
    assert (sig.iloc[20:] == 1).all()


def test_tsmom_band_creates_deadzone():
    # Avec une grosse band, un micro-mouvement ne déclenche aucun état.
    bt = Backtester(None)
    rng = np.random.default_rng(0)
    close = 100 + np.cumsum(rng.normal(0, 0.01, 200))  # quasi plat
    df = pd.DataFrame({"open": close, "high": close + 0.01, "low": close - 0.01, "close": close})
    sig = bt._signals_tsmom(df, lookback=20, band=0.10)
    assert (sig == 0).all()


def test_tsmom_is_causal():
    bt = Backtester(None)
    rng = np.random.default_rng(5)
    close = 100 + np.cumsum(rng.normal(0, 0.5, 400))
    df = pd.DataFrame({"open": close, "high": close + 0.2, "low": close - 0.2, "close": close})
    full = bt._signals_tsmom(df, lookback=30, band=0.0)
    half = bt._signals_tsmom(df.iloc[:200], lookback=30, band=0.0)
    assert (full.iloc[:200].values == half.values).all()


# ── Donchian breakout ────────────────────────────────────────────────────────
def test_donchian_long_on_breakout_and_persists():
    bt = Backtester(None)
    # Range puis cassure haussière nette → état LONG qui se maintient.
    close = np.concatenate([np.full(40, 100.0) + np.sin(np.arange(40)), np.linspace(101, 130, 40)])
    df = pd.DataFrame({"open": close, "high": close + 0.1, "low": close - 0.1, "close": close})
    sig = bt._signals_donchian(df, lookback=20)
    assert sig.iloc[-1] == 1


def test_donchian_is_causal():
    bt = Backtester(None)
    rng = np.random.default_rng(9)
    close = 100 + np.cumsum(rng.normal(0, 0.4, 300))
    df = pd.DataFrame({"open": close, "high": close + 0.2, "low": close - 0.2, "close": close})
    full = bt._signals_donchian(df, lookback=20)
    half = bt._signals_donchian(df.iloc[:150], lookback=20)
    assert (full.iloc[:150].values == half.values).all()


# ── Dispatch ─────────────────────────────────────────────────────────────────
def test_tsmom_dispatch_reverse_exit():
    bt = Backtester(None)
    rng = np.random.default_rng(2)
    close = 100 + np.cumsum(rng.normal(0, 0.3, 500))
    df = pd.DataFrame({"ts": np.arange(500), "open": close, "high": close + 0.2,
                       "low": close - 0.2, "close": close, "volume": np.ones(500)})
    r = bt.run_on_df(df, "BTC", "tsmom", lookback=30, band=0.0, exit_mode="reverse")
    assert r.strategy == "tsmom"
    assert r.nb_trades >= 0

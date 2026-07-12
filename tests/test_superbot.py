# -*- coding: utf-8 -*-
"""
Tests SuperBot Phase 1 — sleeve Adaptive EMA, backtester unifié maker,
walk-forward multi-TF, filtre qualité. Aucun réseau : bougies synthétiques
et backtest_fn injectés.

    python -m pytest tests/test_superbot.py -v
"""

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from simplebot.backtester import BacktestResult
from simplebot.strategy import StrategyParams

from superbot import config
from superbot.backtester import run_sleeve_backtest
from superbot.optimizer import SuperOptimizer, train_composite
from superbot.sleeves.adaptive_ema import AdaptiveEMASleeve
from superbot.sleeves.base import ExitPolicy, Sleeve
from superbot.symbol_filter import apply_symbol_filter, quality_score

T0 = 1_700_000_000_000


def mk_candles(closes, interval_ms=900_000, spread=0.5, extra=None):
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        candle = {
            "ts": T0 + i * interval_ms,
            "open": prev,
            "high": max(prev, c) + spread,
            "low": min(prev, c) - spread,
            "close": c,
            "volume": 100.0,
        }
        if extra:
            candle.update(extra)
        out.append(candle)
        prev = c
    return out


def wave_closes(n=600, base=100.0, amplitude=10.0, period=80):
    return [base + amplitude * math.sin(2 * math.pi * i / period) for i in range(n)]


class FakeSleeve(Sleeve):
    """Sleeve de test : signaux et politique de sortie fournis par le test —
    découple les tests du moteur de toute stratégie réelle."""

    name = "fake"
    timeframes = ("15m",)

    def __init__(self, sig_bars=None, tp_atr=2.0, sl_atr=10.0, time_exit=None):
        self._sig_bars = dict(sig_bars or {})
        self._policy = ExitPolicy(tp_atr=tp_atr, sl_atr=sl_atr, atr_len=14,
                                  time_exit_bars=time_exit)

    def grid(self):
        return [{"id": 0}]

    def signals(self, candles, params):
        return [self._sig_bars.get(i, 0) for i in range(len(candles))]

    def exit_policy(self, params):
        return self._policy

    def params_to_dict(self, params):
        return dict(params)

    def params_from_dict(self, d):
        return dict(d)

    def warmup_bars(self, params):
        return 20


def mk_result(n, pnl, pf, wr=0.5):
    return BacktestResult(params=None, n_trades=n, total_pnl_pct=pnl,
                          winrate=wr, profit_factor=pf, max_drawdown_pct=0.0)


# ── Sleeve B — grille et garde-fous ──────────────────────────────────────────

def test_grid_is_120_sets_all_trend_ema_fixed():
    sleeve = AdaptiveEMASleeve()
    grid = sleeve.grid()
    assert len(grid) == 120
    assert all(p.trend_ema == config.TREND_EMA_FIXED for p in grid)
    assert all(p.ema_slow >= 2 * p.ema_fast for p in grid)


def test_params_from_dict_forces_trend_ema():
    """Un best_params.json ancien/trafiqué ne peut pas désactiver le filtre."""
    sleeve = AdaptiveEMASleeve()
    p = sleeve.params_from_dict(
        {"ema_fast": 9, "ema_slow": 26, "tp_atr": 2.5, "sl_atr": 1.5, "trend_ema": 0})
    assert p.trend_ema == config.TREND_EMA_FIXED


# ── Backtester unifié ────────────────────────────────────────────────────────

def _flat_then_dip(n=60, base=100.0):
    """Bougies plates ; la bougie 31 plonge sous le close de 30 (fill maker)
    puis remonte — construite pour tester le modèle de fill."""
    closes = [base] * n
    candles = mk_candles(closes, spread=0.2)
    return candles


def test_backtester_maker_mode():
    """Fill maker quand la bougie d'exécution perce le limit : entrée au close
    du signal (meilleure que l'open) et frais maker → PnL >= taker, mêmes trades."""
    candles = _flat_then_dip()
    sig_bar = 30
    lim = candles[sig_bar]["close"]           # 100.0
    ex = candles[sig_bar + 1]
    ex["open"] = lim * 1.004                  # ouvre au-dessus → pas de cross
    ex["low"] = lim * 0.997                   # perce le limit → fill maker mid-bar
    ex["high"] = lim * 1.005
    ex["close"] = lim * 1.002
    # sortie propre plus tard : monte au TP
    for c in candles[sig_bar + 2:]:
        c["open"] = c["close"] = lim * 1.01
        c["high"] = lim * 1.20
        c["low"] = lim * 1.005

    sleeve = FakeSleeve(sig_bars={sig_bar: 1}, tp_atr=2.0, sl_atr=50.0)
    maker = run_sleeve_backtest(sleeve, candles, {"id": 0}, entry_mode="maker")
    taker = run_sleeve_backtest(sleeve, candles, {"id": 0}, entry_mode="taker")

    assert maker.n_trades == taker.n_trades == 1
    assert maker.trades[0]["entry"] == pytest.approx(lim)          # au limit
    assert taker.trades[0]["entry"] == pytest.approx(ex["open"])   # à l'open
    assert maker.total_pnl_pct > taker.total_pnl_pct


def test_backtester_maker_no_same_bar_tp_on_midbar_fill():
    """Fill mid-bar → TP interdit dans la même bougie même si le high le touche
    (anti-mirage R&D 07/2026). La sortie doit arriver sur une bougie ultérieure."""
    candles = _flat_then_dip()
    sig_bar = 30
    lim = candles[sig_bar]["close"]
    ex = candles[sig_bar + 1]
    ex["open"] = lim * 1.004
    ex["low"] = lim * 0.997      # fill maker mid-bar
    ex["high"] = lim * 1.50      # TP largement touchable — doit être ignoré
    ex["close"] = lim * 1.001
    for c in candles[sig_bar + 2:]:                       # plat ensuite
        c["open"] = c["close"] = lim
        c["high"] = lim * 1.001
        c["low"] = lim * 0.999

    sleeve = FakeSleeve(sig_bars={sig_bar: 1}, tp_atr=1.0, sl_atr=50.0)
    res = run_sleeve_backtest(sleeve, candles, {"id": 0}, entry_mode="maker")
    tr = res.trades[0]
    assert not (tr["reason"] == "TP" and tr["exit_bar"] == tr["entry_bar"])


def test_backtester_no_tp_policy_never_takes_profit():
    """tp_atr=None (momentum) : aucune sortie TP même sur un spike énorme."""
    candles = _flat_then_dip(n=80)
    sig_bar = 30
    for c in candles[sig_bar + 1:]:
        c["high"] = c["close"] * 2.0          # TP touchable s'il existait
    sleeve = FakeSleeve(sig_bars={sig_bar: 1}, tp_atr=None, sl_atr=50.0,
                        time_exit=10)
    res = run_sleeve_backtest(sleeve, candles, {"id": 0})
    assert res.n_trades == 1
    assert res.trades[0]["reason"] == "TIME"   # time-exit, jamais TP


def test_backtester_time_exit_bar_count():
    candles = _flat_then_dip(n=80)
    sig_bar = 30
    sleeve = FakeSleeve(sig_bars={sig_bar: 1}, tp_atr=None, sl_atr=50.0,
                        time_exit=12)
    res = run_sleeve_backtest(sleeve, candles, {"id": 0})
    tr = res.trades[0]
    assert tr["reason"] == "TIME"
    assert tr["exit_bar"] - tr["entry_bar"] == 12


def test_backtester_sl_priority_pessimistic():
    """TP et SL touchables dans la même bougie → SL retenu (pessimiste)."""
    candles = _flat_then_dip(n=60)
    sig_bar = 30
    big = candles[sig_bar + 2]
    big["high"] = 1000.0
    big["low"] = 1.0
    sleeve = FakeSleeve(sig_bars={sig_bar: 1}, tp_atr=2.0, sl_atr=2.0)
    res = run_sleeve_backtest(sleeve, candles, {"id": 0}, entry_mode="taker")
    assert res.trades[0]["reason"] == "SL"
    assert res.trades[0]["pnl_pct"] < 0


# ── Walk-forward — anti-overfit ──────────────────────────────────────────────

def _fake_backtester(train_map, valid_map, default=(0, 0.0, 0.0)):
    """backtest_fn injectable : train (start_index=0) et valid (start_index>0)
    servis depuis des tables params->métriques."""
    def fn(sleeve, candles, params, start_index=0, **kw):
        key = (params.ema_fast, params.ema_slow, params.tp_atr, params.sl_atr)
        table = train_map if start_index == 0 else valid_map
        n, pnl, pf = table.get(key, default)
        return mk_result(n, pnl, pf)
    return fn


def test_walk_forward_no_overfit_selection(tmp_path):
    """RÈGLE ABSOLUE : le PREMIER set du classement TRAIN qui confirme gagne —
    jamais le meilleur PnL de validation. B a une validation spectaculaire
    (PnL ×50, PF 5.0) mais A est devant au train et confirme : A doit gagner."""
    sleeve = AdaptiveEMASleeve()
    grid = sleeve.grid()
    A = grid[0]
    B = grid[1]
    kA = (A.ema_fast, A.ema_slow, A.tp_atr, A.sl_atr)
    kB = (B.ema_fast, B.ema_slow, B.tp_atr, B.sl_atr)
    train = {kA: (20, 0.30, 1.8), kB: (20, 0.20, 1.6)}   # A devant au train
    valid = {kA: (8, 0.01, 1.25),                          # A confirme (juste)
             kB: (30, 0.50, 5.00)}                         # B = jackpot valid
    opt = SuperOptimizer(symbols=["X"], backtest_fn=_fake_backtester(train, valid),
                         state_file=tmp_path / "bp.json")
    candles = mk_candles(wave_closes(n=600))
    w = opt.optimize_tf(sleeve, candles)
    assert w is not None
    got = (w["params"]["ema_fast"], w["params"]["ema_slow"],
           w["params"]["tp_atr"], w["params"]["sl_atr"])
    assert got == kA, "sélection sur le PnL de validation détectée (overfit) !"


def test_walk_forward_falls_through_to_next_confirming(tmp_path):
    """Si le n°1 du train ÉCHOUE en validation, on descend au suivant qui
    confirme — sans jamais comparer les PnL de validation entre eux."""
    sleeve = AdaptiveEMASleeve()
    grid = sleeve.grid()
    A, B = grid[0], grid[1]
    kA = (A.ema_fast, A.ema_slow, A.tp_atr, A.sl_atr)
    kB = (B.ema_fast, B.ema_slow, B.tp_atr, B.sl_atr)
    train = {kA: (20, 0.30, 1.8), kB: (20, 0.20, 1.6)}
    valid = {kA: (8, -0.05, 0.7),        # A ne confirme pas
             kB: (8, 0.02, 1.30)}        # B confirme
    opt = SuperOptimizer(symbols=["X"], backtest_fn=_fake_backtester(train, valid),
                         state_file=tmp_path / "bp.json")
    w = opt.optimize_tf(sleeve, mk_candles(wave_closes(n=600)))
    got = (w["params"]["ema_fast"], w["params"]["ema_slow"],
           w["params"]["tp_atr"], w["params"]["sl_atr"])
    assert got == kB


def test_multi_tf_picks_best_interval(tmp_path):
    """L'optimiseur teste 15m ET 1h ; le TF dont le set retenu a le meilleur
    score composite TRAIN est publié (ici le 1h, nettement meilleur en train
    malgré une validation 15m plus flatteuse — anti-snooping)."""
    sleeve = AdaptiveEMASleeve()

    def fetch(symbol, tf, days, **kw):
        return mk_candles(wave_closes(n=600),
                          interval_ms=config.INTERVAL_MS[tf], extra={"tf": tf})

    def bt(sleeve_, candles, params, start_index=0, **kw):
        tf = candles[0].get("tf", "15m")
        if start_index == 0:   # train : 1h écrase le 15m
            return mk_result(20, 0.40, 2.0) if tf == "1h" else mk_result(20, 0.10, 1.3)
        # valid : les deux confirment, le 15m a le PnL valid le plus haut (piège)
        return mk_result(8, 0.03, 1.3) if tf == "1h" else mk_result(8, 0.60, 4.0)

    opt = SuperOptimizer(symbols=["X"], fetch=fetch, backtest_fn=bt,
                         state_file=tmp_path / "bp.json")
    entry = opt.optimize_symbol("X", sleeve)
    assert entry["active"] is True
    assert entry["timeframe"] == "1h"
    assert set(entry["tf_candidates"]) == {"15m", "1h"}


def test_optimizer_publishes_schema(tmp_path):
    """run_once écrit un best_params.json atomique au schéma SPEC §3B
    (sleeve, timeframe, params, train, valid) + historique JSONL."""
    sleeve = AdaptiveEMASleeve()

    def fetch(symbol, tf, days, **kw):
        return mk_candles(wave_closes(n=600),
                          interval_ms=config.INTERVAL_MS[tf], extra={"tf": tf})

    def bt(sleeve_, candles, params, start_index=0, **kw):
        if start_index == 0:
            return mk_result(20, 0.30, 1.8, wr=0.55)
        return mk_result(10, 0.05, 1.6, wr=0.50)

    hist = tmp_path / "hist.jsonl"
    import unittest.mock as um
    with um.patch.object(config, "OPTIMIZER_HISTORY_FILE", hist):
        opt = SuperOptimizer(symbols=["AAA", "BBB"], fetch=fetch, backtest_fn=bt,
                             state_file=tmp_path / "bp.json")
        state = opt.run_once()

    on_disk = json.loads((tmp_path / "bp.json").read_text())
    assert on_disk["symbols"].keys() == {"AAA", "BBB"}
    entry = on_disk["symbols"]["AAA"]
    assert entry["active"] is True
    assert entry["sleeve"] == "adaptive_ema"
    assert entry["timeframe"] in ("15m", "1h")
    assert {"ema_fast", "ema_slow", "tp_atr", "sl_atr"} <= set(entry["params"])
    assert entry["valid"]["profit_factor"] >= config.MIN_VALID_PF
    assert hist.exists() and len(hist.read_text().strip().splitlines()) == 1
    assert state["entry_mode"] == config.ENTRY_MODE


# ── Filtre qualité ───────────────────────────────────────────────────────────

def _entry(pf_v, pnl_v, wr_v, pf_t=1.5, pnl_t=0.05, n_v=10):
    return {
        "active": True,
        "sleeve": "adaptive_ema",
        "timeframe": "15m",
        "params": {},
        "train": {"profit_factor": pf_t, "total_pnl_pct": pnl_t},
        "valid": {"profit_factor": pf_v, "total_pnl_pct": pnl_v,
                  "winrate": wr_v, "n_trades": n_v},
    }


def test_quality_filter_demotes_weak_symbols():
    result = apply_symbol_filter({
        "GOOD": _entry(1.8, 0.06, 0.55),
        "WEAK_PF": _entry(1.1, 0.06, 0.55),        # PF valid < 1.4
        "WEAK_PNL": _entry(1.8, 0.005, 0.55),      # PnL valid < 2%
        "WEAK_WR": _entry(1.8, 0.06, 0.30),        # WR < 40%
        "WEAK_TRAIN": _entry(1.8, 0.06, 0.55, pf_t=0.9),
    })
    assert result["GOOD"]["active"] is True
    for sym, why in (("WEAK_PF", "valid_pf"), ("WEAK_PNL", "valid_pnl"),
                     ("WEAK_WR", "valid_wr"), ("WEAK_TRAIN", "train_pf")):
        assert result[sym]["active"] is False
        assert result[sym]["filter_reason"].startswith(why)


def test_quality_filter_caps_top_n(monkeypatch):
    monkeypatch.setattr(config, "MAX_ACTIVE_SYMBOLS", 2)
    entries = {f"S{i}": _entry(1.5 + i * 0.2, 0.05, 0.55) for i in range(5)}
    result = apply_symbol_filter(entries)
    actives = [s for s, e in result.items() if e["active"]]
    assert len(actives) == 2
    # les 2 meilleurs scores survivent
    scores = {s: quality_score(e) for s, e in entries.items()}
    best2 = sorted(scores, key=scores.get, reverse=True)[:2]
    assert set(actives) == set(best2)
    demoted = [s for s, e in result.items() if e.get("filter_reason") == "cap_top_2"]
    assert len(demoted) == 3


# ── Config ───────────────────────────────────────────────────────────────────

def test_sleeve_allocations_sum_to_one():
    assert (config.MOMENTUM_ALLOC + config.EMA_ALLOC
            + config.BREAKOUT_ALLOC) == pytest.approx(1.0)


def test_train_composite_blind_to_valid():
    """Le score de choix du TF ne lit QUE le train."""
    m = {"profit_factor": 2.0, "total_pnl_pct": 0.10, "winrate": 0.5, "n_trades": 20}
    s1 = train_composite(m)
    s2 = train_composite({**m, "valid": {"total_pnl_pct": 99.0}})
    assert s1 == s2 > 0

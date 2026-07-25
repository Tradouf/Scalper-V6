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


@pytest.fixture(autouse=True)
def _isolate_superbot_state(tmp_path, monkeypatch):
    """HYGIÈNE OBLIGATOIRE : aucun test ne touche superbot/state/ réel.

    Incident 2026-07-12 17:41 : un test construisant SuperOptimizer sans engine
    isolé a entraîné le HMM marché sur ses vagues synthétiques, ÉCRASÉ le vrai
    market.pkl, créé AAA/BBB.pkl et PRUNÉ les 8 vrais modèles symboles pendant
    que le bot tournait. Cette fixture rend la récidive impossible."""
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "HMM_DIR", tmp_path / "state" / "hmm")
    monkeypatch.setattr(config, "BEST_PARAMS_FILE", tmp_path / "state" / "bp.json")
    monkeypatch.setattr(config, "OPTIMIZER_HISTORY_FILE",
                        tmp_path / "state" / "hist.jsonl")
    monkeypatch.setattr(config, "LIVE_STATE_FILE",
                        tmp_path / "state" / "live_state.json")
    monkeypatch.setattr(config, "REGIME_MARKET_FILE",
                        tmp_path / "state" / "regime_market.json")
    monkeypatch.setattr(config, "REGIME_SYMBOLS_FILE",
                        tmp_path / "state" / "regime_symbols.json")


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


# ═════════════════════════ PHASE 2 — HMM + double gate ═══════════════════════

import random

from superbot.hmm import (HMMRegimeEngine, MARKET_LABELS, SYMBOL_LABELS,
                          _label_symbol)
from superbot.markov import compute_latent_state, markov_transition_stats
from superbot.orchestrator import (Orchestrator, allow_entry,
                                   effective_margin_pct, hmm_size_mult,
                                   market_size_mult, prioritize_candidates,
                                   sleeve_allowed)
from superbot.regime import RegimeFacade, fallback_symbol_state


def regime_candles(segments, interval_ms=14_400_000, base=100.0, seed=42):
    """Bougies synthétiques par segments de régime : (n, drift/bar, bruit, vol).
    Régimes bien séparés → le GaussianHMM converge de façon déterministe."""
    rng = random.Random(seed)
    price = base
    out = []
    ts = T0
    for (n, drift, noise, vol) in segments:
        for _ in range(n):
            step = drift + rng.gauss(0, noise)
            new_price = max(price * (1 + step), 1e-6)
            hi = max(price, new_price) * (1 + noise)
            lo = min(price, new_price) * (1 - noise)
            out.append({"ts": ts, "open": price, "high": hi, "low": lo,
                        "close": new_price, "volume": vol * rng.uniform(0.8, 1.2)})
            price = new_price
            ts += interval_ms
    return out


def market_cycle(reps=6, n=60):
    """bull / bear / range / chaos répétés — les 4 régimes présents dans le
    train ET la validation (walk-forward 70/30 sans shift de distribution)."""
    segs = []
    for _ in range(reps):
        segs += [
            (n, +0.004, 0.002, 100.0),    # bull_orderly
            (n, -0.004, 0.002, 100.0),    # bear_orderly
            (n, 0.0, 0.0004, 60.0),       # range_compressed
            (n, 0.0, 0.02, 300.0),        # high_vol_chaotic
        ]
    return regime_candles(segs)


def symbol_cycle(reps=6, n=60, interval_ms=3_600_000):
    segs = []
    for _ in range(reps):
        segs += [
            (n, +0.005, 0.002, 120.0),    # trending_up
            (n, -0.005, 0.002, 120.0),    # trending_down
            (n, 0.0, 0.0005, 60.0),       # choppy
        ]
    return regime_candles(segs, interval_ms=interval_ms)


@pytest.fixture()
def hmm_engine(tmp_path):
    return HMMRegimeEngine(hmm_dir=tmp_path / "hmm")


# ── HMM marché ───────────────────────────────────────────────────────────────

def test_hmm_market_train_and_save(hmm_engine, tmp_path):
    fitted = hmm_engine.fit_market(market_cycle())
    assert fitted is not None
    assert (tmp_path / "hmm" / "market.pkl").exists()
    assert set(fitted.label_map.values()) == set(MARKET_LABELS)


def test_hmm_market_inference_confidence(hmm_engine):
    candles = market_cycle()
    assert hmm_engine.fit_market(candles) is not None
    out = hmm_engine.infer_market(candles)
    assert out["state"] in MARKET_LABELS
    assert 0.0 < out["confidence"] <= 1.0
    assert 0.0 <= out["transition_risk"] <= 1.0
    assert sum(out["state_probs"].values()) == pytest.approx(1.0, abs=0.01)
    assert out["source"] == "hmm"


def test_hmm_market_fallback_when_no_model(tmp_path):
    facade = RegimeFacade(engine=HMMRegimeEngine(hmm_dir=tmp_path / "vide"),
                          market_file=tmp_path / "rm.json",
                          symbols_file=tmp_path / "rs.json")
    out = facade.market_regime(market_cycle(reps=2))
    assert out["source"] == "fallback_adx"
    assert out["state"] in MARKET_LABELS


def test_hmm_market_hysteresis_blocks_flip():
    """Un flip de régime exige confiance + 2 observations consécutives : la
    première lecture opposée est retenue par l'hystérésis, la seconde bascule."""
    prev = {"state": "bull_orderly", "recent_raw": ["bull_orderly"]}
    raw = {"state": "bear_orderly", "confidence": 0.9, "transition_risk": 0.1}
    once = RegimeFacade._apply_hysteresis(raw, prev, min_conf=0.55, max_risk=0.45)
    assert once["state"] == "bull_orderly"          # retenu
    assert once.get("held_by_hysteresis") is True
    assert once["pending_state"] == "bear_orderly"
    twice = RegimeFacade._apply_hysteresis(raw, once, min_conf=0.55, max_risk=0.45)
    assert twice["state"] == "bear_orderly"         # 2ᵉ observation → bascule
    # transition_risk trop haut → jamais de bascule même répétée
    risky = {"state": "bear_orderly", "confidence": 0.9, "transition_risk": 0.6}
    held = RegimeFacade._apply_hysteresis(risky, twice | {"state": "bull_orderly"},
                                          min_conf=0.55, max_risk=0.45)
    assert held["state"] == "bull_orderly"


def test_hmm_market_walkforward_rejects_bad(hmm_engine, tmp_path):
    """Distribution radicalement différente en OOS (30 % finaux) → dégradation
    de log-vraisemblance → modèle REJETÉ, aucun .pkl écrit."""
    calm = [(400, 0.0005, 0.001, 100.0), (400, -0.0005, 0.001, 100.0),
            (200, 0.0, 0.0006, 80.0)]
    shifted = [(430, 0.0, 0.08, 900.0)]              # 30 % finaux : chaos ×80
    candles = regime_candles(calm + shifted)
    assert hmm_engine.fit_market(candles) is None
    assert not (tmp_path / "hmm" / "market.pkl").exists()


# ── HMM par symbole ──────────────────────────────────────────────────────────

def test_hmm_symbol_train_per_active_symbol(tmp_path, hmm_engine):
    """SPEC §8 étapes 8-9 : run_once entraîne un .pkl par symbole ACTIF
    uniquement (au TF de sa sleeve) et prune les inactifs."""
    sleeve = AdaptiveEMASleeve()

    def fetch(symbol, tf, days, **kw):
        return symbol_cycle(interval_ms=config.INTERVAL_MS.get(tf, 3_600_000))

    def bt(sleeve_, candles, params, start_index=0, **kw):
        good = candles[0]["volume"] is not None     # tous bons…
        if start_index == 0:
            return mk_result(20, 0.30, 1.8, wr=0.55)
        # …mais seul ACTIVE confirme en validation
        return (mk_result(10, 0.05, 1.6, wr=0.50)
                if getattr(bt, "sym", "") == "ACTIVE" else mk_result(1, -0.1, 0.5))

    # bt a besoin de savoir le symbole → wrapper
    def bt_for(symbol):
        def inner(sleeve_, candles, params, start_index=0, **kw):
            if start_index == 0:
                return mk_result(20, 0.30, 1.8, wr=0.55)
            return (mk_result(10, 0.05, 1.6, wr=0.50)
                    if symbol["cur"] == "ACTIVE" else mk_result(1, -0.1, 0.5))
        return inner

    cur = {"cur": ""}
    opt = SuperOptimizer(symbols=["ACTIVE", "DEAD"], fetch=fetch,
                         backtest_fn=bt_for(cur),
                         state_file=tmp_path / "bp.json", hmm_engine=hmm_engine)
    orig = opt.optimize_symbol
    opt.optimize_symbol = lambda s, sl: (cur.__setitem__("cur", s) or orig(s, sl))
    # pkl périmé d'un symbole plus actif → doit être pruné
    hmm_engine.hmm_dir.mkdir(parents=True, exist_ok=True)
    (hmm_engine.hmm_dir / "OLD.pkl").write_bytes(b"stale")

    import unittest.mock as um
    with um.patch.object(config, "OPTIMIZER_HISTORY_FILE", tmp_path / "h.jsonl"):
        opt.run_once()

    assert (hmm_engine.hmm_dir / "ACTIVE.pkl").exists()
    assert not (hmm_engine.hmm_dir / "DEAD.pkl").exists()
    assert not (hmm_engine.hmm_dir / "OLD.pkl").exists()      # pruné


def test_hmm_symbol_label_mapping_by_centroids():
    """trending_up = centroïde de log-return MAX (jamais un index arbitraire)."""
    class StubModel:
        means_ = __import__("numpy").array([
            [0.0, 0.5, 0.1, 0.0, 1.0],     # ret ~0  → choppy
            [+2.0, 0.5, 0.6, 0.5, 1.0],    # ret max → trending_up
            [-2.0, 0.5, 0.6, -0.5, 1.0],   # ret min → trending_down
        ])
    import numpy as np
    labels = _label_symbol(StubModel(), np.zeros(5), np.ones(5))
    assert labels[1] == "trending_up"
    assert labels[2] == "trending_down"
    assert labels[0] == "choppy"


def test_hmm_symbol_inference_functional(hmm_engine):
    """Sur une fin de série en tendance haussière nette, l'état inféré est
    trending_up (mapping + inférence bout-en-bout)."""
    candles = symbol_cycle()
    assert hmm_engine.fit_symbol("SOL", candles, "1h") is not None
    up_tail = candles + regime_candles([(40, +0.005, 0.002, 120.0)],
                                       interval_ms=3_600_000, seed=7,
                                       base=candles[-1]["close"])[0:]
    out = hmm_engine.infer_symbol("SOL", up_tail)
    assert out["state"] == "trending_up"
    assert out["timeframe"] == "1h"


def test_hmm_symbol_blocks_long_in_trending_down():
    market = {"state": "bull_orderly", "confidence": 0.8, "transition_risk": 0.1}
    sym = {"state": "trending_down", "confidence": 0.7, "transition_risk": 0.1}
    ok, reason = allow_entry(+1, "adaptive_ema", market, sym)
    assert (ok, reason) == (False, "hmm_no_long")
    ok, reason = allow_entry(-1, "adaptive_ema", market, sym)
    assert (ok, reason) == (True, "ok")


def test_hmm_symbol_allows_choppy_when_confident():
    market = {"state": "bull_orderly", "confidence": 0.8, "transition_risk": 0.1}
    sym = {"state": "choppy", "confidence": 0.9, "transition_risk": 0.1}
    for sig in (+1, -1):
        ok, reason = allow_entry(sig, "adaptive_ema", market, sym)
        assert (ok, reason) == (True, "ok_choppy")
    low = {"state": "choppy", "confidence": 0.3, "transition_risk": 0.1}
    assert allow_entry(+1, "adaptive_ema", market, low) == (False, "hmm_choppy")


def test_hmm_symbol_fallback_when_no_pkl(tmp_path):
    facade = RegimeFacade(engine=HMMRegimeEngine(hmm_dir=tmp_path / "vide"),
                          market_file=tmp_path / "rm.json",
                          symbols_file=tmp_path / "rs.json")
    up = regime_candles([(120, +0.005, 0.002, 120.0)], interval_ms=3_600_000)
    out = facade.symbol_regime("SOL", up, "1h")
    assert out["source"] == "fallback_adx"
    assert out["state"] == "trending_up"           # ADX fort + close > EMA50
    assert out["allowed"] == {"long": True, "short": False}
    # persistance pour le dashboard
    saved = json.loads((tmp_path / "rs.json").read_text())
    assert saved["SOL"]["state"] == "trending_up"


def test_hmm_symbol_prune_stale_pkls(hmm_engine):
    hmm_engine.hmm_dir.mkdir(parents=True, exist_ok=True)
    for name in ("market", "SOL", "XPL", "DEAD1", "DEAD2"):
        (hmm_engine.hmm_dir / f"{name}.pkl").write_bytes(b"x")
    removed = hmm_engine.prune_stale({"SOL", "XPL"})
    assert sorted(removed) == ["DEAD1", "DEAD2"]
    assert (hmm_engine.hmm_dir / "market.pkl").exists()      # jamais pruné
    assert (hmm_engine.hmm_dir / "SOL.pkl").exists()


def test_hmm_symbol_sizing_scales_with_confidence():
    up_conf = {"state": "trending_up", "confidence": 0.8}
    assert hmm_size_mult(+1, up_conf) == pytest.approx(0.8)
    assert hmm_size_mult(-1, up_conf) == 0.0
    choppy = {"state": "choppy", "confidence": 0.99}
    assert hmm_size_mult(+1, choppy) == pytest.approx(config.HMM_CHOPPY_SIZE_MULT * 0.99)
    # margin effectif = base × (dir×conf) × mult marché
    market = {"state": "range_compressed"}
    eff = effective_margin_pct(0.04, +1, market, up_conf)
    assert eff == pytest.approx(0.04 * 0.8 * 0.7)
    assert market_size_mult({"state": "high_vol_chaotic"}) == 0.5


# ── Markov + orchestrateur ───────────────────────────────────────────────────

def test_markov_transition_probs():
    states = ["bull"] * 8 + ["bear"] * 2 + ["bull"] * 10
    stats = markov_transition_stats(states, "bull")
    assert stats["stay_probability"] > 0.7
    assert stats["stay_probability"] + stats["switch_probability"] == pytest.approx(1.0)
    latent = compute_latent_state("bull", 0.9, states, previous_latent="bull")
    assert 0.0 <= latent["transition_risk"] <= 1.0
    # peu confiant + instable → inertie sur l'état précédent
    unstable = ["bull", "bear"] * 10
    held = compute_latent_state("bear", 0.1, unstable, previous_latent="bull")
    assert held["latent_state"] == "bull"


def test_orchestrator_double_gate_market_and_symbol():
    """Gate 1 : range_compressed coupe momentum/breakout mais pas adaptive_ema ;
    gate 2 : le symbole doit ÊTRE en tendance dans le sens du signal."""
    market = {"state": "range_compressed", "confidence": 0.8, "transition_risk": 0.1}
    sym_up = {"state": "trending_up", "confidence": 0.7, "transition_risk": 0.1}
    assert allow_entry(+1, "momentum", market, sym_up) == (False, "sleeve_blocked")
    assert allow_entry(+1, "breakout", market, sym_up) == (False, "sleeve_blocked")
    assert allow_entry(+1, "adaptive_ema", market, sym_up) == (True, "ok")
    assert sleeve_allowed("breakout", "high_vol_chaotic") is False
    assert sleeve_allowed("momentum", "high_vol_chaotic") is True


def test_orchestrator_freezes_on_high_transition_risk():
    sym = {"state": "trending_up", "confidence": 0.7, "transition_risk": 0.1}
    mkt_ok = {"state": "bull_orderly", "confidence": 0.8, "transition_risk": 0.1}
    mkt_hot = dict(mkt_ok, transition_risk=0.6)
    assert allow_entry(+1, "adaptive_ema", mkt_hot, sym) == (False, "market_transition")
    sym_hot = dict(sym, transition_risk=0.6)
    assert allow_entry(+1, "adaptive_ema", mkt_ok, sym_hot) == (False, "symbol_transition")


def test_orchestrator_allows_long_only_when_trending_up():
    market = {"state": "bull_orderly", "confidence": 0.8, "transition_risk": 0.1}
    sym = {"state": "trending_up", "confidence": 0.7, "transition_risk": 0.1}
    assert allow_entry(+1, "adaptive_ema", market, sym) == (True, "ok")
    assert allow_entry(-1, "adaptive_ema", market, sym) == (False, "hmm_no_short")
    low_conf = dict(sym, confidence=0.3)
    assert allow_entry(+1, "adaptive_ema", market, low_conf) == (False, "hmm_low_conf")


# ═════════════════════════ PHASE 3 — Sleeves A + C ═══════════════════════════

from superbot.sleeves import all_sleeves, get_sleeve
from superbot.sleeves.breakout import BreakoutParams, BreakoutSleeve
from superbot.sleeves.momentum import MomentumParams, MomentumSleeve
from superbot.orchestrator import sleeve_alloc, sleeve_capacity_left


def trend_candles(n=120, drift=0.003, interval_ms=14_400_000, base=100.0):
    closes = [base * (1 + drift) ** i for i in range(n)]
    return mk_candles(closes, interval_ms=interval_ms, spread=0.1)


# ── Sleeve A — Momentum ──────────────────────────────────────────────────────

def test_momentum_no_tp():
    """SPEC §3A : AUCUN take-profit, figé. La policy n'en a pas et un spike
    énorme ne produit jamais de sortie TP."""
    sleeve = MomentumSleeve()
    p = MomentumParams()
    assert sleeve.exit_policy(p).tp_atr is None
    candles = trend_candles(n=140)                      # ROC 48h >> +2% partout
    for c in candles[40:]:
        c["high"] = c["close"] * 3.0                    # TP touchable s'il existait
    res = run_sleeve_backtest(sleeve, candles, p, entry_mode="taker")
    assert res.n_trades >= 1
    assert all(t["reason"] != "TP" for t in res.trades)


def test_momentum_signals_roc():
    sleeve = MomentumSleeve()
    p = MomentumParams()
    up = trend_candles(n=60, drift=+0.003)              # ROC12 ≈ +3.7% > 2%
    dn = trend_candles(n=60, drift=-0.003)
    flat = trend_candles(n=60, drift=0.0)
    assert sleeve.signals(up, p)[-1] == 1
    assert sleeve.signals(dn, p)[-1] == -1
    assert sleeve.signals(flat, p)[-1] == 0


def test_momentum_time_exit_72_bars():
    sleeve = MomentumSleeve()
    p = MomentumParams()
    # tendance qui s'éteint (signal off) mais position tenue → time-exit à 72
    closes = [100 * 1.003 ** i for i in range(30)]
    closes += [closes[-1]] * 120                        # plat ensuite, SL intouché
    candles = mk_candles(closes, interval_ms=14_400_000, spread=0.01)
    res = run_sleeve_backtest(sleeve, candles, p, entry_mode="taker")
    times = [t for t in res.trades if t["reason"] == "TIME"]
    assert times and times[0]["exit_bar"] - times[0]["entry_bar"] == 72


def test_momentum_funding_filter():
    """SPEC §3A : pas de LONG si funding > +0.01%/h (on paierait la foule),
    pas de SHORT si funding < -0.01%/h, pas d'entrée si spread > 0.15%."""
    sleeve = MomentumSleeve()
    assert sleeve.allow_live_entry(+1, {"funding_hourly": 0.0002}) == (False, "funding_gate_long")
    assert sleeve.allow_live_entry(+1, {"funding_hourly": 0.00005}) == (True, "ok")
    assert sleeve.allow_live_entry(-1, {"funding_hourly": -0.0002}) == (False, "funding_gate_short")
    assert sleeve.allow_live_entry(-1, {"funding_hourly": 0.0002}) == (True, "ok")
    assert sleeve.allow_live_entry(+1, {"spread_pct": 0.002}) == (False, "spread_too_wide")
    assert sleeve.allow_live_entry(+1, {}) == (True, "ok")     # contexte absent = pas de blocage


def test_momentum_not_optimizable():
    """SPEC §8.4 : la sleeve A n'entre jamais dans le walk-forward."""
    from superbot.optimizer import optimizable_sleeves
    names = {s.name for s in optimizable_sleeves()}
    assert names == {"adaptive_ema", "breakout"}
    assert MomentumSleeve().optimizable is False
    assert len(MomentumSleeve().grid()) == 1                   # params figés


def test_backtester_funding_accrual():
    """SPEC §7 : le funding accru pendant la tenue pèse sur le PnL (long paie
    quand le taux est positif)."""
    sleeve = MomentumSleeve()
    p = MomentumParams()
    closes = [100 * 1.003 ** i for i in range(30)] + [100 * 1.003 ** 29] * 90
    candles = mk_candles(closes, interval_ms=14_400_000, spread=0.01)
    free = run_sleeve_backtest(sleeve, candles, p, entry_mode="taker")
    paying = run_sleeve_backtest(sleeve, candles, p, entry_mode="taker",
                                 funding_hourly=[0.0001] * len(candles))
    assert paying.total_pnl_pct < free.total_pnl_pct
    # ordre de grandeur : 72 bougies × 4h × 0.01%/h ≈ -2.88% sur le trade TIME
    diff = free.total_pnl_pct - paying.total_pnl_pct
    assert diff == pytest.approx(0.0001 * 4 * 72 * len(free.trades), rel=0.35)


# ── Sleeve C — Breakout ──────────────────────────────────────────────────────

def _breakout_series(direction=+1, n_flat=80, spike=0.04):
    """Base plate (ATR comprimé) puis cassure violente (expansion + breakout)."""
    rng = random.Random(7)
    closes = [100.0 + 0.05 * math.sin(i / 3.0) + rng.gauss(0, 0.02)
              for i in range(n_flat)]
    last = closes[-1]
    for i in range(1, 15):
        closes.append(last * (1 + direction * spike * i / 10))
    return mk_candles(closes, interval_ms=3_600_000, spread=0.05)


def test_breakout_donchian_signal():
    """LONG sur cassure du plus-haut Donchian AVEC expansion ATR ; SHORT en
    miroir ; base plate sans cassure → aucun signal."""
    sleeve = BreakoutSleeve()
    p = BreakoutParams(donchian_len=20, sl_atr=1.5, tp_atr=3.0)
    up = sleeve.signals(_breakout_series(+1), p)
    dn = sleeve.signals(_breakout_series(-1), p)
    flat = sleeve.signals(_breakout_series(+1, spike=0.0), p)
    assert any(s == 1 for s in up[80:])
    assert not any(s == -1 for s in up[80:])
    assert any(s == -1 for s in dn[80:])
    assert not any(s == 1 for s in flat)


def test_breakout_requires_atr_expansion():
    """Une cassure marginale SANS expansion de volatilité est ignorée : le
    filtre ATR>SMA(ATR,50) est structurel, hors grille."""
    sleeve = BreakoutSleeve()
    p = BreakoutParams(donchian_len=15)
    # dérive ultra-lente : nouveaux plus-hauts sans changement de volatilité
    closes = [100.0 * (1 + 0.00002) ** i + 0.05 * math.sin(i / 3.0)
              for i in range(200)]
    candles = mk_candles(closes, interval_ms=3_600_000, spread=0.05)
    sig = sleeve.signals(candles, p)
    assert sum(1 for s in sig if s != 0) <= 2      # quasi aucun signal


def test_breakout_grid_27_and_time_exit():
    sleeve = BreakoutSleeve()
    assert len(sleeve.grid()) == 27
    pol = sleeve.exit_policy(BreakoutParams())
    assert pol.time_exit_bars == 48 and pol.tp_atr == 3.0 and pol.sl_atr == 1.5
    # round-trip sérialisation
    p = BreakoutParams(donchian_len=30, sl_atr=2.0, tp_atr=4.0)
    assert sleeve.params_from_dict(sleeve.params_to_dict(p)) == p


# ── Arbitrage inter-sleeves (optimiseur) ─────────────────────────────────────

def test_sleeve_arbitration_by_train_composite(tmp_path):
    """Deux sleeves confirment sur le même symbole : la meilleure au COMPOSITE
    TRAIN gagne — même si l'autre a une validation plus flatteuse."""
    ema, brk = AdaptiveEMASleeve(), BreakoutSleeve()

    def fetch(symbol, tf, days, **kw):
        return mk_candles(wave_closes(n=600),
                          interval_ms=config.INTERVAL_MS[tf], extra={"tf": tf})

    def bt(sleeve_, candles, params, start_index=0, **kw):
        is_brk = isinstance(params, BreakoutParams)
        if start_index == 0:
            return mk_result(20, 0.50, 3.0) if is_brk else mk_result(20, 0.10, 1.3)
        # valid : les deux confirment, l'EMA a le meilleur PnL valid (piège)
        return mk_result(8, 0.03, 1.3) if is_brk else mk_result(8, 0.70, 5.0)

    opt = SuperOptimizer(symbols=["X"], fetch=fetch, backtest_fn=bt,
                         state_file=tmp_path / "bp.json", sleeves=[ema, brk])
    entry = opt.optimize_symbol_all_sleeves("X")
    assert entry["active"] is True
    assert entry["sleeve"] == "breakout"                      # meilleur TRAIN
    assert set(entry["sleeve_candidates"]) == {"adaptive_ema", "breakout"}


# ── Orchestrateur — gating par régime + allocations + caps ──────────────────

def test_orchestrator_regime_gating():
    """SPEC §11 Phase 3 : le régime marché route les sleeves — range coupe
    momentum et breakout, chaos coupe breakout seul ; les filtres live de la
    sleeve s'appliquent après la double gate."""
    sym_up = {"state": "trending_up", "confidence": 0.8, "transition_risk": 0.1}
    range_mkt = {"state": "range_compressed", "confidence": 0.9, "transition_risk": 0.1}
    chaos_mkt = {"state": "high_vol_chaotic", "confidence": 0.9, "transition_risk": 0.1}

    orch = Orchestrator()
    mk = lambda sleeve, **ctx: {"symbol": "S", "sleeve": sleeve, "signal": +1,
                                "quality_score": 1.0, "symbol_regime": sym_up, **ctx}

    # range : seule adaptive_ema passe
    got = orch.filter_entries([mk("momentum"), mk("adaptive_ema"), mk("breakout")],
                              market=range_mkt)
    assert [c["sleeve"] for c in got] == ["adaptive_ema"]
    # chaos : momentum + ema passent, breakout coupé, sizing marché ×0.5
    got = orch.filter_entries([mk("momentum"), mk("adaptive_ema"), mk("breakout")],
                              market=chaos_mkt)
    assert sorted(c["sleeve"] for c in got) == ["adaptive_ema", "momentum"]
    assert all(c["margin_mult"] == pytest.approx(0.8 * 0.5) for c in got)
    # filtre live sleeve : funding contre le long → momentum refusé même en bull
    bull = {"state": "bull_orderly", "confidence": 0.9, "transition_risk": 0.1}
    got = orch.filter_entries([mk("momentum", funding_hourly=0.0005)], market=bull)
    assert got == []


def test_orchestrator_sleeve_caps_and_alloc():
    assert sleeve_alloc("momentum") == pytest.approx(0.35)
    assert sleeve_alloc("adaptive_ema") == pytest.approx(0.45)
    assert sleeve_alloc("breakout") == pytest.approx(0.20)
    # cap par sleeve
    assert sleeve_capacity_left("momentum", {"momentum": 5}, 5) is True
    assert sleeve_capacity_left("momentum", {"momentum": 6}, 6) is False
    # cap total portefeuille
    assert sleeve_capacity_left("breakout", {"momentum": 6, "adaptive_ema": 4}, 10) is False
    # intra-cycle : un lot de candidats ne dépasse pas le cap de la sleeve
    bull = {"state": "bull_orderly", "confidence": 0.9, "transition_risk": 0.1}
    up = {"state": "trending_up", "confidence": 0.8, "transition_risk": 0.1}
    cands = [{"symbol": f"S{i}", "sleeve": "breakout", "signal": +1,
              "quality_score": 1.0, "symbol_regime": up} for i in range(5)]
    got = Orchestrator().filter_entries(cands, market=bull, open_by_sleeve={})
    assert len(got) == 3                                       # cap breakout = 3


def test_sleeve_registry():
    assert set(all_sleeves()) == {"momentum", "adaptive_ema", "breakout"}
    assert get_sleeve("momentum").name == "momentum"
    with pytest.raises(KeyError):
        get_sleeve("inexistante")


# ═════════════════════════ PHASE 4 — risk / live / run ═══════════════════════

from superbot.risk import (KillSwitch, direction_caps_ok, dynamic_margin_pct,
                           DAILY_PAUSE_SEC, DD_PAUSE_SEC)
from superbot.live_trader import SuperLiveTrader, _assert_third_wallet


def test_kill_switch_hysteresis():
    """-3% jour : 1ʳᵉ lecture sous seuil = confirmation, la 2ᵉ déclenche ;
    une lecture saine intercalée remet le compteur à zéro."""
    ks = KillSwitch()
    t0 = 1_800_000_000.0
    assert ks.check(1000.0, t0) is None                  # ancre du jour
    assert ks.check(960.0, t0 + 60) is None              # -4% → confirmation 1/2
    assert ks.check(995.0, t0 + 120) is None             # sain → reset
    assert ks.check(960.0, t0 + 180) is None             # 1/2 à nouveau
    d = ks.check(958.0, t0 + 240)                        # 2/2 → kill
    assert d is not None and d["pause_sec"] == DAILY_PAUSE_SEC
    assert "daily_loss" in d["reason"]


def test_kill_switch_portfolio_dd_7d():
    """-8% vs pic 7 j → pause 24 h (2 confirmations), même si la perte du jour
    est sous le seuil quotidien."""
    ks = KillSwitch()
    t0 = 1_800_000_000.0
    ks.check(1000.0, t0)                                 # pic 7j = 1000
    day2 = t0 + 86_400 + 1                               # nouveau jour → ancre 915
    assert ks.check(915.0, day2) is None                 # -8.5% vs pic : 1/2
    d = ks.check(915.0, day2 + 60)                       # 2/2 → kill DD
    assert d is not None and d["pause_sec"] == DD_PAUSE_SEC
    assert "portfolio_dd" in d["reason"]


def test_kill_switch_ignores_zero_equity():
    ks = KillSwitch()
    ks.check(1000.0, 1_800_000_000.0)
    assert ks.check(0.0, 1_800_000_060.0) is None        # lecture douteuse : jamais de kill


def test_dynamic_margin_and_direction_caps():
    scores = {"BEST": 10.0, "MID": 5.0, "LOW": 1.0}
    assert dynamic_margin_pct("BEST", scores) == pytest.approx(config.MARGIN_PCT_MAX)
    assert dynamic_margin_pct("LOW", scores) == pytest.approx(config.MARGIN_PCT)
    assert dynamic_margin_pct("ABSENT", scores) == pytest.approx(config.MARGIN_PCT)
    # corrélation : max 2 majors même direction, max 4 alts
    majors2 = {"BTC": {"dir": 1}, "ETH": {"dir": 1}}
    assert direction_caps_ok("SOL", 1, majors2) == (False, "majors_same_dir")
    assert direction_caps_ok("SOL", -1, majors2)[0] is True
    alts4 = {f"A{i}": {"dir": -1} for i in range(4)}
    assert direction_caps_ok("PEPE", -1, alts4) == (False, "alts_same_dir")
    assert direction_caps_ok("BTC", -1, alts4)[0] is True     # majors : autre groupe
    six = {f"S{i}": {"dir": 1} for i in range(6)}
    assert direction_caps_ok("BTC", 1, six) == (False, "max_same_direction")


def test_wallet_guard_refuses_hl_and_hl2(monkeypatch):
    """RÈGLE ABSOLUE 3 : wallet HL3 ≠ HL_* (V6) ≠ HL2_* (SimpleBot)."""
    monkeypatch.setenv("HL_PRIVATE_KEY", "0xAAA")
    monkeypatch.setenv("HL2_PRIVATE_KEY", "0xBBB")
    monkeypatch.setenv("HL2_ACCOUNT_ADDRESS", "0xADDR2")
    with pytest.raises(RuntimeError, match="HL_PRIVATE_KEY"):
        _assert_third_wallet("0xaaa", "0xNEUF")             # même clé que V6
    with pytest.raises(RuntimeError, match="HL2_PRIVATE_KEY"):
        _assert_third_wallet("0xBBB", "0xNEUF")             # même clé que SimpleBot
    with pytest.raises(RuntimeError, match="HL2_ACCOUNT_ADDRESS"):
        _assert_third_wallet("0xCCC", "0xaddr2")            # même adresse
    _assert_third_wallet("0xCCC", "0xNEUF")                 # distinct → OK


def _paper_trader(tmp_path, monkeypatch, best_symbols, fetch, market=None,
                  sym_regime=None):
    """SuperLiveTrader papier hermétique : best_params synthétique, régimes
    injectés (pas de réseau)."""
    monkeypatch.setattr(config, "LIVE_STATE_FILE", tmp_path / "live_state.json")
    bp = tmp_path / "bp.json"
    bp.write_text(json.dumps({"updated_at": "test", "symbols": best_symbols}))
    from superbot.live_trader import ParamStore

    class FakeRegime:
        def market_regime(self, candles, funding=None):
            return market or {"state": "bull_orderly", "confidence": 0.9,
                              "transition_risk": 0.1}
        def symbol_regime(self, symbol, candles, tf):
            return sym_regime or {"state": "trending_up", "confidence": 0.8,
                                  "transition_risk": 0.1,
                                  "allowed": {"long": True, "short": False}}

    monkeypatch.setattr(config, "FETCH_THROTTLE_SEC", 0.0)
    monkeypatch.setattr(config, "SYMBOLS", [])       # pas de sweep momentum
    t = SuperLiveTrader(store=ParamStore(bp), dry_run=True, fetch=fetch,
                        regime=FakeRegime(), funding_fetch=lambda: {})
    t._market = FakeRegime().market_regime(None)
    t._market_ts = time.time() + 10_000              # pas de refresh réseau
    t._funding_ts = time.time() + 10_000
    return t


import time


def _ema_entry(tf="1h"):
    return {"active": True, "sleeve": "adaptive_ema", "timeframe": tf,
            "params": {"ema_fast": 9, "ema_slow": 26, "tp_atr": 2.5,
                       "sl_atr": 1.5, "trend_ema": 200},
            "train": {"profit_factor": 2.0, "total_pnl_pct": 0.1},
            "valid": {"profit_factor": 1.8, "total_pnl_pct": 0.05,
                      "winrate": 0.6, "n_trades": 12}}


def _up_candles(tf_ms=3_600_000):
    """Série TRONQUÉE au premier cross EMA long (close>EMA200) : le signal
    tombe sur la DERNIÈRE bougie clôturée — condition d'entrée du live."""
    up = [100.0 + max(0, i - 260) * 0.08 + 0.4 * math.sin(i / 3.1)
          for i in range(360)]
    candles = mk_candles(up, interval_ms=tf_ms, spread=0.2)
    sleeve = AdaptiveEMASleeve()
    p = sleeve.params_from_dict(_ema_entry()["params"])
    sigs = sleeve.signals(candles, p)
    idx = [i for i, s in enumerate(sigs) if s == 1]
    assert idx, "série calibrée sans cross — revoir la pente"
    return candles[:idx[0] + 1]


def test_no_double_order_per_bar(tmp_path, monkeypatch):
    """SPEC §11 : une décision par bougie clôturée — deux ticks sur la même
    bougie n'ouvrent qu'une position (et zéro trade en double)."""
    candles = _up_candles()
    fetch = lambda s, i, d, **kw: candles
    t = _paper_trader(tmp_path, monkeypatch, {"UP": _ema_entry()}, fetch)
    t.tick()
    assert "UP" in t.state["positions"]                  # entrée ouverte
    t.tick()                                             # même bougie → rien de plus
    assert len(t.state["positions"]) == 1
    assert len(t.state["trades"]) == 0
    pos = t.state["positions"]["UP"]
    assert pos["sleeve"] == "adaptive_ema"
    assert pos["tp"] is not None and pos["sl"] < pos["entry"]


def test_paper_entry_blocked_by_double_gate(tmp_path, monkeypatch):
    """Symbole choppy faible confiance → gate refuse, aucune position papier."""
    candles = _up_candles()
    fetch = lambda s, i, d, **kw: candles
    t = _paper_trader(tmp_path, monkeypatch, {"UP": _ema_entry()}, fetch,
                      sym_regime={"state": "choppy", "confidence": 0.3,
                                  "transition_risk": 0.1})
    t.tick()
    assert t.state["positions"] == {}
    assert t.state["gate_stats"].get("hmm_choppy", 0) >= 1


def test_paper_exit_sl_and_equity(tmp_path, monkeypatch):
    """La position papier sort au SL sur la bougie suivante et l'equity
    papier encaisse la perte."""
    candles = _up_candles()
    feed = {"c": candles}
    fetch = lambda s, i, d, **kw: feed["c"]
    t = _paper_trader(tmp_path, monkeypatch, {"UP": _ema_entry()}, fetch)
    t.tick()
    assert "UP" in t.state["positions"]
    pos = t.state["positions"]["UP"]
    eq0 = t.state["equity"]
    last = feed["c"][-1]
    crash = {"ts": last["ts"] + 3_600_000, "open": pos["sl"] * 1.001,
             "high": pos["sl"] * 1.002, "low": pos["sl"] * 0.98,
             "close": pos["sl"] * 0.99, "volume": 100.0}
    feed["c"] = candles + [crash]
    t.tick()
    assert "UP" not in t.state["positions"]
    tr = t.state["trades"][-1]
    assert tr["reason"] == "SL" and tr["pnl_pct"] < 0
    assert t.state["equity"] < eq0


def test_single_instance_lock_superbot(tmp_path, monkeypatch):
    from superbot.run import acquire_single_instance_lock
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    first = acquire_single_instance_lock()
    assert first is not None
    assert acquire_single_instance_lock() is None
    first.close()


def test_orchestrator_filter_entries_prioritizes_and_sizes():
    market = {"state": "bull_orderly", "confidence": 0.8, "transition_risk": 0.1}
    cands = [
        {"symbol": "A", "sleeve": "adaptive_ema", "signal": +1, "quality_score": 1.0,
         "symbol_regime": {"state": "trending_up", "confidence": 0.9, "transition_risk": 0.1}},
        {"symbol": "B", "sleeve": "adaptive_ema", "signal": +1, "quality_score": 5.0,
         "symbol_regime": {"state": "trending_up", "confidence": 0.6, "transition_risk": 0.1}},
        {"symbol": "C", "sleeve": "adaptive_ema", "signal": +1, "quality_score": 9.0,
         "symbol_regime": {"state": "choppy", "confidence": 0.9, "transition_risk": 0.1}},
    ]
    accepted = Orchestrator().filter_entries(cands, market=market)
    assert [c["symbol"] for c in accepted] == ["C", "B", "A"]  # choppy confiant OK
    assert accepted[0]["margin_mult"] == pytest.approx(
        config.HMM_CHOPPY_SIZE_MULT * 0.9 * 1.0
    )
    ordered = prioritize_candidates(cands)
    assert ordered[0]["symbol"] == "C"      # priorisé par score×conf avant gate

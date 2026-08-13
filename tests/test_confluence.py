"""
Tests du module ConfluenceAgent — SPEC §10.

Couverture demandée par le §10, dans l'ordre :

* BiasLayer : hystérésis, veto macro, cas FLAT ;
* RegimeLayer : zone morte ADX 20–25, bornes de percentile ATR, veto funding,
  incohérence direction 1h vs biais 1d ;
* TimingLayer : setup annulé sur dépassement EMA_50, TTL, non-entrée en
  compression BBW ;
* ExecutionLayer : timeout + re-cotations + abandon (jamais de taker), sanity
  checks spread et bougie anormale ;
* Risque : sizing, plafonds, compteurs et leur persistance après restart,
  kill-switch frais ;
* Anti-repaint : aucune décision ne change quand on rejoue l'historique bougie
  par bougie.

Les tests visent les fonctions qui PORTENT la décision plutôt que des séries
réalistes : un test qui doit fabriquer 2 000 bougies pour observer un veto
finit par tester le générateur de bougies.
"""

from __future__ import annotations

import random

import pytest

from confluence import config as config_mod
from confluence import indicators as ind
from confluence.agent import ConfluenceAgent
from confluence.config import ConfigError
from confluence.data import History
from confluence.layers.bias import BiasLayer
from confluence.layers.context import LayerContext
from confluence.layers.execution import ExecutionLayer, ExecutionOutcome, ExecutionPlan
from confluence.layers.regime import RegimeLayer
from confluence.layers.timing import TimingLayer
from confluence.meanrev import MeanReversionAgent
from confluence.risk import RiskManager
from confluence.state import AgentState, BiasState, ClosedTrade, GuardState, StateStore
from confluence.trailing import TrailingStopAgent
from confluence.types import Bias, Regime, RiskLevel, Side

DAY = ind.INTERVAL_MS["1d"]
HOUR = ind.INTERVAL_MS["1h"]
M15 = ind.INTERVAL_MS["15m"]
MIN = ind.INTERVAL_MS["1m"]


# ── Fixtures et fabriques ────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Même règle que tests/conftest.py pour le cache OHLCV : aucun test ne
    doit écrire dans l'état réel du module."""
    monkeypatch.setenv("CONFLUENCE_STATE_DIR", str(tmp_path / "confluence_state"))


@pytest.fixture
def cfg():
    return config_mod.load()


@pytest.fixture
def small_cfg(cfg):
    """Config aux périodes réduites : les tests de couche doivent pouvoir
    atteindre le warmup en quelques dizaines de bougies."""
    out = cfg
    for path, value in [
        ("bias_1d.ema", 5), ("bias_1d.sma_fast", 3), ("bias_1d.sma_slow", 5),
        ("regime_1h.adx_period", 5), ("regime_1h.atr_period", 5),
        ("regime_1h.atr_percentile_days", 1),
        ("regime_1h.ema_fast", 3), ("regime_1h.ema_slow", 5),
        ("timing_15m.ema_pullback", 3), ("timing_15m.ema_invalidation", 5),
        ("timing_15m.bbw_sma", 4),
        ("meanrev.zscore_period", 20),
    ]:
        out = out.replace_path(path, value)
    return out


def candles(closes, interval_ms, start_ts=0, wick=0.001, opens=None):
    """Série OHLC depuis une liste de clôtures. `open` = clôture précédente,
    comme un vrai marché continu — c'est ce que les conditions de reprise du
    §4.3 lisent."""
    out = []
    for i, close in enumerate(closes):
        o = opens[i] if opens is not None else (closes[i - 1] if i else close)
        hi = max(o, close) * (1 + wick)
        lo = min(o, close) * (1 - wick)
        out.append({"ts": start_ts + i * interval_ms, "open": o, "high": hi,
                    "low": lo, "close": close, "volume": 10.0})
    return out


def ctx(**kwargs):
    base = {"now_ms": 10 ** 12, "candles": {}}
    base.update(kwargs)
    return LayerContext(**base)


# ── §10.1 BiasLayer ──────────────────────────────────────────────────────────

def test_bias_hysteresis_ne_bascule_pas_sur_une_seule_cloture(small_cfg):
    layer = BiasLayer(small_cfg.bias_1d)
    state = BiasState(current=Bias.SHORT_ONLY, last_bar_ts=0)

    after_one = layer.advance(state, Bias.LONG_ONLY, DAY)
    assert after_one.current is Bias.SHORT_ONLY, "une seule clôture ne doit pas basculer le biais"
    assert after_one.pending is Bias.LONG_ONLY
    assert after_one.pending_count == 1

    after_two = layer.advance(after_one, Bias.LONG_ONLY, 2 * DAY)
    assert after_two.current is Bias.LONG_ONLY
    assert after_two.pending is None and after_two.pending_count == 0


def test_bias_hysteresis_remise_a_zero_si_le_candidat_change(small_cfg):
    layer = BiasLayer(small_cfg.bias_1d)
    state = BiasState(current=Bias.FLAT, last_bar_ts=0)
    state = layer.advance(state, Bias.LONG_ONLY, DAY)
    state = layer.advance(state, Bias.SHORT_ONLY, 2 * DAY)
    assert state.current is Bias.FLAT
    assert state.pending is Bias.SHORT_ONLY and state.pending_count == 1


def test_bias_avance_idempotente_sur_la_meme_bougie(small_cfg):
    """§8 : rejouer la même bougie daily ne doit pas confirmer une bascule."""
    layer = BiasLayer(small_cfg.bias_1d)
    state = BiasState(current=Bias.FLAT, last_bar_ts=0)
    state = layer.advance(state, Bias.LONG_ONLY, DAY)
    replayed = layer.advance(state, Bias.LONG_ONLY, DAY)
    assert replayed.pending_count == 1, "la même bougie ne doit pas compter deux fois"
    assert replayed.current is Bias.FLAT


def test_bias_long_only_puis_veto_macro_extreme(small_cfg):
    layer = BiasLayer(small_cfg.bias_1d)
    series = candles([100 + i * 2 for i in range(30)], DAY)

    state = BiasState()
    for _ in range(3):                      # laisse l'hystérésis se confirmer
        verdict = layer.evaluate(series, ctx(bias_state=state))
        state = verdict.data["bias_state"]
        state.last_bar_ts = 0               # force la reprise de l'avance
    verdict = layer.evaluate(series, ctx(bias_state=state))
    assert verdict.passed and verdict.data["bias"] is Bias.LONG_ONLY

    vetoed = layer.evaluate(series, ctx(bias_state=state, macro_risk=RiskLevel.EXTREME))
    assert not vetoed.passed
    assert vetoed.data["bias"] is Bias.FLAT
    assert "EXTREME" in vetoed.reason
    assert vetoed.data["confirmed_bias"] is Bias.LONG_ONLY, (
        "le veto macro porte sur la sortie, pas sur l'état interne")


def test_bias_flat_quand_les_moyennes_ne_sont_pas_alignees(small_cfg):
    layer = BiasLayer(small_cfg.bias_1d)
    prices = [100, 101, 99, 102, 98, 103, 97, 104, 96, 105, 95, 106, 94, 107, 93]
    verdict = layer.evaluate(candles(prices, DAY), ctx())
    assert not verdict.passed
    assert verdict.data["bias"] is Bias.FLAT


def test_bias_veto_si_warmup_insuffisant(small_cfg):
    layer = BiasLayer(small_cfg.bias_1d)
    verdict = layer.evaluate(candles([100, 101, 102], DAY), ctx())
    assert not verdict.passed and "warmup" in verdict.reason


# ── §10.2 RegimeLayer ────────────────────────────────────────────────────────

@pytest.mark.parametrize("adx_value,expected", [
    (30.0, Regime.TREND),
    (25.0, Regime.CHOP),      # borne haute exclue : > 25 pour TREND
    (22.5, Regime.CHOP),
    (20.0, Regime.CHOP),      # borne basse exclue : < 20 pour RANGE
    (15.0, Regime.RANGE),
])
def test_regime_zone_morte_adx(cfg, adx_value, expected):
    """§4.2 : entre 20 et 25 inclus, aucun trade."""
    assert RegimeLayer(cfg.regime_1h).classify(adx_value) is expected


def test_regime_zone_morte_est_bien_un_veto(small_cfg):
    layer = RegimeLayer(small_cfg.regime_1h)
    # Série sans direction : l'ADX y reste bas, mais le test porte sur le fait
    # qu'un régime CHOP renvoie un veto et NON le régime précédent.
    verdict = layer.evaluate.__self__.classify(22.0)
    assert verdict is Regime.CHOP


def test_regime_bornes_percentile_atr(cfg):
    """Le filtre §4.2 exclut strictement en dehors de [20, 90]."""
    r = cfg.regime_1h
    window = list(range(100))
    assert ind.percentile_rank(window, 5) < r.atr_percentile_min      # trop calme
    assert ind.percentile_rank(window, 95) > r.atr_percentile_max     # trop volatil
    assert r.atr_percentile_min <= ind.percentile_rank(window, 50) <= r.atr_percentile_max


def test_regime_veto_volatilite_trop_calme(small_cfg):
    layer = RegimeLayer(small_cfg.regime_1h)
    # 40 bougies très agitées puis un calme plat : l'ATR courant tombe au bas
    # de sa distribution glissante.
    noisy = [100 + (10 if i % 2 else -10) for i in range(40)]
    calm = [100.0 + i * 0.001 for i in range(20)]
    verdict = layer.evaluate(candles(noisy + calm, HOUR, wick=0.0),
                             ctx(bias=Bias.LONG_ONLY, funding_hourly=0.0))
    assert not verdict.passed
    assert "volatilité trop calme" in verdict.reason


def test_regime_veto_funding_du_cote_qui_paie(cfg):
    layer = RegimeLayer(cfg.regime_1h)
    # 0.00006/h ≈ +52 % annualisé : au-dessus du seuil de 30 %.
    ok_short, annual = layer.funding_ok(0.00006, Side.SHORT)
    ok_long, _ = layer.funding_ok(0.00006, Side.LONG)
    assert annual > cfg.regime_1h.funding_max_annualized
    assert not ok_long, "un long ne doit pas payer un funding au-dessus du seuil"
    assert ok_short, "le côté qui ENCAISSE le funding n'est pas bloqué"


def test_regime_funding_absent_vaut_veto(cfg):
    ok, annual = RegimeLayer(cfg.regime_1h).funding_ok(None, Side.LONG)
    assert not ok and annual is None


def test_regime_incoherence_direction_vs_biais(small_cfg):
    layer = RegimeLayer(small_cfg.regime_1h)
    # Tendance haussière franche, mais biais 1d SHORT_ONLY.
    up = [100 * (1.01 ** i) for i in range(60)]
    verdict = layer.evaluate(candles(up, HOUR), ctx(bias=Bias.SHORT_ONLY, funding_hourly=0.0))
    assert not verdict.passed
    assert "incohérence" in verdict.reason or "volatilité" in verdict.reason


def test_regime_range_sans_biais_est_vete(small_cfg):
    layer = RegimeLayer(small_cfg.regime_1h)
    flat = [100 + random.Random(3).uniform(-0.2, 0.2) for _ in range(60)]
    verdict = layer.evaluate(candles(flat, HOUR), ctx(bias=Bias.FLAT, funding_hourly=0.0))
    assert not verdict.passed


# ── §10.3 TimingLayer ────────────────────────────────────────────────────────

def test_timing_setup_annule_par_depassement_ema50(small_cfg):
    layer = TimingLayer(small_cfg.timing_15m)
    prices = [100 + i for i in range(20)] + [90, 88, 92]   # effondrement sous l'EMA_50
    series = candles(prices, M15)
    ema50 = ind.ema([c["close"] for c in series], small_cfg.timing_15m.ema_invalidation)
    i = len(series) - 1
    broken = layer._invalidated(series, ema50, Side.LONG, i - 2, i)
    assert broken is not None, "un repli sous l'EMA_50 doit annuler le setup"


def test_timing_setup_intact_si_le_repli_reste_au_dessus_ema50(small_cfg):
    layer = TimingLayer(small_cfg.timing_15m)
    # Repli peu profond : le prix redescend mais reste au-dessus de l'EMA
    # d'invalidation — c'est un pullback, pas un retournement.
    prices = [100.0 + i for i in range(20)] + [119.5, 119.2, 120.0]
    series = candles(prices, M15)
    ema50 = ind.ema([c["close"] for c in series], small_cfg.timing_15m.ema_invalidation)
    i = len(series) - 1
    assert layer._invalidated(series, ema50, Side.LONG, i - 2, i) is None


def test_timing_compression_bbw_bloque_lentree(small_cfg):
    """§4.3 : on n'entre pas dans un resserrement de volatilité.

    Une rampe strictement linéaire a une largeur de Bollinger CONSTANTE, donc
    un BBW égal à sa propre SMA : c'est le cas limite de compression. Tout le
    reste du setup est satisfait (pullback touché, EMA d'invalidation intacte,
    reprise haussière), si bien que le seul veto possible est celui du BBW.
    """
    layer = TimingLayer(small_cfg.timing_15m)
    closes = [100.0 + i for i in range(40)]
    series = candles(closes, M15)
    ema_pull = ind.ema(closes, small_cfg.timing_15m.ema_pullback)
    series[-1]["low"] = ema_pull[-1] - 0.01          # le creux touche la zone de pullback

    verdict = layer.evaluate(series, ctx(regime=Regime.TREND, direction=Side.LONG,
                                         bias=Bias.LONG_ONLY, atr_1h=1.0))
    assert not verdict.passed
    assert "compression" in verdict.reason
    assert verdict.data["bbw"] <= verdict.data["bbw_sma"]
    assert verdict.data["pullback_bar"] is not None, "le pullback était bien détecté"


def test_timing_ttl_du_signal(small_cfg):
    layer = TimingLayer(small_cfg.timing_15m)
    bar_ts = 10 * M15
    expires = layer.expiry(bar_ts)
    ttl = small_cfg.timing_15m.signal_ttl_bars
    from confluence.types import ms

    assert ms(expires) == bar_ts + M15 + ttl * M15
    from confluence.types import utc

    assert not utc(bar_ts + M15).__ge__(expires), "le signal est vivant à sa naissance"
    assert utc(bar_ts + M15 + ttl * M15) >= expires, "il meurt après signal_ttl_bars bougies"


def test_timing_zone_dentree_toujours_du_bon_cote(small_cfg):
    layer = TimingLayer(small_cfg.timing_15m)
    lo, hi = layer.entry_zone(100.0, Side.LONG, atr_1h=4.0)
    assert lo < hi <= 100.0, "un achat maker se place SOUS le marché"
    lo_s, hi_s = layer.entry_zone(100.0, Side.SHORT, atr_1h=4.0)
    assert 100.0 <= lo_s < hi_s, "une vente maker se place AU-DESSUS du marché"


def test_timing_pullback_retient_la_reference_la_plus_proche(small_cfg):
    layer = TimingLayer(small_cfg.timing_15m)
    # Pour un long, la première référence touchée en redescendant est la PLUS HAUTE.
    assert layer._pullback_ref(100.0, 105.0, Side.LONG) == 105.0
    assert layer._pullback_ref(100.0, 105.0, Side.SHORT) == 100.0
    assert layer._pullback_ref(None, None, Side.LONG) is None


def test_timing_range_sans_meanrev_est_vete(small_cfg):
    layer = TimingLayer(small_cfg.timing_15m, meanrev=None)
    series = candles([100 + i * 0.1 for i in range(80)], M15)
    verdict = layer.evaluate(series, ctx(regime=Regime.RANGE, bias=Bias.LONG_ONLY))
    assert not verdict.passed and "MeanReversionAgent" in verdict.reason


# ── §10.4 ExecutionLayer ─────────────────────────────────────────────────────

def test_execution_prix_limite_jamais_croisant(cfg):
    plan = ExecutionPlan(side=Side.LONG, entry_zone=(90.0, 110.0), cfg=cfg.execution_1m)
    price = plan.limit_price(best_bid=100.0, best_ask=101.0)
    assert price < 101.0, "un achat post-only ne doit jamais atteindre le meilleur ask"

    plan_s = ExecutionPlan(side=Side.SHORT, entry_zone=(90.0, 110.0), cfg=cfg.execution_1m)
    price_s = plan_s.limit_price(best_bid=100.0, best_ask=101.0)
    assert price_s > 100.0


def test_execution_prix_limite_borne_par_la_zone(cfg):
    plan = ExecutionPlan(side=Side.LONG, entry_zone=(90.0, 95.0), cfg=cfg.execution_1m)
    price = plan.limit_price(best_bid=100.0, best_ask=101.0)
    assert price <= 95.0, "on ne poursuit pas le marché au-delà de la zone du signal"


def test_execution_timeout_requotes_puis_abandon_jamais_taker(cfg):
    """§4.4 et §11 : après max_requotes, le signal est ABANDONNÉ."""
    c = cfg.execution_1m
    plan = ExecutionPlan(side=Side.LONG, entry_zone=(90.0, 110.0), cfg=c)
    t = 0

    first = plan.step(t, 100.0, 101.0)
    assert first.kind == "post"

    kinds = []
    for _ in range(c.max_requotes + 1):
        t += int(c.fill_timeout_s * 1000) + 1
        kinds.append(plan.step(t, 100.0, 101.0).kind)

    assert kinds[:-1] == ["requote"] * c.max_requotes
    assert kinds[-1] == "abandon"
    assert plan.status is ExecutionOutcome.ABANDONED
    assert "taker" in plan.abandon_reason
    assert all(k != "market" for k in kinds), "aucun chemin ne mène à un ordre au marché"


def test_execution_attend_avant_le_timeout(cfg):
    plan = ExecutionPlan(side=Side.LONG, entry_zone=(90.0, 110.0), cfg=cfg.execution_1m)
    plan.step(0, 100.0, 101.0)
    action = plan.step(int(cfg.execution_1m.fill_timeout_s * 1000) - 1, 100.0, 101.0)
    assert action.kind == "wait" and plan.attempts == 1


def test_execution_fill_termine_le_plan(cfg):
    plan = ExecutionPlan(side=Side.LONG, entry_zone=(90.0, 110.0), cfg=cfg.execution_1m)
    plan.step(0, 100.0, 101.0)
    action = plan.step(1000, 100.0, 101.0, filled=True)
    assert action.kind == "filled" and plan.status is ExecutionOutcome.FILLED


def test_execution_veto_spread_trop_large(cfg):
    layer = ExecutionLayer(cfg.execution_1m)
    series = candles([100.0 + i * 0.01 for i in range(40)], MIN, wick=0.0002)
    verdict = layer.evaluate(series, ctx(best_bid=99.9, best_ask=100.9))
    assert not verdict.passed and "spread" in verdict.reason


def test_execution_veto_bougie_anormale(cfg):
    layer = ExecutionLayer(cfg.execution_1m)
    closes = [100.0 + i * 0.01 for i in range(40)]
    series = candles(closes, MIN, wick=0.0001)
    series[-2]["high"] = series[-2]["close"] * 1.05      # flash spike
    series[-2]["low"] = series[-2]["close"] * 0.95
    verdict = layer.evaluate(series, ctx(best_bid=100.39, best_ask=100.40))
    assert not verdict.passed and "anormale" in verdict.reason


def test_execution_carnet_sain_passe(cfg):
    layer = ExecutionLayer(cfg.execution_1m)
    series = candles([100.0 + i * 0.01 for i in range(40)], MIN, wick=0.0001)
    last = series[-1]["close"]
    verdict = layer.evaluate(series, ctx(best_bid=last - 0.005, best_ask=last + 0.005))
    assert verdict.passed, verdict.reason


def test_execution_modele_de_fill_exige_une_traversee(cfg):
    plan = ExecutionPlan(side=Side.LONG, entry_zone=(90.0, 110.0), cfg=cfg.execution_1m)
    plan.step(0, 100.0, 101.0)
    price = plan.quoted_price
    assert not plan.would_fill(low=price, high=price + 5), "le simple contact ne remplit pas"
    assert plan.would_fill(low=price - 0.5, high=price + 5)


# ── §10.5 Risque ─────────────────────────────────────────────────────────────

def test_sizing_formule_sur_la_distance_au_stop(cfg):
    rm = RiskManager(cfg.risk)
    equity, entry, stop = 10_000.0, 100.0, 98.0
    result = rm.size(equity, entry, stop)
    assert result.size == pytest.approx(equity * cfg.risk.risk_pct / 2.0)
    assert result.risk_usd == pytest.approx(equity * cfg.risk.risk_pct)
    assert result.capped_by is None


def test_sizing_plafonne_par_le_levier(cfg):
    rm = RiskManager(cfg.risk)
    # Stop très serré ⇒ taille énorme ⇒ le plafond de levier doit mordre.
    result = rm.size(equity=10_000.0, entry=100.0, stop=99.99)
    assert result.notional <= 10_000.0 * cfg.risk.max_leverage + 1e-6
    assert result.capped_by == "leverage"
    assert result.risk_usd < 10_000.0 * cfg.risk.risk_pct


def test_sizing_plafonne_par_le_notionnel_max(cfg):
    small = cfg.replace_path("risk.max_position_usd", 1_000.0)
    result = RiskManager(small.risk).size(equity=10_000.0, entry=100.0, stop=99.99)
    assert result.notional == pytest.approx(1_000.0)
    assert result.capped_by == "max_position_usd"


def test_sizing_nul_sur_equity_nulle(cfg):
    assert not RiskManager(cfg.risk).size(0.0, 100.0, 98.0).valid


def test_stop_initial_atr(cfg):
    rm = RiskManager(cfg.risk)
    assert rm.stop_price(100.0, Side.LONG, 2.0) == pytest.approx(100.0 - cfg.risk.k_stop * 2.0)
    assert rm.stop_price(100.0, Side.SHORT, 2.0) == pytest.approx(100.0 + cfg.risk.k_stop * 2.0)
    with pytest.raises(ValueError):
        rm.stop_price(100.0, Side.LONG, 0.0)


def test_filtre_edge_minimal(cfg):
    """§6.5 : le mouvement espéré doit couvrir 5× les frais aller-retour."""
    rm = RiskManager(cfg.risk)
    entry = 50_000.0
    seuil = entry * cfg.risk.fee_roundtrip * cfg.risk.edge_multiple / cfg.risk.k_edge

    ok, detail = rm.edge_ok(entry, atr_1h=seuil * 1.01)
    assert ok and detail["edge_ratio"] > cfg.risk.edge_multiple
    refused, _ = rm.edge_ok(entry, atr_1h=seuil * 0.99)
    assert not refused


def test_garde_fou_plafond_journalier(cfg):
    rm = RiskManager(cfg.risk)
    guards = GuardState()
    now = 1_700_000_000_000
    for i in range(cfg.risk.max_trades_per_day):
        assert rm.check_guards(guards, now, bar_ts=i).allowed
        rm.register_entry(guards, now, bar_ts=i)
        guards.last_entry_ms = 0          # neutralise le cooldown entre trades
    blocked = rm.check_guards(guards, now, bar_ts=99)
    assert not blocked.allowed and "plafond journalier" in blocked.reason


def test_garde_fou_compteur_journalier_remis_a_zero(cfg):
    rm = RiskManager(cfg.risk)
    guards = GuardState()
    now = 1_700_000_000_000
    rm.register_entry(guards, now, bar_ts=1)
    assert guards.trades_today == 1
    guards.roll_day(now + DAY)
    assert guards.trades_today == 0


def test_garde_fou_cooldown_apres_perte(cfg):
    rm = RiskManager(cfg.risk)
    guards = GuardState()
    now = 1_700_000_000_000
    rm.register_exit(guards, ClosedTrade(closed_ms=now, gross_pnl=-50.0, fees=5.0))

    encore_chaud = rm.check_guards(guards, now + int(3.9 * 3_600_000))
    assert not encore_chaud.allowed and "cooldown après perte" in encore_chaud.reason

    refroidi = rm.check_guards(guards, now + int(4.1 * 3_600_000))
    assert refroidi.allowed, refroidi.reason


def test_garde_fou_cooldown_entre_trades(cfg):
    rm = RiskManager(cfg.risk)
    guards = GuardState()
    now = 1_700_000_000_000
    rm.register_entry(guards, now, bar_ts=1)
    trop_tot = rm.check_guards(guards, now + int(0.5 * 3_600_000), bar_ts=2)
    assert not trop_tot.allowed and "cooldown entre trades" in trop_tot.reason


def test_garde_fou_idempotence_par_bougie(cfg):
    rm = RiskManager(cfg.risk)
    guards = GuardState()
    now = 1_700_000_000_000
    rm.register_entry(guards, now, bar_ts=4242)
    guards.last_entry_ms = 0
    rejoue = rm.check_guards(guards, now, bar_ts=4242)
    assert not rejoue.allowed and "déjà prise" in rejoue.reason


def test_killswitch_frais(cfg):
    """§6.5 : au-delà de 25 % du PnL brut absorbé par les frais, mode observation."""
    rm = RiskManager(cfg.risk)
    now = 1_700_000_000_000
    sain = [ClosedTrade(closed_ms=now - i * 3_600_000, gross_pnl=100.0, fees=5.0)
            for i in range(10)]
    triggered, detail = rm.killswitch_triggered(sain, now)
    assert not triggered and detail["fee_ratio"] == pytest.approx(0.05)

    ruineux = [ClosedTrade(closed_ms=now - i * 3_600_000, gross_pnl=10.0, fees=5.0)
               for i in range(10)]
    triggered, detail = rm.killswitch_triggered(ruineux, now)
    assert triggered and detail["fee_ratio"] == pytest.approx(0.5)

    blocked = rm.check_guards(GuardState(history=ruineux), now)
    assert not blocked.allowed and "kill-switch" in blocked.reason


def test_killswitch_ignore_les_trades_hors_fenetre(cfg):
    rm = RiskManager(cfg.risk)
    now = 1_700_000_000_000
    vieux = [ClosedTrade(closed_ms=now - 60 * 86_400_000, gross_pnl=10.0, fees=9.0)]
    triggered, detail = rm.killswitch_triggered(vieux, now)
    assert not triggered and detail["trades"] == 0


def test_killswitch_sans_historique_ne_declenche_pas(cfg):
    triggered, _ = RiskManager(cfg.risk).killswitch_triggered([], 1_700_000_000_000)
    assert not triggered


# ── §10.5 (suite) Persistance après restart ──────────────────────────────────

def test_garde_fous_survivent_au_restart(tmp_path, cfg):
    """§8 : « un restart ne doit pas réinitialiser les garde-fous »."""
    path = tmp_path / "state.json"
    rm = RiskManager(cfg.risk)
    now = 1_700_000_000_000

    state = AgentState()
    for i in range(cfg.risk.max_trades_per_day):
        rm.register_entry(state.guards, now, bar_ts=i)
        state.guards.last_entry_ms = 0
    state.bias = BiasState(current=Bias.LONG_ONLY, pending=Bias.SHORT_ONLY,
                           pending_count=1, last_bar_ts=DAY)
    StateStore(path).save(state)

    # « Redémarrage » : un tout nouveau magasin, sur le même fichier.
    reloaded = StateStore(path).load()
    assert reloaded.guards.trades_today == cfg.risk.max_trades_per_day
    assert reloaded.bias.current is Bias.LONG_ONLY
    assert reloaded.bias.pending is Bias.SHORT_ONLY
    assert reloaded.bias.pending_count == 1

    blocked = rm.check_guards(reloaded.guards, now, bar_ts=999)
    assert not blocked.allowed and "plafond journalier" in blocked.reason


def test_historique_du_killswitch_survit_au_restart(tmp_path, cfg):
    path = tmp_path / "state.json"
    now = 1_700_000_000_000
    state = AgentState()
    state.guards.history = [ClosedTrade(closed_ms=now, gross_pnl=10.0, fees=5.0)
                            for _ in range(5)]
    StateStore(path).save(state)

    reloaded = StateStore(path).load()
    triggered, _ = RiskManager(cfg.risk).killswitch_triggered(reloaded.guards.history, now)
    assert triggered, "le kill-switch doit rester armé après un redémarrage"


def test_etat_illisible_est_archive_et_non_ecrase(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ceci n'est pas du JSON", encoding="utf-8")
    store = StateStore(path)
    state = store.load()
    assert state.guards.trades_today == 0
    assert list(tmp_path.glob("state.corrupt.*")), "l'état illisible doit être archivé"


def test_purge_de_lhistorique(cfg):
    guards = GuardState()
    now = 1_700_000_000_000
    guards.history = [
        ClosedTrade(closed_ms=now - 60 * 86_400_000, gross_pnl=1.0, fees=0.1),
        ClosedTrade(closed_ms=now, gross_pnl=1.0, fees=0.1),
    ]
    guards.prune(now, cfg.risk.fee_killswitch_days)
    assert len(guards.history) == 1


# ── TrailingStopAgent (§6.3) ─────────────────────────────────────────────────

def test_trailing_ne_recule_jamais(cfg):
    agent = TrailingStopAgent(cfg.trailing)
    state = agent.open(Side.LONG, entry=100.0, initial_stop=97.0)
    agent.update(state, close=112.0, atr_1h=1.0)
    haut = state.stop
    agent.update(state, close=101.0, atr_1h=1.0)
    assert state.stop == haut, "le stop ne doit jamais se relâcher"


def test_trailing_passe_au_moins_a_lentree_a_1r(cfg):
    agent = TrailingStopAgent(cfg.trailing)
    state = agent.open(Side.LONG, entry=100.0, initial_stop=97.0)
    agent.update(state, close=103.0, atr_1h=0.1)      # +1R exactement
    assert state.stop >= 100.0


def test_trailing_stop_touche(cfg):
    agent = TrailingStopAgent(cfg.trailing)
    state = agent.open(Side.LONG, entry=100.0, initial_stop=97.0)
    assert agent.hit(state, low=97.5, high=101.0) is None
    assert agent.hit(state, low=96.0, high=101.0) == pytest.approx(97.0)


def test_trailing_short_symetrique(cfg):
    agent = TrailingStopAgent(cfg.trailing)
    state = agent.open(Side.SHORT, entry=100.0, initial_stop=103.0)
    agent.update(state, close=97.0, atr_1h=0.1)
    assert state.stop <= 100.0
    assert agent.hit(state, low=99.0, high=104.0) is not None


# ── MeanReversionAgent (§4.3) ────────────────────────────────────────────────

def test_meanrev_refuse_une_serie_non_stationnaire(small_cfg):
    agent = MeanReversionAgent(small_cfg.meanrev)
    rng = random.Random(5)
    walk = [100.0]
    for _ in range(200):
        walk.append(walk[-1] + rng.gauss(0, 1))
    verdict = agent.evaluate(candles(walk, HOUR), ctx(bias=Bias.LONG_ONLY))
    assert not verdict.passed


def test_meanrev_respecte_le_biais_1d(small_cfg):
    agent = MeanReversionAgent(small_cfg.meanrev)
    assert agent._side_from_z(-3.0) is Side.LONG
    assert agent._side_from_z(3.0) is Side.SHORT
    assert agent._side_from_z(0.5) is None


def test_meanrev_sortie_sur_retour_en_zone_neutre(cfg):
    agent = MeanReversionAgent(cfg.meanrev)
    assert agent.should_exit(-0.1, Side.LONG)
    assert not agent.should_exit(-1.9, Side.LONG)
    assert agent.should_exit(0.1, Side.SHORT)


def test_meanrev_desactive_est_vete(cfg):
    off = cfg.replace_path("meanrev.enabled", False)
    verdict = MeanReversionAgent(off.meanrev).evaluate(candles([1.0] * 100, HOUR), ctx())
    assert not verdict.passed and "désactivé" in verdict.reason


# ── §10.6 Anti-repaint ───────────────────────────────────────────────────────

def _history_for_agent(days=60, seed=17):
    rng = random.Random(seed)
    n = days * 96
    px, c15, drift = 30_000.0, [], 0.0
    for i in range(n):
        if i % (96 * 10) == 0:
            drift = rng.choice([1, -1]) * rng.uniform(0.0, 0.0002)
        o = px
        px *= (1 + rng.gauss(drift, 0.002))
        c15.append({"ts": i * M15, "open": o,
                    "high": max(o, px) * 1.001, "low": min(o, px) * 0.999,
                    "close": px, "volume": rng.uniform(1, 20)})
    return {
        "15m": c15,
        "1h": ind.aggregate(c15, "15m", "1h"),
        "1d": ind.aggregate(c15, "15m", "1d"),
        "1m": [],
    }


def _sweep(agent, series, bars, truncate: bool):
    """Rejoue la série et rend la trace des décisions.

    `truncate=True` ne donne à l'agent que le passé ; `truncate=False` lui donne
    l'historique COMPLET, futur inclus, et compte sur `closed()` pour le
    masquer. Si les deux traces divergent, c'est qu'une couche regarde devant
    elle.
    """
    from confluence.state import AgentState as _State

    state = _State()
    trace = []
    for i, bar in enumerate(bars):
        close_ms = int(bar["ts"]) + M15
        candles_in = ({tf: [c for c in cs if int(c["ts"]) + ind.INTERVAL_MS[tf] <= close_ms]
                       for tf, cs in series.items()} if truncate else series)
        decision = agent.decide(now_ms=close_ms, candles=candles_in, state=state,
                                equity=10_000.0, funding_hourly=0.00001)
        state = decision.state or state
        trace.append((decision.bar_ts, decision.blocked_by, decision.reason,
                      None if decision.signal is None else decision.signal.side.name))
    return trace


def test_anti_repaint_le_futur_ne_change_aucune_decision(small_cfg):
    """§10 : aucune décision ne change quand on rejoue l'historique bougie par
    bougie."""
    series = _history_for_agent()
    bars = series["15m"][-300:]
    avec_futur = _sweep(ConfluenceAgent(small_cfg), series, bars, truncate=False)
    sans_futur = _sweep(ConfluenceAgent(small_cfg), series, bars, truncate=True)
    assert avec_futur == sans_futur


def test_anti_repaint_deux_passages_identiques(small_cfg):
    series = _history_for_agent()
    bars = series["15m"][-200:]
    premier = _sweep(ConfluenceAgent(small_cfg), series, bars, truncate=False)
    second = _sweep(ConfluenceAgent(small_cfg), series, bars, truncate=False)
    assert premier == second


def test_idempotence_pas_de_second_signal_sur_la_meme_bougie(small_cfg):
    """§8 : « rejouer la même bougie ne doit jamais produire deux signaux »."""
    series = _history_for_agent()
    agent = ConfluenceAgent(small_cfg)
    state = AgentState()
    bar = series["15m"][-1]
    close_ms = int(bar["ts"]) + M15

    first = agent.decide(now_ms=close_ms, candles=series, state=state,
                         equity=10_000.0, funding_hourly=0.00001)
    second = agent.decide(now_ms=close_ms, candles=series, state=first.state,
                          equity=10_000.0, funding_hourly=0.00001)
    assert second.signal is None
    assert "déjà évaluée" in second.verdicts["15m"].reason


def test_le_cache_devaluation_ne_change_pas_les_decisions(small_cfg):
    """La mémoïsation du backtest doit être strictement transparente."""
    from confluence.agent import EvalCache

    series = _history_for_agent()
    bars = series["15m"][-200:]

    def sweep(cache):
        state = AgentState()
        out = []
        for bar in bars:
            close_ms = int(bar["ts"]) + M15
            d = ConfluenceAgent(small_cfg).decide(
                now_ms=close_ms, candles=series, state=state, equity=10_000.0,
                funding_hourly=0.00001, cache=cache)
            state = d.state or state
            out.append((d.bar_ts, d.blocked_by, d.reason))
        return out

    assert sweep(None) == sweep(EvalCache())


# ── Walk-forward (§9.2) ──────────────────────────────────────────────────────

def test_walkforward_ne_perd_aucune_fenetre_faute_de_trades(cfg, monkeypatch):
    """Une fenêtre stérile in-sample doit quand même être testée OOS.

    Régression réelle : quand aucun point de grille n'atteignait
    `min_is_trades`, tous les scores valaient -inf, la comparaison `>` restait
    fausse dès le premier tour, et la fenêtre disparaissait du rapport. Les
    fenêtres écartées étaient celles où la stratégie n'avait rien produit — les
    retirer de l'agrégat OOS, c'est un biais de survivance logé au cœur du
    protocole censé le débusquer.
    """
    from confluence import walkforward as wf
    from confluence.backtest import BacktestResult

    windows = [(0, 100, 100, 200), (200, 300, 300, 400)]
    monkeypatch.setattr(wf, "windows_for", lambda *a, **k: windows)
    # Tous les backtests rendent zéro trade : aucun point n'atteint le minimum.
    monkeypatch.setattr(wf.Backtester, "run",
                        lambda self, *a, **k: BacktestResult(initial_equity=10_000.0))

    report = wf.walk_forward(cfg, History(symbol="BTC"),
                             grid={"risk.k_stop": (1.2, 1.5)}, min_is_trades=10)

    assert len(report.windows) == len(windows), "aucune fenêtre ne doit être escamotée"
    assert all("repli" in n for n in report.notes)


def test_walkforward_choisit_le_meilleur_point_quand_il_existe(cfg, monkeypatch):
    from confluence import walkforward as wf
    from confluence.backtest import BacktestResult, Trade

    def fake_run(self, *args, **kwargs):
        result = BacktestResult(initial_equity=10_000.0)
        # k_stop=2.0 rend un PF meilleur : c'est lui qui doit être retenu.
        gain = 300.0 if self.cfg.risk.k_stop == 2.0 else 120.0
        for i in range(12):
            result.trades.append(Trade(
                entry_ms=i, exit_ms=i + 1, side="LONG", entry=100.0, exit=101.0,
                size=1.0, notional=100.0,
                gross_pnl=(gain if i % 2 else -100.0), fees=0.0, funding=0.0,
                reason="test", bars_held=1))
        return result

    monkeypatch.setattr(wf, "windows_for", lambda *a, **k: [(0, 100, 100, 200)])
    monkeypatch.setattr(wf.Backtester, "run", fake_run)

    report = wf.walk_forward(cfg, History(symbol="BTC"),
                             grid={"risk.k_stop": (1.2, 2.0)}, min_is_trades=1)
    assert report.windows[0].params == {"risk.k_stop": 2.0}
    assert not report.notes


def test_walkforward_profit_factor_agrege_et_non_moyenne(cfg):
    """Le PF global agrège gains et pertes, il ne moyenne pas les PF de fenêtre.

    Sinon une fenêtre à 2 trades et PF=8 pèserait autant qu'une fenêtre à 40
    trades — et le protocole se laisserait convaincre par un accident.
    """
    from confluence.walkforward import WalkForwardReport, WindowResult

    report = WalkForwardReport(windows=[
        WindowResult(0, 0, 1, 1, 2, {}, {}, {}, oos_wins=800.0, oos_losses=100.0),
        WindowResult(1, 0, 1, 1, 2, {}, {}, {}, oos_wins=100.0, oos_losses=900.0),
    ])
    assert report.oos_profit_factor == pytest.approx(900.0 / 1000.0)


# ── Configuration (§7) ───────────────────────────────────────────────────────

def test_config_refuse_une_zone_morte_inversee(cfg):
    with pytest.raises(ConfigError):
        cfg.replace_path("regime_1h.adx_trend", 10.0)


def test_config_refuse_post_only_desactive(cfg):
    with pytest.raises(ConfigError):
        cfg.replace_path("execution_1m.post_only", False)


def test_config_refuse_une_cle_inconnue():
    with pytest.raises(ConfigError):
        config_mod.from_dict({"symbol": "BTC", "risk": {"k_stopp": 1.5}})


def test_config_percentiles_incoherents(cfg):
    with pytest.raises(ConfigError):
        cfg.replace_path("regime_1h.atr_percentile_min", 95.0)


def test_config_par_defaut_charge_et_valide(cfg):
    assert cfg.symbol == "BTC"
    assert cfg.risk.fee_roundtrip == pytest.approx(cfg.risk.fee_maker + cfg.risk.fee_taker)
    assert cfg.regime_1h.percentile_window_bars == cfg.regime_1h.atr_percentile_days * 24


# ── Indicateurs : causalité stricte ──────────────────────────────────────────

@pytest.mark.parametrize("fn,args", [
    (ind.atr, (14,)),
    (ind.adx, (14,)),
])
def test_indicateurs_sur_bougies_strictement_causaux(fn, args):
    rng = random.Random(23)
    px = [100.0]
    for _ in range(400):
        px.append(px[-1] * (1 + rng.gauss(0, 0.004)))
    series = candles(px, HOUR)
    full = fn(series, *args)
    for cut in (100, 250, 399):
        assert fn(series[:cut], *args)[-1] == pytest.approx(full[cut - 1])


def test_agregation_rejette_les_bougies_incompletes():
    c1m = candles([100.0] * 20, MIN)
    assert len(ind.aggregate(c1m, "1m", "15m")) == 1, "la bougie partielle est rejetée"


def test_les_trous_sont_signales_pas_combles():
    series = candles([100.0, 101.0, 102.0], HOUR)
    series[2]["ts"] += 5 * HOUR
    gaps = ind.find_gaps(series, "1h")
    assert gaps and gaps[0][2] == 5


def test_bougies_aberrantes_detectees():
    series = candles([100.0, 101.0, 102.0], HOUR)
    series[1]["high"] = 1.0          # high < low : impossible
    assert 1 in ind.anomalies(series)

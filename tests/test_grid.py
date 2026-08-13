"""
Tests du GridAgent — SPEC §12.

Beaucoup de ces tests vérifient une **absence de pouvoir** plutôt qu'une
fonctionnalité : qu'aucun chemin ne repose des ordres après une cassure,
qu'aucune configuration ne fasse grossir la taille, qu'aucun fill hors flatten
ne soit taker. Sur une stratégie short-volatilité, ce sont ces interdictions qui
tiennent le risque — pas les fonctionnalités.

Le fil conducteur reste le §0 : l'edge d'une grille vient de trois choses, et
chacune a ici sa batterie de tests. Filtre de régime (§2), plancher de frais
(§3.2), sortie non négociable (§6.1).
"""

from __future__ import annotations

import pytest

from grid import config as config_mod
from grid.accounting import GridAccounting, GridSession, SessionPnL, aggregate
from grid.agent import GridAgent
from grid.build import (
    RangeSpec,
    build_grid,
    check_activation,
    detect_range,
    step_price,
    traversing_loss,
)
from grid.config import GridConfigError
from grid.router import Engine, RouterState, StrategyRouter
from grid.types import Fill, Side, StopReason

M15 = 900_000


@pytest.fixture
def cfg():
    return config_mod.load()


@pytest.fixture
def rng():
    return RangeSpec(lower=60_000.0, upper=64_000.0, center=62_000.0, width=4_000.0)


@pytest.fixture
def plan(cfg, rng):
    return build_grid(cfg, rng, atr_1h=400.0, atr_15m=150.0, equity=10_000.0)


def activation_kwargs(**over):
    base = dict(adx=15.0, adx_bars_below=3, atr_percentile=40.0,
                range_spec=RangeSpec(60_000.0, 64_000.0, 62_000.0, 4_000.0),
                atr_1h=400.0, funding_annualized=0.05, fee_killswitch_active=False,
                observation_mode=False, macro_extreme=False, cooldown_remaining_h=0.0)
    base.update(over)
    return base


# ── §12 Activation : chaque condition prise isolément bloque ─────────────────

def test_activation_nominale_passe(cfg):
    assert check_activation(cfg, **activation_kwargs()).passed


@pytest.mark.parametrize("override,marker", [
    ({"adx": 25.0}, "ADX"),
    ({"adx_bars_below": 1}, "non confirmé"),
    ({"atr_percentile": 5.0}, "trop calme"),
    ({"atr_percentile": 80.0}, "trop volatil"),
    ({"funding_annualized": 0.5}, "funding"),
    ({"funding_annualized": None}, "funding indisponible"),
    ({"fee_killswitch_active": True}, "kill-switch"),
    ({"observation_mode": True}, "observation"),
    ({"macro_extreme": True}, "macro"),
    ({"cooldown_remaining_h": 5.0}, "cooldown"),
    ({"range_spec": None}, "range non détectable"),
])
def test_chaque_condition_bloque_isolement(cfg, override, marker):
    """§2 : toutes les conditions sont requises. Le défaut est l'inaction."""
    verdict = check_activation(cfg, **activation_kwargs(**override))
    assert not verdict.passed
    assert marker in verdict.reason


def test_range_trop_etroit_ne_deploie_pas(cfg):
    """§2 : un range trop étroit ne paie pas."""
    etroit = RangeSpec(61_800.0, 62_200.0, 62_000.0, 400.0)      # 1× ATR seulement
    verdict = check_activation(cfg, **activation_kwargs(range_spec=etroit))
    assert not verdict.passed and "trop étroit" in verdict.reason


def test_grille_refusee_si_pas_assez_de_niveaux(cfg):
    """Le §2 exige `min_levels` niveaux à l'espacement du §3.2."""
    minuscule = RangeSpec(61_900.0, 62_100.0, 62_000.0, 200.0)
    assert build_grid(cfg, minuscule, atr_1h=400.0, atr_15m=150.0, equity=10_000.0) is None


def test_grille_tourne_avec_un_biais_1d_FLAT(cfg):
    """Une grille neutre n'a besoin d'AUCUN biais directionnel — c'est même son
    terrain idéal. Le RegimeLayer du ConfluenceAgent veto en biais FLAT ; on
    consomme ses données, pas son verdict."""
    assert check_activation(cfg, **activation_kwargs()).passed   # aucun biais fourni


# ── §12 Construction : plancher de frais, sizing par perte traversante ───────

@pytest.mark.parametrize("atr_1h", [1.0, 50.0, 400.0, 5_000.0])
def test_step_jamais_sous_le_plancher_de_frais(cfg, atr_1h):
    """§3.2 : quel que soit l'ATR, un cycle rapporte ≥ 5× ses frais.

    C'est ce qui empêche la grille d'être « une machine à payer l'exchange ».
    """
    mid = 62_000.0
    step = step_price(cfg, atr_1h, mid)
    floor_price = cfg.build.grid_edge_multiple * cfg.build.roundtrip_maker_bps / 10_000.0 * mid
    assert step >= floor_price - 1e-9


def test_step_suit_latr_quand_elle_domine(cfg):
    petit = step_price(cfg, atr_1h=400.0, mid_price=62_000.0)
    grand = step_price(cfg, atr_1h=4_000.0, mid_price=62_000.0)
    assert grand > petit


def test_sizing_derive_de_la_contrainte_de_perte(cfg, rng, plan):
    """§3.3 : la taille se DÉDUIT de la perte tolérable, pas l'inverse."""
    budget = 10_000.0 * cfg.build.max_grid_loss_pct
    assert plan.projected_worst_loss == pytest.approx(budget, rel=1e-6)


@pytest.mark.parametrize("equity", [1_000.0, 10_000.0, 250_000.0])
def test_propriete_perte_traversante_pour_tout_chemin_monotone(cfg, rng, equity):
    """Propriété du §12 : pour TOUT chemin de prix monotone, la perte simulée au
    flatten reste ≤ max_grid_loss_pct."""
    plan = build_grid(cfg, rng, atr_1h=400.0, atr_15m=150.0, equity=equity)
    buys = [lv.price for lv in plan.levels if lv.side is Side.BUY]
    sells = [lv.price for lv in plan.levels if lv.side is Side.SELL]
    flatten_down = rng.lower - cfg.exits.k_breakout_atr15m * 150.0
    flatten_up = rng.upper + cfg.exits.k_breakout_atr15m * 150.0

    for prices, side, flat in ((buys, Side.BUY, flatten_down),
                               (sells, Side.SELL, flatten_up)):
        loss = traversing_loss(plan.size_per_level, prices, flat, side)
        assert loss <= equity * cfg.build.max_grid_loss_pct + 1e-6


def test_taille_identique_sur_tous_les_niveaux(plan):
    """§3.3 et §10 : taille constante, aucune progression."""
    tailles = {lv.size for lv in plan.levels}
    assert len(tailles) == 1


def test_detection_range_arrondit_vers_linterieur(cfg):
    closes = [60_000 + (i % 100) * 40 for i in range(400)]
    candles = [{"ts": i * M15, "open": c, "high": c, "low": c, "close": float(c),
                "volume": 1.0} for i, c in enumerate(closes)]
    spec = detect_range(candles, lookback=384, tick_size=1.0)
    assert spec is not None and spec.lower < spec.upper


# ── §12 Long/Short : plafond d'inventaire, verrouillage du cycle ─────────────

def test_cycle_buy_sell_verrouille_step_moins_frais(cfg, plan):
    """§4 : chaque BUY exécuté pose un SELL au niveau supérieur."""
    agent = GridAgent(cfg, plan, equity=10_000.0, started_ms=0)
    buy = next(lv for lv in agent.place_orders() if lv.side is Side.BUY)
    agent.on_fill(buy, ts_ms=1_000)

    paired = agent.pending[buy.index]
    assert paired.side is Side.SELL
    assert paired.price == pytest.approx(buy.paired_price)

    agent.on_fill(paired, ts_ms=2_000)
    brut = plan.step * buy.size
    assert agent.acct.pnl.realized_grid_pnl == pytest.approx(brut, rel=1e-9)
    assert agent.acct.pnl.fees > 0
    assert agent.acct.pnl.realized_grid_pnl - agent.acct.pnl.fees > 0, (
        "un cycle doit rester gagnant NET — c'est tout l'objet du plancher §3.2")


def test_plafond_dinventaire_net_respecte(cfg, plan):
    """§4 : les ordres qui AUGMENTERAIENT l'exposition sont retirés, ceux qui la
    réduisent restent."""
    agent = GridAgent(cfg, plan, equity=10_000.0, started_ms=0)
    for lv in sorted([l for l in plan.levels if l.side is Side.BUY],
                     key=lambda l: -l.price):
        if lv in agent.place_orders():
            agent.on_fill(lv, ts_ms=1_000)
    exposure = abs(agent.acct.inventory.size) * plan.center
    assert exposure <= plan.max_net_exposure_usd * 1.05

    restants = agent.place_orders()
    assert all(lv.side is Side.SELL for lv in restants), (
        "au plafond, seuls les ordres réducteurs d'exposition subsistent")


def test_plafond_tient_sous_fills_partiels(cfg, plan):
    agent = GridAgent(cfg, plan, equity=10_000.0, started_ms=0)
    for lv in [l for l in plan.levels if l.side is Side.BUY][:3]:
        agent.on_fill(lv, ts_ms=1_000)
    for lv in agent.place_orders():
        assert not agent._would_breach_exposure(lv)


# ── §12 Sorties : cassure, taker circonscrit, cooldown ──────────────────────

def test_cassure_declenche_sur_cloture_15m(cfg, plan):
    agent = GridAgent(cfg, plan, equity=10_000.0, started_ms=0)
    seuil = plan.upper + cfg.exits.k_breakout_atr15m * 150.0
    assert not agent.check_breakout(seuil - 1, atr_15m=150.0).triggered
    d = agent.check_breakout(seuil + 1, atr_15m=150.0)
    assert d.triggered and d.direction is Side.BUY


def test_flatten_ferme_linventaire_defavorable(cfg, plan):
    """§6.1 étape 1 : shorts fermés sur cassure haussière, sans condition."""
    agent = GridAgent(cfg, plan, equity=10_000.0, started_ms=0)
    for lv in [l for l in plan.levels if l.side is Side.SELL][:4]:
        agent.on_fill(lv, ts_ms=1_000)
    assert agent.acct.inventory.size < 0

    d = agent.check_breakout(plan.upper + 200, atr_15m=150.0)
    agent.on_breakout(d, ts_ms=2_000, price=plan.upper + 200,
                      bias_1d="LONG_ONLY", atr_1h=400.0)
    assert agent.acct.inventory.is_flat
    assert agent.stopped is StopReason.BREAKOUT


def test_taker_autorise_uniquement_au_flatten(cfg, plan):
    """§10 : pas de taker à l'entrée. Les seuls fills non-maker viennent de
    `_flatten`, et nulle part ailleurs."""
    agent = GridAgent(cfg, plan, equity=10_000.0, started_ms=0)
    for lv in agent.place_orders()[:5]:
        agent.on_fill(lv, ts_ms=1_000)
    assert all(f.maker for f in agent.acct.fills), "aucun fill d'entrée ne peut être taker"

    d = agent.check_breakout(plan.lower - 200, atr_15m=150.0)
    agent.on_breakout(d, ts_ms=2_000, price=plan.lower - 200, bias_1d=None,
                      atr_1h=400.0, timed_out=True)
    assert any(not f.maker for f in agent.acct.fills)
    assert agent.session.taker_fills == 1


def test_flatten_maker_avant_timeout(cfg, plan):
    agent = GridAgent(cfg, plan, equity=10_000.0, started_ms=0)
    for lv in [l for l in plan.levels if l.side is Side.BUY][:3]:
        agent.on_fill(lv, ts_ms=1_000)
    d = agent.check_breakout(plan.lower - 200, atr_15m=150.0)
    agent.on_breakout(d, ts_ms=2_000, price=plan.lower - 200, bias_1d=None,
                      atr_1h=400.0, timed_out=False)
    assert agent.session.taker_fills == 0, "avant timeout, le flatten reste maker"


def test_bascule_de_regime_arrete_proprement(cfg, plan):
    agent = GridAgent(cfg, plan, equity=10_000.0, started_ms=0)
    agent.on_fill(agent.place_orders()[0], ts_ms=1_000)
    agent.on_regime_shift(ts_ms=2_000, price=62_000.0)
    assert agent.stopped is StopReason.REGIME_SHIFT
    assert agent.acct.inventory.is_flat


def test_pic_de_volatilite_conserve_linventaire(cfg, plan):
    """§6.3 : retrait des ordres, conservation PASSIVE — pas de flatten
    d'urgence. Sortir au marché dans un pic coûte plus cher qu'attendre."""
    agent = GridAgent(cfg, plan, equity=10_000.0, started_ms=0)
    agent.on_fill(agent.place_orders()[0], ts_ms=1_000)
    inv = agent.acct.inventory.size
    agent.on_vol_spike(ts_ms=2_000)
    assert agent.stopped is StopReason.VOL_SPIKE
    assert agent.acct.inventory.size == inv, "l'inventaire est conservé"
    assert agent.session.taker_fills == 0


# ── §12 Handoff (§6.1 étape 2) ──────────────────────────────────────────────

def _agent_long(cfg, plan):
    agent = GridAgent(cfg, plan, equity=10_000.0, started_ms=0)
    for lv in sorted([l for l in plan.levels if l.side is Side.BUY],
                     key=lambda l: -l.price)[:5]:
        agent.on_fill(lv, ts_ms=1_000)
    return agent


def test_handoff_sur_cassure_alignee_au_biais(cfg, plan):
    """§6.1 : cassure haussière + biais LONG_ONLY ⇒ l'inventaire favorable est
    transféré, avec le bord du range comme niveau d'invalidation."""
    agent = _agent_long(cfg, plan)
    inv = agent.acct.inventory.size
    d = agent.check_breakout(plan.upper + 200, atr_15m=150.0)
    fills, handoff = agent.on_breakout(d, ts_ms=2_000, price=plan.upper + 200,
                                       bias_1d="LONG_ONLY", atr_1h=400.0)
    assert handoff is not None
    assert handoff.side is Side.BUY
    assert handoff.size == pytest.approx(inv)
    assert handoff.stop_price == pytest.approx(
        plan.upper - cfg.exits.handoff_stop_k_atr * 400.0)
    assert agent.stopped is StopReason.BREAKOUT


@pytest.mark.parametrize("bias", ["SHORT_ONLY", "FLAT", None])
def test_flatten_complet_si_cassure_contre_biais_ou_flat(cfg, plan, bias):
    """§6.1 : une cassure à contre-biais est un candidat au faux breakout — on
    ne la chevauche pas. Un biais FLAT n'est pas un alignement."""
    agent = _agent_long(cfg, plan)
    d = agent.check_breakout(plan.upper + 200, atr_15m=150.0)
    fills, handoff = agent.on_breakout(d, ts_ms=2_000, price=plan.upper + 200,
                                       bias_1d=bias, atr_1h=400.0)
    assert handoff is None
    assert agent.acct.inventory.is_flat


def test_handoff_plafonne_et_deboucle_lexcedent(cfg, rng):
    """L'excédent au-delà du plafond est débouclé en MAKER — il va dans le bon
    sens, rien ne presse."""
    petit_cap = config_mod.load().replace_path("exits.handoff_max_position_usd", 500.0)
    plan = build_grid(petit_cap, rng, atr_1h=400.0, atr_15m=150.0, equity=100_000.0)
    agent = _agent_long(petit_cap, plan)
    d = agent.check_breakout(plan.upper + 200, atr_15m=150.0)
    fills, handoff = agent.on_breakout(d, ts_ms=2_000, price=plan.upper + 200,
                                       bias_1d="LONG_ONLY", atr_1h=400.0)
    assert handoff.excess_size > 0
    assert handoff.size * handoff.entry_price <= 500.0 * 1.001
    assert all(f.maker for f in fills), "l'excédent se déboucle en maker"


def test_handoff_desactive_reproduit_le_comportement_v1(cfg, plan):
    """`breakout_handoff: false` ⇒ flatten complet, à l'identique."""
    sans = config_mod.load().replace_path("exits.breakout_handoff", False)
    agent = _agent_long(sans, plan)
    d = agent.check_breakout(plan.upper + 200, atr_15m=150.0)
    fills, handoff = agent.on_breakout(d, ts_ms=2_000, price=plan.upper + 200,
                                       bias_1d="LONG_ONLY", atr_1h=400.0)
    assert handoff is None and agent.acct.inventory.is_flat


def test_aucun_ordre_de_grille_apres_cassure(cfg, plan):
    """Interdiction structurelle du §6.1 : pas de réancrage, pas de trailing
    grid. Après un arrêt, il n'existe AUCUN chemin qui repose des ordres."""
    agent = _agent_long(cfg, plan)
    d = agent.check_breakout(plan.upper + 200, atr_15m=150.0)
    agent.on_breakout(d, ts_ms=2_000, price=plan.upper + 200,
                      bias_1d="LONG_ONLY", atr_1h=400.0)
    assert agent.place_orders() == []
    agent.on_fill  # l'API existe encore, mais plus rien n'est coté
    assert agent.place_orders() == []


# ── §12 Comptabilité (§7) ───────────────────────────────────────────────────

def test_net_est_la_somme_exacte_des_composantes():
    pnl = SessionPnL(realized_grid_pnl=100.0, inventory_mtm=-250.0,
                     inventory_realized=-30.0, funding_pnl=5.0, fees=12.0)
    assert pnl.net == pytest.approx(100.0 - 250.0 - 30.0 - 5.0 - 12.0)


def test_scenario_realise_positif_mtm_negatif_est_perdant():
    """§7 : le cas d'auto-illusion. Un réalisé flatteur, un inventaire hors
    range, et une session qui perd."""
    pnl = SessionPnL(realized_grid_pnl=420.0, inventory_mtm=-900.0, fees=30.0)
    assert not pnl.is_winner
    assert pnl.net < 0
    assert pnl.illusion_gap > 0, "l'écart mesure exactement ce que le réalisé cache"


def test_realized_grid_pnl_reste_positif_apres_flatten(cfg, plan):
    """Le flatten cristallise du LATENT, il ne crée pas un cycle perdant.

    Sans cette séparation, `realized_grid_pnl` deviendrait négatif et l'écart
    d'illusion cesserait de mesurer ce qu'il prétend.
    """
    agent = _agent_long(cfg, plan)
    d = agent.check_breakout(plan.lower - 200, atr_15m=150.0)
    agent.on_breakout(d, ts_ms=2_000, price=plan.lower - 300, bias_1d=None, atr_1h=400.0)
    session = agent.finish(plan.lower - 300)
    assert session.pnl.realized_is_structurally_positive
    assert session.pnl.inventory_pnl < 0, "la perte est portée par l'inventaire"
    assert session.pnl.net < 0


def test_funding_est_soustrait(cfg):
    acct = GridAccounting(0.0, 0.0)
    acct.apply_fill(Fill(ts_ms=0, price=60_000.0, side=Side.BUY, size=1.0, level_index=0))
    acct.accrue_funding(rate=0.0001, mark_price=60_000.0)
    assert acct.pnl.funding_pnl == pytest.approx(6.0)
    acct.mark(60_000.0)
    assert acct.net == pytest.approx(-6.0)


def test_agregat_compte_en_sessions_pas_en_cycles():
    """§9.4 : la significativité se compte au niveau SESSION."""
    gagnante = GridSession(started_ms=0, equity_at_start=10_000.0)
    gagnante.pnl = SessionPnL(realized_grid_pnl=200.0, gross_pnl_abs=200.0, fees=10.0)
    gagnante.stop_reason = StopReason.REGIME_SHIFT
    perdante = GridSession(started_ms=0, equity_at_start=10_000.0)
    perdante.pnl = SessionPnL(realized_grid_pnl=50.0, inventory_realized=-400.0,
                              gross_pnl_abs=450.0, fees=20.0)
    perdante.stop_reason = StopReason.BREAKOUT

    agg = aggregate([gagnante, perdante])
    assert agg["sessions"] == 2
    assert agg["stop_reasons"] == {"regime_shift": 1, "breakout": 1}
    assert agg["profit_factor"] == pytest.approx(190.0 / 370.0, rel=1e-3)


def test_perte_de_session_bornee_par_le_flatten(cfg, rng):
    """§9.4 : la perte max d'une session vérifie que le flatten fonctionne."""
    plan = build_grid(cfg, rng, atr_1h=400.0, atr_15m=150.0, equity=10_000.0)
    agent = GridAgent(cfg, plan, equity=10_000.0, started_ms=0)
    for lv in sorted([l for l in plan.levels if l.side is Side.BUY],
                     key=lambda l: -l.price):
        agent.on_fill(lv, ts_ms=1_000)
    trigger = rng.lower - cfg.exits.k_breakout_atr15m * 150.0
    d = agent.check_breakout(trigger - 1, atr_15m=150.0)
    agent.on_breakout(d, ts_ms=2_000, price=trigger - 1, bias_1d=None, atr_1h=400.0)
    session = agent.finish(trigger - 1)
    assert session.loss_pct <= cfg.build.max_grid_loss_pct * 1.1


# ── §12 Anti-martingale ─────────────────────────────────────────────────────

@pytest.mark.parametrize("cle", ["size_multiplier", "martingale", "size_progression",
                                 "geometric_sizing", "averaging_down"])
def test_toute_progression_de_taille_est_rejetee_au_chargement(cle):
    """§10 : refusée par construction, au CHARGEMENT — pas à l'exécution."""
    with pytest.raises(GridConfigError, match="progression de taille"):
        config_mod.from_dict({"symbol": "BTC", "build": {cle: 2.0}})


def test_progression_rejetee_meme_imbriquee():
    with pytest.raises(GridConfigError):
        config_mod.from_dict({"symbol": "BTC", "exits": {"nested": {"martingale": True}}})


def test_post_only_desactive_est_rejete():
    with pytest.raises(GridConfigError, match="post_only"):
        config_mod.from_dict({"symbol": "BTC", "execution": {"post_only": False}})


def test_edge_multiple_sous_1_est_rejete():
    with pytest.raises(GridConfigError, match="grid_edge_multiple"):
        config_mod.from_dict({"symbol": "BTC", "build": {"grid_edge_multiple": 0.5}})


def test_tirages_placebo_insuffisants_rejetes():
    """Le gate ne peut pas passer sous 1/α tirages : le refuser au chargement
    évite de condamner un candidat par arithmétique."""
    with pytest.raises(GridConfigError, match="trop faible"):
        config_mod.from_dict({"symbol": "BTC",
                              "backtest": {"placebo": {"n_draws": 10, "alpha": 0.025}}})


def test_config_gelee_impose_40_tirages(cfg):
    """Registre entrée n°2 : α = 0,025 ⇒ 40 tirages minimum."""
    assert cfg.backtest.placebo.alpha == pytest.approx(0.025)
    assert cfg.backtest.placebo.n_draws >= 40


# ── §12 Routage (§1) ────────────────────────────────────────────────────────

def test_range_exige_confirmation_avant_deploiement():
    router = StrategyRouter(confirm_bars_1h=3)
    state = RouterState()
    for i in range(2):
        route = router.route(state, regime="range", bar_ts=(i + 1) * 3_600_000)
        assert route.engine is Engine.NONE
    route = router.route(state, regime="range", bar_ts=3 * 3_600_000)
    assert route.engine is Engine.GRID


def test_sortie_vers_trend_est_immediate():
    """Asymétrie du §1 : attendre pour ARRÊTER laisserait la grille tourner
    pendant la cassure."""
    router = StrategyRouter(confirm_bars_1h=3)
    state = RouterState()
    for i in range(3):
        router.route(state, regime="range", bar_ts=(i + 1) * 3_600_000)
    route = router.route(state, regime="trend", bar_ts=4 * 3_600_000)
    assert route.engine is not Engine.GRID
    assert state.range_streak == 0


def test_les_deux_moteurs_ne_sont_jamais_actifs_ensemble():
    router = StrategyRouter(confirm_bars_1h=1)
    state = RouterState()
    for regime in ("range", "trend", "chop", None, "range"):
        route = router.route(state, regime=regime, bar_ts=state.last_bar_ts + 3_600_000)
        assert route.is_coherent()


def test_veto_macro_coupe_tout():
    router = StrategyRouter(confirm_bars_1h=1)
    state = RouterState()
    route = router.route(state, regime="range", bar_ts=3_600_000, macro_extreme=True)
    assert route.engine is Engine.NONE and "macro" in route.reason


def test_branche_trend_sans_moteur_dentree():
    """Le ConfluenceAgent est REJETÉ : TREND n'ouvre aucune position."""
    router = StrategyRouter(confirm_bars_1h=1, trend_engine_available=False)
    state = RouterState()
    route = router.route(state, regime="trend", bar_ts=3_600_000)
    assert route.engine is Engine.NONE
    assert "REJETÉ" in route.reason


def test_branche_trend_accueille_la_position_transferee():
    router = StrategyRouter(confirm_bars_1h=1, trend_engine_available=False)
    state = RouterState()
    route = router.route(state, regime="trend", bar_ts=3_600_000,
                         has_handoff_position=True)
    assert route.engine is Engine.TREND
    assert "TrailingStopAgent" in route.reason


def test_routeur_idempotent_sur_la_meme_bougie():
    router = StrategyRouter(confirm_bars_1h=3)
    state = RouterState()
    router.route(state, regime="range", bar_ts=3_600_000)
    router.route(state, regime="range", bar_ts=3_600_000)
    assert state.range_streak == 1, "rejouer une bougie ne confirme pas plus vite"


# ── §12 Reconciliation / restart ────────────────────────────────────────────

def test_etat_de_grille_survit_a_un_restart(cfg, plan):
    from grid.types import GridState

    state = GridState(levels=plan.levels, lower=plan.lower, upper=plan.upper,
                      center=plan.center, step=plan.step, atr_1h=plan.atr_1h,
                      started_ms=1_000)
    state.inventory.size = 0.05
    state.inventory.avg_price = 61_500.0
    state.filled = {0: True, 1: False}

    rechargee = GridState.from_json(state.to_json())
    assert len(rechargee.levels) == len(plan.levels)
    assert rechargee.inventory.size == pytest.approx(0.05)
    assert rechargee.inventory.avg_price == pytest.approx(61_500.0)
    assert rechargee.filled[0] is True
    assert rechargee.step == pytest.approx(plan.step)


def test_router_state_survit_a_un_restart():
    state = RouterState(range_streak=2, last_bar_ts=3_600_000)
    rechargee = RouterState.from_json(state.to_json())
    assert rechargee.range_streak == 2 and rechargee.last_bar_ts == 3_600_000


# ── Inventaire : sémantique de compensation ─────────────────────────────────

def test_compensation_realise_sans_deplacer_la_moyenne():
    from grid.types import Inventory

    inv = Inventory()
    inv.apply(Fill(ts_ms=0, price=100.0, side=Side.BUY, size=2.0, level_index=0))
    assert inv.avg_price == pytest.approx(100.0)
    realized = inv.apply(Fill(ts_ms=1, price=110.0, side=Side.SELL, size=1.0,
                              level_index=0))
    assert realized == pytest.approx(10.0)
    assert inv.avg_price == pytest.approx(100.0), "une compensation ne bouge pas la moyenne"
    assert inv.size == pytest.approx(1.0)


def test_retournement_dinventaire_ouvre_au_prix_du_fill():
    from grid.types import Inventory

    inv = Inventory()
    inv.apply(Fill(ts_ms=0, price=100.0, side=Side.BUY, size=1.0, level_index=0))
    inv.apply(Fill(ts_ms=1, price=110.0, side=Side.SELL, size=3.0, level_index=0))
    assert inv.size == pytest.approx(-2.0)
    assert inv.avg_price == pytest.approx(110.0)


# ── §8 Intégration APM ──────────────────────────────────────────────────────

def test_conditionnement_interdit_sur_les_seuils_de_regime():
    """§8 : les seuils ADX/percentile du §2 restent FIGÉS.

    Si le percentile de volatilité pouvait modifier le seuil qui décide du
    régime, le système choisirait le régime dans lequel il préfère se trouver.
    """
    from grid.adaptive import GridFeedbackError, assert_no_grid_feedback

    with pytest.raises(GridFeedbackError, match="détection de régime"):
        assert_no_grid_feedback({"activation.adx_max": (18.0, 22.0)})
    with pytest.raises(GridFeedbackError):
        assert_no_grid_feedback({"activation.atr_percentile_range": (10.0, 70.0)})
    assert_no_grid_feedback({"build.k_step": (0.3, 0.5)})       # autorisé


def test_k_step_selargit_avec_la_volatilite(cfg):
    """§8 : vol plus haute ⇒ pas plus large."""
    from grid.adaptive import condition_grid

    bounds = {"build.k_step": (0.30, 0.50)}
    calme, _ = condition_grid(cfg, vol_percentile=10.0, bounds=bounds)
    agite, _ = condition_grid(cfg, vol_percentile=90.0, bounds=bounds)
    assert calme.build.k_step == pytest.approx(0.30)
    assert agite.build.k_step == pytest.approx(0.50)


def test_exposition_reduite_en_forte_volatilite(cfg):
    from grid.adaptive import condition_grid

    bounds = {"build.max_net_exposure_frac": (0.60, 0.40)}
    calme, _ = condition_grid(cfg, vol_percentile=0.0, bounds=bounds)
    agite, _ = condition_grid(cfg, vol_percentile=100.0, bounds=bounds)
    assert agite.build.max_net_exposure_frac < calme.build.max_net_exposure_frac


def test_aucune_posture_ne_peut_elargir_la_perte_max(cfg):
    """§8 : `max_grid_loss_pct` ne peut jamais être relevé."""
    from grid.adaptive import condition_grid

    conditionnee, notes = condition_grid(
        cfg, vol_percentile=100.0, bounds={"build.max_grid_loss_pct": (0.015, 0.05)})
    assert conditionnee.build.max_grid_loss_pct <= cfg.build.max_grid_loss_pct
    assert any("plafonné" in n for n in notes)


def test_percentile_absent_laisse_la_config_inchangee(cfg):
    from grid.adaptive import condition_grid

    out, notes = condition_grid(cfg, vol_percentile=None,
                                bounds={"build.k_step": (0.3, 0.5)})
    assert out.build.k_step == cfg.build.k_step
    assert any("indisponible" in n for n in notes)


def test_apm_degrade_interdit_le_deploiement():
    from grid.adaptive import posture_allows_deployment

    allowed, reason = posture_allows_deployment("neutral", degraded=True)
    assert not allowed and "dégradé" in reason
    assert posture_allows_deployment("neutral", degraded=False)[0]


def test_params_registre_restent_restreints(cfg):
    """Chaque paramètre supplémentaire est une dimension de plus dans l'espace
    d'optimisation, donc une chance de plus de trouver du bruit."""
    from grid.adaptive import grid_params_for_registry

    params = grid_params_for_registry(cfg)
    assert all(k.startswith("build.") for k in params)
    assert len(params) <= 8


# ── Régression : curseur de funding ─────────────────────────────────────────

def test_le_curseur_de_funding_avance_sans_session_active(cfg):
    """Régression réelle, trouvée sur données réelles : `i_fund` n'avançait qu'à
    l'intérieur d'une session active. Sans session, il restait à 0 et le filtre
    §2 lisait le TOUT PREMIER taux de la série pour l'éternité — donc vetait en
    permanence si ce taux dépassait le seuil.

    Résultat : zéro session sur 1029 jours, pour une raison qui n'avait rien à
    voir avec le marché. Un « rejet » de ce genre est indiscernable d'un vrai
    échec de stratégie, exactement ce que le protocole cherche à éviter.
    """
    from confluence import config as conf_config, indicators as ind
    from grid.backtest import GridBacktester

    # Premier taux ÉNORME (87 % annualisé), puis taux normaux. Avec le bug, le
    # premier taux restait actif indéfiniment et bloquait tout déploiement.
    funding = [(0, 0.0001)] + [(i * 3_600_000, 0.000001) for i in range(1, 24 * 60)]

    import random
    random.seed(11)
    px, c1 = 62_000.0, []
    for i in range(30 * 1440):
        o = px
        px *= (1 + random.gauss(0.0, 0.0005))
        px = max(60_500, min(63_500, px))
        c1.append({"ts": i * 60_000, "open": o, "high": max(o, px) * 1.0003,
                   "low": min(o, px) * 0.9997, "close": px, "volume": 1.0})

    ccfg = conf_config.load().replace_path("regime_1h.atr_percentile_days", 3)
    bt = GridBacktester(cfg, ccfg, 10_000.0)
    res = bt.run(c1, ind.aggregate(c1, "1m", "15m"), ind.aggregate(c1, "1m", "1h"),
                 funding=funding)

    vetoes = dict(res.veto_distribution(20))
    funding_vetoes = sum(v for k, v in vetoes.items() if "funding" in k)
    assert funding_vetoes < res.bars_processed / 100, (
        f"le funding vete {funding_vetoes} fois — le curseur ne suit pas la série")


def test_funding_annualise_utilise_le_dernier_reglement_traverse():
    from grid.backtest import GridBacktester

    rates = [0.00001, 0.00002, 0.00003]
    assert GridBacktester._funding_annualized(rates, 0) is None, (
        "avant tout règlement, aucun taux n'est connu")
    assert GridBacktester._funding_annualized(rates, 1) == pytest.approx(
        0.00001 * 24 * 365)
    assert GridBacktester._funding_annualized(rates, 3) == pytest.approx(
        0.00003 * 24 * 365)

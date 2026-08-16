"""
Tests du MomentumAgent — SPEC §12.

Le fichier s'appelle `test_momentum_agent.py` et non `test_momentum.py` :
ce dernier existe déjà et couvre le `MomentumPaperTrader` de SimpleBot
(momentum 4 h paper). Deux stratégies différentes, deux suites distinctes.

**La propriété centrale est le test anti-lookahead de l'univers.** Le §1 la
nomme « piège n°1 », et c'est le mécanisme par lequel un backtest
cross-sectionnel ment presque toujours : les alts liquides de 2026 ne sont pas
un échantillon aléatoire de ceux qui existaient en 2021, et les sélectionner
rétroactivement fabrique un edge qui n'a jamais existé. Plusieurs tests ici ne
vérifient donc pas qu'une fonctionnalité marche, mais qu'une information
n'atteint jamais une décision.
"""

from __future__ import annotations

import random

import pytest

from momentum import config as config_mod
from momentum.accounting import (
    MomentumAccounting,
    MomentumPnL,
    RebalanceEvent,
    max_drawdown,
    profit_factor,
)
from momentum.agent import CircuitBreakerTripped, MomentumAgent
from momentum.config import MomentumConfigError, SignalConditioningError
from momentum.core import (
    DAY_MS,
    Portfolio,
    build_portfolio,
    momentum_score,
    rank_scores,
    select_universe,
    target_symbols,
)

DAY = DAY_MS


def series(n=120, start_day=0, drift=0.0, price=100.0, volume=1e6, step=None):
    """Série quotidienne synthétique. `step` force des rendements explicites."""
    out, px = [], price
    for i in range(n):
        px = px * (1 + (step[i] if step else drift))
        out.append({"ts": (start_day + i) * DAY, "open": px, "high": px,
                    "low": px, "close": px, "volume": volume / px})
    return out


@pytest.fixture
def cfg():
    return config_mod.load()


# ── §12 Univers : la propriété centrale ─────────────────────────────────────

def test_univers_nutilise_aucune_donnee_posterieure_a_t():
    """Propriété CENTRALE (§1) : le panier à t ignore tout ce qui suit t.

    L'actif piège a un volume mille fois supérieur à tous les autres, mais il
    n'existe qu'à partir du jour 90. À t = jour 60 il doit être invisible — le
    voir entrerait dans le panier un gagnant sélectionné rétroactivement.
    """
    data = {
        "BTCUSDT": series(120, 0, 0.002, volume=5e6),
        "ETHUSDT": series(120, 0, 0.001, volume=3e6),
        "ADAUSDT": series(120, 0, 0.0005, volume=2e6),
        "PIEGEUSDT": series(30, 90, 0.01, volume=9e9),
    }
    keep, why = select_universe(data, 60 * DAY, basket_size=10,
                                liquidity_lookback_d=30, min_history_d=25,
                                max_gap_bars=12)
    assert "PIEGEUSDT" not in keep
    assert "aucune donnée avant t" in why["PIEGEUSDT"]

    plus_tard, _ = select_universe(data, 119 * DAY, basket_size=10,
                                   liquidity_lookback_d=30, min_history_d=25,
                                   max_gap_bars=12)
    assert "PIEGEUSDT" in plus_tard, "une fois listé, il entre normalement"


def test_univers_de_2021_differe_de_celui_de_2026():
    """Le panier doit ÉVOLUER : un univers figé est un univers biaisé."""
    data = {
        "OLDUSDT": series(400, 0, 0.001, volume=5e6),
        "MIDUSDT": series(400, 0, 0.001, volume=4e6),
        "BASEUSDT2": series(400, 0, 0.001, volume=3e6),
        "LATEUSDT": series(150, 250, 0.001, volume=8e6),
    }
    tot = dict(basket_size=3, liquidity_lookback_d=30, min_history_d=25, max_gap_bars=12)
    early, _ = select_universe(data, 100 * DAY, **tot)
    late, _ = select_universe(data, 390 * DAY, **tot)
    assert "LATEUSDT" not in early
    assert "LATEUSDT" in late
    assert set(early) != set(late)


def test_historique_insuffisant_exclut():
    data = {"AAAUSDT": series(10, 0, 0.001), "BBBUSDT": series(200, 0, 0.001)}
    keep, why = select_universe(data, 200 * DAY, basket_size=5,
                                liquidity_lookback_d=30, min_history_d=60,
                                max_gap_bars=12)
    assert "AAAUSDT" not in keep


def test_stablecoins_et_rebasing_exclus():
    data = {s: series(120, 0, 0.001, volume=9e9)
            for s in ("USDCUSDT", "DAIUSDT", "AMPLUSDT", "BTCUSDT")}
    keep, why = select_universe(data, 100 * DAY, basket_size=10,
                                liquidity_lookback_d=30, min_history_d=25,
                                max_gap_bars=12)
    assert keep == ["BTCUSDT"]
    assert why["USDCUSDT"] == "stablecoin"
    assert why["AMPLUSDT"] == "supply rebasing"


def test_trous_de_donnees_excluent():
    trouee = series(120, 0, 0.001)
    del trouee[40:70]                                  # 30 barres manquantes
    data = {"TROUUSDT": trouee, "BTCUSDT": series(120, 0, 0.001)}
    keep, why = select_universe(data, 110 * DAY, basket_size=5,
                                liquidity_lookback_d=30, min_history_d=100,
                                max_gap_bars=12)
    assert "TROUUSDT" not in keep and "manquantes" in why["TROUUSDT"]


def test_liquidite_mesuree_par_mediane_pas_moyenne():
    """Un unique jour de volume aberrant ne doit pas propulser un illiquide."""
    calme = series(120, 0, 0.001, volume=1e6)
    pic = series(120, 0, 0.001, volume=1e5)
    pic[100]["volume"] = 1e12                          # un seul jour délirant
    data = {"CALMEUSDT": calme, "PICUSDT": pic}
    keep, _ = select_universe(data, 119 * DAY, basket_size=1,
                              liquidity_lookback_d=30, min_history_d=25,
                              max_gap_bars=12)
    assert keep == ["CALMEUSDT"]


# ── §12 Signal ──────────────────────────────────────────────────────────────

def test_le_skip_exclut_bien_les_derniers_jours():
    """Les `skip_d` derniers jours ne doivent pas entrer dans le score."""
    steps = [0.0] * 100
    steps[95:100] = [0.5] * 5                           # explosion sur les 5 derniers
    data = series(100, 0, step=steps)
    sans_skip = momentum_score(data, 100 * DAY, lookback_d=20, skip_d=0)[0]
    avec_skip = momentum_score(data, 100 * DAY, lookback_d=20, skip_d=6)[0]
    assert sans_skip > 1.0, "sans skip, l'explosion récente domine"
    assert abs(avec_skip) < 1e-9, "avec skip, elle est hors fenêtre"


def test_score_invariant_par_echelle_des_prix():
    base = series(80, 0, 0.003)
    grand = [{**c, "close": c["close"] * 1e6, "open": c["open"] * 1e6} for c in base]
    s1 = momentum_score(base, 80 * DAY, 21, 2)[0]
    s2 = momentum_score(grand, 80 * DAY, 21, 2)[0]
    assert s1 == pytest.approx(s2, rel=1e-12)


def test_score_ignore_les_donnees_futures():
    data = series(120, 0, 0.001)
    tronquee = [c for c in data if c["ts"] < 60 * DAY]
    assert (momentum_score(data, 60 * DAY, 21, 2)[0]
            == pytest.approx(momentum_score(tronquee, 60 * DAY, 21, 2)[0]))


def test_score_non_calculable_exclut_du_classement():
    """§2 : jamais de rang par défaut. Inventer un rang, c'est inventer de
    l'information au milieu du seul signal de la stratégie."""
    data = {"BONUSDT": series(120, 0, 0.002), "COURTUSDT": series(3, 117, 0.002)}
    ranked = rank_scores(data, ["BONUSDT", "COURTUSDT"], 120 * DAY, 21, 2)
    assert [a.symbol for a in ranked] == ["BONUSDT"]


def test_classement_deterministe_sur_ex_aequo():
    data = {s: series(120, 0, 0.001) for s in ("BUSDT", "AUSDT", "CUSDT")}
    ranks = [tuple(a.symbol for a in rank_scores(data, sorted(data), 100 * DAY, 21, 2))
             for _ in range(5)]
    assert len(set(ranks)) == 1, "des ex æquo doivent se départager de façon stable"


# ── §12 Portefeuille ────────────────────────────────────────────────────────

def test_neutralite_dollar_au_rebalancement():
    prices = {s: 100.0 for s in "ABCDEF"}
    pf = build_portfolio(list("ABC"), list("DEF"), 10_000.0, 1.0, 0.20, prices)
    assert pf.net_notional() == pytest.approx(0.0, abs=1e-9)
    assert pf.dollar_neutrality() == pytest.approx(0.0, abs=1e-12)


def test_plafond_par_actif_respecte_quand_le_panier_retrecit():
    """§3 : le plafond réduit l'exposition brute plutôt que de concentrer."""
    prices = {"A": 100.0, "B": 100.0}
    pf = build_portfolio(["A"], ["B"], 10_000.0, 1.0, 0.20, prices)
    assert max(abs(l.weight) for l in pf.legs.values()) <= 0.20 + 1e-12
    assert pf.gross_notional() == pytest.approx(4_000.0)
    assert pf.dollar_neutrality() == pytest.approx(0.0, abs=1e-12)


def test_hysteresis_conserve_au_rang_n_plus_un_sort_au_dela():
    """§4 : un actif au rang n_legs+1 reste, au rang n_legs+hysteresis+1 sort."""
    from momentum.core import AssetScore

    def ranked(order):
        return [AssetScore(symbol=s, score=-i, rank=i + 1) for i, s in enumerate(order)]

    held = Portfolio(legs=build_portfolio(["A"], ["F"], 1000.0, 1.0, 0.5,
                                          {s: 10.0 for s in "AF"}).legs)

    # A glisse au rang 2 (n_legs=1, hysteresis=2) → dans la bande → conservé
    longs, _, _ = target_symbols(ranked(list("BACDEF")), n_legs=1, held=held,
                                 hysteresis_rank=2)
    assert "A" in longs

    # A glisse au rang 4 → hors bande (1 + 2 = 3) → remplacé
    longs, _, _ = target_symbols(ranked(list("BCDAEF")), n_legs=1, held=held,
                                 hysteresis_rank=2)
    assert "A" not in longs and longs == ["B"]


def test_hysteresis_reduit_le_churn_sur_serie_oscillante():
    """Propriété du §12 : churn AVEC hystérésis ≤ churn SANS."""
    from momentum.core import AssetScore

    rng = random.Random(7)
    order = list("ABCDEF")
    churn = {0: 0, 2: 0}
    held = {0: None, 2: None}

    for _ in range(60):
        rng.shuffle(order)
        ranked = [AssetScore(symbol=s, score=-i, rank=i + 1) for i, s in enumerate(order)]
        for hyst in (0, 2):
            longs, shorts, _ = target_symbols(ranked, n_legs=2, held=held[hyst],
                                              hysteresis_rank=hyst)
            prev = held[hyst]
            if prev is not None:
                churn[hyst] += len(set(longs) - set(prev.longs))
                churn[hyst] += len(set(shorts) - set(prev.shorts))
            pf = Portfolio()
            pf.legs = build_portfolio(longs, shorts, 1000.0, 1.0, 0.5,
                                      {s: 10.0 for s in order}).legs
            held[hyst] = pf

    assert churn[2] <= churn[0], f"hystérésis inefficace: {churn[2]} > {churn[0]}"


def test_univers_trop_etroit_ne_produit_aucune_jambe():
    from momentum.core import AssetScore

    ranked = [AssetScore(symbol=s, score=-i, rank=i + 1) for i, s in enumerate("AB")]
    longs, shorts, decisions = target_symbols(ranked, n_legs=3)
    assert not longs and not shorts and "__univers__" in decisions


# ── §12 Risque ──────────────────────────────────────────────────────────────

def test_disjoncteur_drawdown_et_redemarrage_refuse(cfg):
    """§5 : un disjoncteur qui se réarme seul n'est pas un disjoncteur."""
    agent = MomentumAgent(cfg, 10_000.0)
    assert not agent.check_drawdown(6_100.0)          # −39 %
    assert agent.check_drawdown(5_900.0)              # −41 %

    agent.halt("drawdown −41 %", 0)
    assert agent.state.halted
    with pytest.raises(CircuitBreakerTripped, match="intervention humaine"):
        agent.restart()
    agent.restart(human_override=True)
    assert not agent.state.halted


def test_agent_arrete_ne_rebalance_plus(cfg):
    agent = MomentumAgent(cfg, 10_000.0)
    agent.halt("test", 0)
    assert agent.rebalance(0, {}, {}, 10_000.0) is None


def test_fenetre_de_rebalancement_ancree_sur_lepoque(cfg):
    """Un redémarrage ne doit pas décaler la grille temporelle."""
    agent = MomentumAgent(cfg, 10_000.0)
    hours = [t for t in range(0, 6 * DAY, 3_600_000) if agent.is_rebalance_time(t)]
    assert len(hours) == 3
    for t in hours:
        assert (t // DAY) % cfg.rebalance.every_d == 0
        assert (t % DAY) // 3_600_000 == cfg.rebalance.hour_utc


# ── §12 Comptabilité (§7) ───────────────────────────────────────────────────

def test_net_est_la_somme_exacte_des_composantes():
    pnl = MomentumPnL(pnl_long=300.0, pnl_short=-120.0, funding_long=15.0,
                      funding_short=-40.0, fees_maker=8.0, fees_taker=2.0)
    assert pnl.funding_pnl == pytest.approx(-25.0)
    assert pnl.net == pytest.approx(300.0 - 120.0 - (-25.0) - 10.0)


def test_funding_signe_par_jambe():
    """§3 : le short est censé RECEVOIR le funding en régime normal."""
    acct = MomentumAccounting(0.0, 0.0)
    acct.accrue_funding(side=1, notional=1_000.0, rate=0.0001)    # long paie
    acct.accrue_funding(side=-1, notional=1_000.0, rate=0.0001)   # short reçoit
    assert acct.pnl.funding_long == pytest.approx(0.1)
    assert acct.pnl.funding_short == pytest.approx(-0.1)
    assert acct.pnl.funding_pnl == pytest.approx(0.0)


def test_edge_location_signale_la_domination_du_long():
    """Diagnostic §7 : un long-short dont tout vient du long est du beta."""
    assert "LONGUE" in MomentumPnL(pnl_long=900.0, pnl_short=50.0).edge_location
    assert "COURTE" in MomentumPnL(pnl_long=20.0, pnl_short=-900.0).edge_location
    assert "réparti" in MomentumPnL(pnl_long=500.0, pnl_short=-400.0).edge_location


def test_part_taker_visible():
    acct = MomentumAccounting(0.0001, 0.001)
    acct.charge_fee(10_000.0, maker=True)
    acct.charge_fee(10_000.0, maker=False)
    assert acct.pnl.taker_share == pytest.approx(10 / 11, rel=1e-6)


def test_drawdown_et_profit_factor():
    curve = [(0, 10_000.0), (1, 12_000.0), (2, 7_000.0), (3, 9_000.0)]
    assert max_drawdown(curve, 10_000.0) == pytest.approx((12_000 - 7_000) / 12_000)
    assert profit_factor(curve) == pytest.approx((2_000 + 2_000) / 5_000)


def test_churn_compte_les_jambes_remplacees():
    ev = RebalanceEvent(ts_ms=0, opened=["A", "B"], closed=["C"], held=["D"])
    assert ev.churn == 3


# ── §12 Anti-conditionnement (§8) ───────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "signal.lookback_d", "signal.skip_d", "portfolio.n_legs",
    "universe.basket_size", "rebalance.every_d", "portfolio.hysteresis_rank",
])
def test_conditionner_le_signal_est_rejete(path):
    """§8 : le signal est hors de portée. Le conditionner reviendrait à tester
    une autre hypothèse que celle enregistrée au registre."""
    with pytest.raises(SignalConditioningError, match="conditionnement interdit"):
        config_mod.assert_no_signal_conditioning([path])


def test_seule_lexposition_brute_est_conditionnable():
    config_mod.assert_no_signal_conditioning(["portfolio.gross_exposure_frac"])


# ── §12 Configuration ───────────────────────────────────────────────────────

def test_placebo_par_date_est_rejete():
    """§9.2 amendé : la permutation par date biaiserait le critère principal."""
    with pytest.raises(MomentumConfigError, match="PERSISTANTE"):
        config_mod.from_dict({
            "backtest": {"placebo": {"method": "per_date"},
                         "windows": [{"label": "a", "days": 1},
                                     {"label": "b", "days": 1}]}})


def test_tirages_insuffisants_rejetes():
    with pytest.raises(MomentumConfigError, match="trop faible"):
        config_mod.from_dict({
            "backtest": {"placebo": {"n_draws": 20, "alpha": 0.0167},
                         "windows": [{"label": "a", "days": 1},
                                     {"label": "b", "days": 1}]}})


def test_une_seule_fenetre_est_rejetee():
    """§9.3 : deux fenêtres obligatoires. Une stratégie validée sur un seul
    régime ne l'est pas."""
    with pytest.raises(MomentumConfigError, match="DEUX fenêtres"):
        config_mod.from_dict({"backtest": {"windows": [{"label": "a", "days": 1}]}})


def test_skip_superieur_au_lookback_rejete():
    with pytest.raises(MomentumConfigError, match="skip_d"):
        config_mod.from_dict({"signal": {"lookback_d": 5, "skip_d": 5}})


def test_jambes_trop_nombreuses_pour_le_panier_rejetees():
    with pytest.raises(MomentumConfigError, match="chevaucheraient"):
        config_mod.from_dict({
            "universe": {"basket_size": 4}, "portfolio": {"n_legs": 3},
            # Fenêtres valides : sans elles, c'est la validation du §9.3 qui
            # lèverait d'abord et le test ne prouverait rien sur les jambes.
            "backtest": {"windows": [{"label": "a", "days": 1},
                                     {"label": "b", "days": 1}]}})


def test_config_gelee_impose_60_tirages(cfg):
    """Registre entrée n°3 : α = 0,0167 ⇒ 60 tirages minimum."""
    assert cfg.backtest.placebo.alpha == pytest.approx(0.0167)
    assert cfg.backtest.placebo.n_draws >= 60
    assert cfg.backtest.placebo.method == "persistent_score_permutation"


def test_source_de_donnees_imposee(cfg):
    """§9.1 amendé : perps USD-M, pas de spot."""
    assert cfg.data.market == "binance_perp_usdm"
    with pytest.raises(MomentumConfigError, match="perps USD-M"):
        config_mod.from_dict({"data": {"market": "binance_spot"},
                              "backtest": {"windows": [{"label": "a", "days": 1},
                                                       {"label": "b", "days": 1}]}})


def test_deux_fenetres_dont_la_bear_2021(cfg):
    labels = [w.label for w in cfg.backtest.windows]
    assert "recente" in labels
    assert any("2021" in lb for lb in labels), (
        "la fenêtre bear doit démarrer en 2021 — au 2020-01 il n'existait que 3 perps")


# ── §12 Compteurs de branche (§9.3) ─────────────────────────────────────────

def test_compteurs_signalent_une_branche_jamais_empruntee(cfg):
    """Leçon de l'A/B fantôme du GridAgent : un chemin jamais emprunté doit
    crier, pas se taire."""
    agent = MomentumAgent(cfg, 10_000.0)
    assert "hysteresis_saved" in agent.branches.never_taken()
    agent.branches.hit("hysteresis_saved")
    assert "hysteresis_saved" not in agent.branches.never_taken()


def test_branche_non_declaree_leve(cfg):
    agent = MomentumAgent(cfg, 10_000.0)
    with pytest.raises(KeyError, match="non déclarée"):
        agent.branches.hit("inventee")


# ── §8 Postures ─────────────────────────────────────────────────────────────

def test_posture_defensive_reduit_lexposition(cfg):
    from momentum.adaptive import apply_posture

    reduced, notes = apply_posture(cfg, "defensive")
    assert reduced.portfolio.gross_exposure_frac < cfg.portfolio.gross_exposure_frac
    assert any("gross_exposure_frac" in n for n in notes)


def test_aucune_posture_naugmente_lexposition(cfg):
    """§8 : le silence de la spec sur l'augmentation se lit comme une
    interdiction. Le tirage §9 s'est fait à gross 100 %."""
    from momentum.adaptive import apply_posture

    for posture in ("neutral", "aggressive"):
        out, _ = apply_posture(cfg, posture)
        assert out.portfolio.gross_exposure_frac <= cfg.portfolio.gross_exposure_frac


def test_apm_degrade_reduit_de_moitie(cfg):
    from momentum.adaptive import apply_posture

    out, notes = apply_posture(cfg, "aggressive", degraded=True)
    assert out.portfolio.gross_exposure_frac == pytest.approx(
        cfg.portfolio.gross_exposure_frac * 0.5)
    assert any("dégradé" in n for n in notes)


def test_plan_de_conditionnement_du_signal_rejete():
    from momentum.adaptive import validate_conditioning_plan

    with pytest.raises(SignalConditioningError):
        validate_conditioning_plan({"signal.lookback_d": 30})
    with pytest.raises(SignalConditioningError, match="RÉDUIRE"):
        validate_conditioning_plan({"portfolio.gross_exposure_frac": 1.5})
    validate_conditioning_plan({"portfolio.gross_exposure_frac": 0.5})


def test_registre_nexpose_pas_le_signal(cfg):
    from momentum.adaptive import params_for_registry

    params = params_for_registry(cfg)
    assert set(params) == {"portfolio.gross_exposure_frac"}


def test_gross_pnl_abs_est_exporte():
    """Régression : son absence rendait le critère de frais §9.4 inévaluable,
    donc compté en échec pour une raison étrangère à la stratégie."""
    acct = MomentumAccounting(0.0, 0.0)
    acct.realize(side=1, amount=250.0)
    acct.realize(side=-1, amount=-100.0)
    d = acct.pnl.as_dict()
    assert d["gross_pnl_abs"] == pytest.approx(350.0)
    assert d["fees"] == 0.0

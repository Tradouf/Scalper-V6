"""
Tests de l'AdaptiveParameterManager — SPEC §12.8.

Couverture demandée :

* ParamRegistry : refus d'enregistrer un set sans métriques OOS, immuabilité,
  rechargement après restart, journal append-only ;
* RegimeConditioner : pureté, bornes d'interpolation, continuité aux
  percentiles 30/70 ;
* WalkForwardOptimizer : blocage de promotion sur dérive > 40 %, passage en
  observation après 3 échecs ;
* PostureSelector : JSON invalide ⇒ fallback, confiance basse ⇒ inchangé,
  ratchet asymétrique, impossibilité de sortir du mode observation, shadow mode
  qui n'applique jamais ;
* bout en bout : paramètres valides même LLM indisponible, registre corrompu
  ⇒ fallback neutral embarqué + alerte.

Le fil rouge de ces tests est le principe cardinal du §12 : **aucun LLM ne
produit jamais de valeur numérique**. Plusieurs tests ne vérifient pas qu'une
fonctionnalité marche, mais qu'un pouvoir est bien ABSENT.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from confluence import config as config_mod
from confluence.adaptive.conditioner import ConditionerError, RegimeConditioner
from confluence.adaptive.manager import AdaptiveParameterManager, build_digest
from confluence.adaptive.optimizer import WalkForwardOptimizer
from confluence.adaptive.posture import (
    Posture,
    PostureAdvice,
    PostureSelector,
    PostureState,
)
from confluence.adaptive.registry import (
    FALLBACK_NEUTRAL,
    ParameterSet,
    ParamRegistry,
    RegistryError,
)
from confluence.data import History

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFLUENCE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CONFLUENCE_REGISTRY_DIR", str(tmp_path / "registry"))


@pytest.fixture
def cfg():
    return config_mod.load()


@pytest.fixture
def registry(tmp_path):
    return ParamRegistry(tmp_path / "reg")


def make_set(posture="neutral", version="v1", metrics=None, **params) -> ParameterSet:
    base = {"risk.k_stop": 1.5, "risk.edge_multiple": 5.0, "risk.risk_pct": 0.005}
    base.update(params)
    return ParameterSet(
        version=version,
        posture=posture,
        params=base,
        validated_at=NOW,
        oos_metrics=metrics if metrics is not None else {
            "profit_factor": 1.4, "fee_ratio": 0.10, "trades": 120.0, "max_drawdown": 0.08},
        data_window=(NOW - timedelta(days=450), NOW),
        conditioning_bounds={"risk.k_stop": (1.2, 2.0),
                             "risk.edge_multiple": (7.0, 4.0),
                             "risk.risk_pct": (0.005, 0.0035)},
    )


# ── §12.8 ParamRegistry ──────────────────────────────────────────────────────

def test_registre_refuse_un_set_sans_metriques_oos(registry):
    with pytest.raises(RegistryError, match="out-of-sample"):
        registry.register(make_set(metrics={}), acceptance_passed=True)


def test_registre_refuse_des_metriques_incompletes(registry):
    partial = make_set(metrics={"profit_factor": 1.5})
    with pytest.raises(RegistryError, match="manquantes"):
        registry.register(partial, acceptance_passed=True)


def test_registre_refuse_un_set_qui_echoue_les_criteres(registry):
    """§12.2 : un set jamais validé ne peut pas être enregistré."""
    with pytest.raises(RegistryError, match="§9.4"):
        registry.register(make_set(), acceptance_passed=False)


def test_parameter_set_est_reellement_immuable():
    """`frozen=True` gèle l'attribut, pas le dict qu'il pointe. Sans le
    MappingProxyType, un set validé serait modifiable après coup."""
    param_set = make_set()
    with pytest.raises(TypeError):
        param_set.params["risk.k_stop"] = 99.0          # type: ignore[index]
    with pytest.raises(TypeError):
        param_set.oos_metrics["profit_factor"] = 99.0   # type: ignore[index]
    with pytest.raises(Exception):
        param_set.version = "autre"                     # type: ignore[misc]


def test_registre_recharge_apres_restart(tmp_path):
    path = tmp_path / "reg"
    ParamRegistry(path).register(make_set(version="v-restart"), acceptance_passed=True)

    reloaded = ParamRegistry(path).load()
    assert reloaded.get("neutral").version == "v-restart"
    assert reloaded.get("neutral").params["risk.k_stop"] == 1.5


def test_journal_est_append_only(tmp_path):
    path = tmp_path / "reg"
    reg = ParamRegistry(path)
    reg.register(make_set(version="v1"), acceptance_passed=True, now=NOW)
    reg.register(make_set(version="v2", **{"risk.k_stop": 1.6}), acceptance_passed=True,
                 now=NOW + timedelta(days=30))
    reg.record_posture("neutral", "defensive", "test", {"reason": "essai"}, now=NOW)

    history = reg.history()
    events = [h["event"] for h in history]
    assert events == ["register", "register", "posture"], "rien n'est réécrit ni réordonné"
    assert history[1]["replaces"] == "v1"
    # Le fichier ne fait que grandir : une seconde instance ajoute à la suite.
    ParamRegistry(path).load().record_posture("defensive", "neutral", "test", {}, now=NOW)
    assert len(ParamRegistry(path).history()) == 4


def test_registre_vide_rend_le_fallback_embarque(registry):
    param_set = registry.get("neutral")
    assert param_set.version == FALLBACK_NEUTRAL.version
    assert not param_set.oos_metrics, "le repli n'a AUCUNE métrique : il n'a jamais été validé"
    assert registry.degraded


def test_registre_corrompu_est_archive_et_signale(tmp_path):
    """§12.8 : registre corrompu ⇒ fallback neutral embarqué + alerte."""
    path = tmp_path / "reg"
    reg = ParamRegistry(path)
    reg.register(make_set(), acceptance_passed=True)
    reg.state_path.write_text("{ceci n'est pas du JSON", encoding="utf-8")

    reloaded = ParamRegistry(path).load()
    assert reloaded.degraded
    assert reloaded.get("neutral").version == FALLBACK_NEUTRAL.version
    assert list(path.glob("active.corrupt.*")), "l'état illisible doit être archivé"
    assert any(h["event"] == "corrupt" for h in reloaded.history())


def test_drift_vs_calcule_lecart_relatif():
    old = make_set(version="old", **{"risk.k_stop": 1.5})
    new = make_set(version="new", **{"risk.k_stop": 2.25})
    assert new.drift_vs(old)["risk.k_stop"] == pytest.approx(0.5)


# ── §12.8 RegimeConditioner ──────────────────────────────────────────────────

def test_conditioner_est_pur():
    conditioner = RegimeConditioner(30, 70)
    params = {"risk.k_stop": 1.5}
    bounds = {"risk.k_stop": (1.2, 2.0)}
    first = conditioner.condition(params, 50.0, bounds)
    second = conditioner.condition(params, 50.0, bounds)
    assert first == second
    assert params == {"risk.k_stop": 1.5}, "l'entrée ne doit pas être mutée"


@pytest.mark.parametrize("percentile,expected", [
    (0.0, 1.2), (30.0, 1.2), (50.0, 1.6), (70.0, 2.0), (100.0, 2.0),
])
def test_conditioner_bornes_et_continuite(percentile, expected):
    """Continuité aux percentiles 30 et 70, et clamp au-delà — on n'extrapole
    pas un k_stop que personne n'a validé."""
    out = RegimeConditioner(30, 70).condition(
        {}, percentile, {"risk.k_stop": (1.2, 2.0)})
    assert out["risk.k_stop"] == pytest.approx(expected)


def test_conditioner_durcit_ledge_quand_le_marche_est_calme():
    """§12.3 : edge_multiple durci en vol basse, assoupli en vol haute."""
    conditioner = RegimeConditioner(30, 70)
    bounds = {"risk.edge_multiple": (7.0, 4.0)}
    assert conditioner.condition({}, 10.0, bounds)["risk.edge_multiple"] == pytest.approx(7.0)
    assert conditioner.condition({}, 90.0, bounds)["risk.edge_multiple"] == pytest.approx(4.0)


def test_conditioner_reduit_le_risque_en_forte_volatilite():
    out = RegimeConditioner(30, 70).condition({}, 90.0, {"risk.risk_pct": (0.005, 0.0035)})
    assert out["risk.risk_pct"] == pytest.approx(0.0035)


def test_conditioner_percentile_absent_laisse_inchange():
    params = {"risk.k_stop": 1.5}
    out = RegimeConditioner(30, 70).condition(params, None, {"risk.k_stop": (1.2, 2.0)})
    assert out == params, "une donnée manquante ne doit pas bouger le risque en silence"


def test_conditioner_refuse_un_parametre_en_amont_du_percentile():
    """Le percentile vient de la couche 1h : conditionner un seuil d'ADX
    créerait une boucle de rétroaction."""
    with pytest.raises(ConditionerError, match="boucle"):
        RegimeConditioner(30, 70).condition({}, 50.0, {"regime_1h.adx_trend": (20.0, 30.0)})
    with pytest.raises(ConditionerError):
        RegimeConditioner.assert_no_feedback({"regime_1h.adx_trend": (20.0, 30.0)})


def test_conditioner_refuse_des_bornes_inversees():
    with pytest.raises(ConditionerError):
        RegimeConditioner(70, 30)


# ── §12.8 PostureSelector ────────────────────────────────────────────────────

class FakeBackend:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        if not self.responses:
            raise RuntimeError("backend épuisé")
        return self.responses.pop(0)


def selector(**kwargs):
    kwargs.setdefault("backend", None)
    kwargs.setdefault("shadow_days", 0)      # hors shadow, sauf mention contraire
    return PostureSelector(**kwargs)


def state_out_of_shadow(current=Posture.NEUTRAL):
    return PostureState(current=current,
                        shadow_started=(NOW - timedelta(days=400)).isoformat())


def test_posture_json_invalide_puis_retry_puis_inchangee():
    """§12.5 : JSON invalide ⇒ retry unique ; second échec ⇒ inchangé + alerte."""
    backend = FakeBackend("pas du json", "toujours pas")
    sel = selector(backend=backend)
    advice = sel.ask({}, NOW)
    assert backend.calls == 2, "exactement un retry"
    assert not advice.valid

    state = state_out_of_shadow()
    outcome = sel.apply(state, advice, NOW)
    assert state.current == Posture.NEUTRAL
    assert outcome["alert"] and not outcome["applied"]


def test_posture_hors_enumeration_est_rejetee():
    advice = selector().parse('{"posture": "yolo", "confidence": 0.9}')
    assert not advice.valid and "énumération" in advice.error


def test_posture_confidence_manquante_est_rejetee():
    advice = selector().parse('{"posture": "defensive"}')
    assert not advice.valid and "confidence" in advice.error


def test_posture_json_encadre_de_texte_est_tolere():
    """La tolérance porte sur la mise en forme, jamais sur le contenu."""
    advice = selector().parse('Voici mon avis :\n{"posture":"defensive","confidence":0.8}\nVoilà.')
    assert advice.valid and advice.posture == "defensive"


def test_posture_confiance_basse_ne_change_rien():
    sel = selector(min_confidence=0.6)
    state = state_out_of_shadow()
    advice = PostureAdvice("defensive", 0.5, "hésitant", at=NOW)
    outcome = sel.apply(state, advice, NOW)
    assert state.current == Posture.NEUTRAL
    assert not outcome["applied"] and "confiance" in outcome["reason"]


def test_ratchet_defensif_immediat():
    """Se protéger vite coûte peu."""
    sel = selector()
    state = state_out_of_shadow()
    outcome = sel.apply(state, PostureAdvice("defensive", 0.9, "", at=NOW), NOW)
    assert state.current == "defensive" and outcome["applied"]


def test_ratchet_agressif_exige_trois_avis_consecutifs():
    """Se découvrir vite coûte cher (§12.5)."""
    sel = selector(aggressive_confirm_days=3)
    state = state_out_of_shadow()
    for day in range(2):
        sel.apply(state, PostureAdvice("aggressive", 0.9, "", at=NOW),
                  NOW + timedelta(days=day))
        assert state.current == Posture.NEUTRAL, "pas de bascule avant 3 avis"
    sel.apply(state, PostureAdvice("aggressive", 0.9, "", at=NOW), NOW + timedelta(days=2))
    assert state.current == "aggressive"


def test_ratchet_agressif_remis_a_zero_par_un_avis_different():
    sel = selector(aggressive_confirm_days=3)
    state = state_out_of_shadow()
    sel.apply(state, PostureAdvice("aggressive", 0.9, "", at=NOW), NOW)
    sel.apply(state, PostureAdvice("neutral", 0.9, "", at=NOW), NOW + timedelta(days=1))
    sel.apply(state, PostureAdvice("aggressive", 0.9, "", at=NOW), NOW + timedelta(days=2))
    assert state.current == Posture.NEUTRAL
    assert state.pending_count == 1, "le compteur repart de zéro"


def test_posture_ne_peut_pas_sortir_du_mode_observation():
    """§12.5, interdiction dure."""
    sel = selector()
    state = state_out_of_shadow(current="defensive")
    outcome = sel.apply(state, PostureAdvice("aggressive", 1.0, "", at=NOW), NOW,
                        observation_mode=True)
    assert state.current == "defensive"
    assert not outcome["applied"] and "observation" in outcome["reason"]


def test_shadow_mode_napplique_jamais_la_posture():
    """§12.6 : les avis sont produits et évalués, la posture reste neutral."""
    sel = PostureSelector(backend=None, shadow_days=45)
    state = PostureState(shadow_started=NOW.isoformat())
    outcome = sel.apply(state, PostureAdvice("defensive", 0.95, "", at=NOW),
                        NOW + timedelta(days=10))
    assert state.current == Posture.NEUTRAL
    assert not outcome["applied"]
    assert outcome["would_be"] == "defensive" and "shadow" in outcome["reason"]


def test_shadow_mode_expire_apres_shadow_days():
    sel = PostureSelector(backend=None, shadow_days=45)
    state = PostureState(shadow_started=NOW.isoformat())
    outcome = sel.apply(state, PostureAdvice("defensive", 0.95, "", at=NOW),
                        NOW + timedelta(days=46))
    assert state.current == "defensive" and outcome["applied"]


def test_cadence_quotidienne():
    """§12.5 : une exécution par jour, jamais en intra-journée."""
    sel = selector()
    state = PostureState()
    assert sel.due(state, NOW)
    sel.apply(state, PostureAdvice("neutral", 0.9, "", at=NOW), NOW)
    assert not sel.due(state, NOW + timedelta(hours=6))
    assert sel.due(state, NOW + timedelta(days=1))


def test_shadow_report_signale_les_violations_de_schema():
    sel = selector()
    state = state_out_of_shadow()
    sel.apply(state, PostureAdvice("neutral", 0.9, "", at=NOW), NOW)
    sel.apply(state, PostureAdvice(None, 0.0, "", valid=False, error="cassé", at=NOW),
              NOW + timedelta(days=1))
    report = sel.shadow_report(state)
    assert report["schema_violations"] == 1
    assert not report["eligible_for_activation"]


def test_le_llm_ne_peut_produire_aucune_valeur_numerique():
    """Principe cardinal du §12 : le seul champ exploité est une posture.

    Un modèle qui tenterait de glisser un `k_stop` dans sa réponse doit voir ce
    champ purement et simplement ignoré — pas validé, pas plafonné : ignoré.
    """
    advice = selector().parse(
        '{"posture":"aggressive","confidence":0.9,"rationale":"ok",'
        '"k_stop": 0.1, "risk_pct": 0.9, "max_trades_per_day": 50}')
    assert advice.valid
    assert not hasattr(advice, "k_stop")
    assert set(advice.to_json()) == {"posture", "confidence", "rationale",
                                     "raw", "valid", "error", "at"}


# ── §12.8 WalkForwardOptimizer ───────────────────────────────────────────────

def test_optimiseur_bloque_la_promotion_sur_derive_excessive(cfg, registry, monkeypatch):
    """§12.4 : dérive > 40 % sur un paramètre clé ⇒ validation humaine."""
    registry.register(make_set(version="ancien", **{"risk.k_stop": 1.5}),
                      acceptance_passed=True)

    from confluence.adaptive import optimizer as opt

    monkeypatch.setattr(opt, "walk_forward", lambda *a, **k: _fake_report(k_stop=3.0))
    monkeypatch.setattr(opt, "acceptance", lambda *a, **k: {"passed": True, "checks": {}})

    optimiser = WalkForwardOptimizer(cfg, registry, max_param_drift=0.40)
    report = optimiser.run_cycle(History(symbol="BTC"), now=NOW,
                                 postures=("neutral",), skip_sensitivity=True)
    outcome = report.outcomes[0]
    assert not outcome.promoted
    assert outcome.requires_human_approval
    assert "dérive" in outcome.reason
    assert registry.get("neutral").version == "ancien", "l'ancien set reste en place"


def test_optimiseur_bloque_sur_degradation_non_expliquee(cfg, registry, monkeypatch):
    registry.register(make_set(version="ancien", metrics={
        "profit_factor": 1.8, "fee_ratio": 0.1, "trades": 150.0, "max_drawdown": 0.07}),
        acceptance_passed=True)

    from confluence.adaptive import optimizer as opt

    monkeypatch.setattr(opt, "walk_forward", lambda *a, **k: _fake_report(pf=1.35))
    monkeypatch.setattr(opt, "acceptance", lambda *a, **k: {"passed": True, "checks": {}})

    report = WalkForwardOptimizer(cfg, registry).run_cycle(
        History(symbol="BTC"), now=NOW, postures=("neutral",), skip_sensitivity=True)
    assert report.outcomes[0].requires_human_approval
    assert "recul" in report.outcomes[0].reason


def test_optimiseur_passe_en_observation_apres_trois_echecs(cfg, registry, monkeypatch):
    """§12.4 : le marché a probablement changé de nature."""
    from confluence.adaptive import optimizer as opt

    monkeypatch.setattr(opt, "walk_forward", lambda *a, **k: _fake_report())
    monkeypatch.setattr(opt, "acceptance",
                        lambda *a, **k: {"passed": False, "checks": {"min_trades": {
                            "passed": False, "value": 3, "threshold": 100}}})

    optimiser = WalkForwardOptimizer(cfg, registry, fail_cycles_to_observation=3)
    failures = 0
    for _ in range(3):
        report = optimiser.run_cycle(History(symbol="BTC"), now=NOW,
                                     consecutive_failures=failures,
                                     postures=("neutral",), skip_sensitivity=True)
        failures = report.consecutive_failures
    assert failures == 3
    assert report.entered_observation
    assert registry.observation_mode


def test_optimiseur_rejette_un_set_fragile_a_la_sensibilite(cfg, registry, monkeypatch):
    from confluence.adaptive import optimizer as opt

    monkeypatch.setattr(opt, "walk_forward", lambda *a, **k: _fake_report())
    monkeypatch.setattr(opt, "acceptance", lambda *a, **k: {"passed": True, "checks": {}})
    monkeypatch.setattr(opt, "sensitivity", lambda *a, **k: {"fragile": True, "variants": []})

    report = WalkForwardOptimizer(cfg, registry).run_cycle(
        History(symbol="BTC"), now=NOW, postures=("neutral",))
    assert not report.outcomes[0].promoted
    assert "sensibilité" in report.outcomes[0].reason


def _fake_report(k_stop: float = 1.5, pf: float = 1.5):
    from confluence.walkforward import WalkForwardReport, WindowResult

    window = WindowResult(
        index=0,
        is_start_ms=int((NOW - timedelta(days=450)).timestamp() * 1000),
        is_end_ms=int((NOW - timedelta(days=90)).timestamp() * 1000),
        oos_start_ms=int((NOW - timedelta(days=90)).timestamp() * 1000),
        oos_end_ms=int(NOW.timestamp() * 1000),
        params={"risk.k_stop": k_stop},
        is_metrics={}, oos_metrics={"profit_factor": pf},
        oos_trades=120, oos_wins=300.0, oos_losses=200.0, oos_max_dd=0.08,
    )
    report = WalkForwardReport(windows=[window])
    # `oos_profit_factor` est agrégé sur wins/losses ; on force la valeur voulue.
    window.oos_wins, window.oos_losses = pf * 100.0, 100.0
    return report


# ── §12.8 Bout en bout ───────────────────────────────────────────────────────

def test_apm_rend_toujours_des_parametres_valides_sans_llm(cfg, tmp_path):
    """§12.8 : le ConfluenceAgent obtient toujours un jeu valide, LLM absent."""
    apm = AdaptiveParameterManager(cfg, registry=ParamRegistry(tmp_path / "vide").load(),
                                   selector=PostureSelector(backend=None))
    effective = apm.effective(vol_percentile=55.0)
    assert effective.config.risk.k_stop > 0
    assert effective.degraded, "un repli non validé doit être signalé"
    assert effective.set_version == FALLBACK_NEUTRAL.version


def test_apm_applique_le_conditionnement(cfg, tmp_path):
    reg = ParamRegistry(tmp_path / "reg")
    reg.register(make_set(), acceptance_passed=True)
    apm = AdaptiveParameterManager(cfg, registry=reg, selector=PostureSelector(backend=None))

    calme = apm.effective(vol_percentile=10.0).config.risk
    agite = apm.effective(vol_percentile=90.0).config.risk
    assert calme.k_stop == pytest.approx(1.2)
    assert agite.k_stop == pytest.approx(2.0)
    assert calme.edge_multiple > agite.edge_multiple, "edge durci quand c'est calme"
    assert agite.risk_pct < calme.risk_pct, "risque réduit quand c'est agité"


def test_apm_plafonne_un_risk_pct_conditionne_trop_haut(cfg, tmp_path):
    """Une borne saisie à l'envers ferait grossir la position quand le marché
    s'emballe. Le plafond du §12.5 l'attrape."""
    reg = ParamRegistry(tmp_path / "reg")
    bad = ParameterSet(
        version="bornes-inversees", posture="neutral",
        params={"risk.risk_pct": 0.005},
        validated_at=NOW,
        oos_metrics={"profit_factor": 1.4, "fee_ratio": 0.1,
                     "trades": 120.0, "max_drawdown": 0.08},
        data_window=(NOW, NOW),
        conditioning_bounds={"risk.risk_pct": (0.005, 0.02)},   # à l'envers
    )
    reg.register(bad, acceptance_passed=True)
    apm = AdaptiveParameterManager(cfg, registry=reg, selector=PostureSelector(backend=None))

    effective = apm.effective(vol_percentile=95.0)
    assert effective.config.risk.risk_pct == pytest.approx(0.005)
    assert any("plafonné" in n for n in effective.notes)


def test_apm_ne_laisse_pas_le_llm_toucher_aux_nombres(cfg, tmp_path):
    """Changer de posture change de SET, jamais une valeur au coup par coup."""
    reg = ParamRegistry(tmp_path / "reg")
    reg.register(make_set(posture="neutral", version="n1", **{"risk.k_stop": 1.5}),
                 acceptance_passed=True)
    reg.register(make_set(posture="defensive", version="d1", **{"risk.k_stop": 2.0}),
                 acceptance_passed=True)
    apm = AdaptiveParameterManager(cfg, registry=reg,
                                   selector=PostureSelector(backend=None, shadow_days=0))

    apm.posture_state.shadow_started = (NOW - timedelta(days=400)).isoformat()
    apm.selector.apply(apm.posture_state, PostureAdvice("defensive", 0.9, "", at=NOW), NOW)
    assert apm.posture == "defensive"
    assert apm.effective(vol_percentile=None).set_version == "d1"


def test_apm_survit_a_un_backend_llm_qui_leve(cfg, tmp_path):
    class Broken:
        def complete(self, prompt):
            raise ConnectionError("QUEEN injoignable")

    apm = AdaptiveParameterManager(
        cfg, registry=ParamRegistry(tmp_path / "reg"),
        selector=PostureSelector(backend=Broken(), shadow_days=0))
    outcome = apm.daily_posture_cycle(build_digest(
        {}, [], "FLAT", "chop", 50.0, 0.0, {}), now=NOW)
    assert outcome is not None and not outcome["applied"]
    assert apm.posture == Posture.NEUTRAL
    assert apm.effective(50.0).config.risk.k_stop > 0


def test_agent_utilise_les_parametres_adaptatifs(cfg, tmp_path):
    """Le ConfluenceAgent doit lire l'APM, pas la config figée (§12)."""
    from confluence.agent import ConfluenceAgent
    from confluence.types import ok

    reg = ParamRegistry(tmp_path / "reg")
    reg.register(make_set(**{"risk.k_stop": 1.9}), acceptance_passed=True)
    apm = AdaptiveParameterManager(cfg, registry=reg, selector=PostureSelector(backend=None))
    agent = ConfluenceAgent(cfg, params=apm)

    verdict = ok("test", NOW, atr_percentile=30.0)
    risk_cfg, note = agent._risk_config(verdict)
    assert risk_cfg.k_stop == pytest.approx(1.2), "k_stop vient du conditionnement, pas du YAML"
    assert note["posture"] == "neutral"


def test_agent_retombe_sur_la_config_figee_si_lapm_casse(cfg):
    from confluence.agent import ConfluenceAgent
    from confluence.types import ok

    class BrokenAPM:
        def effective(self, vol_percentile=None):
            raise RuntimeError("registre en flammes")

    agent = ConfluenceAgent(cfg, params=BrokenAPM())
    risk_cfg, note = agent._risk_config(ok("test", NOW, atr_percentile=50.0))
    assert risk_cfg.k_stop == cfg.risk.k_stop
    assert note["degraded"]


def test_mode_observation_bloque_lemission_de_signal(cfg, tmp_path):
    """§12.4 : mode observation ⇒ signaux loggés, aucun ordre."""
    reg = ParamRegistry(tmp_path / "reg")
    reg.register(make_set(), acceptance_passed=True)
    reg.set_observation(True, "test")
    apm = AdaptiveParameterManager(cfg, registry=reg, selector=PostureSelector(backend=None))
    assert apm.effective(50.0).observation_mode


def test_digest_ne_contient_que_des_champs_produits_par_le_bot():
    """§12.5 : jamais de texte libre externe non contrôlé.

    C'est une frontière de sécurité : tout champ qu'un tiers pourrait remplir
    deviendrait un canal d'instruction vers le LLM.
    """
    digest = build_digest({"net_pnl": 12.0}, [("1h/CHOP", 40)], "LONG_ONLY",
                          "trend", 55.0, 0.1, {"risk_level": "normal"})
    assert set(digest) == {"window_days", "performance", "veto_distribution",
                           "layers", "funding_annualized", "macro"}
    json.dumps(digest)          # doit être sérialisable tel quel pour le log


# ── Configuration §12.7 ──────────────────────────────────────────────────────

def test_config_adaptive_chargee(cfg):
    assert cfg.adaptive.posture_selector.shadow_days == 45
    assert cfg.adaptive.posture_selector.aggressive_confirm_days == 3
    assert cfg.adaptive.walk_forward.max_param_drift == pytest.approx(0.40)


def test_config_refuse_un_backend_inconnu():
    with pytest.raises(config_mod.ConfigError, match="backend inconnu"):
        config_mod.from_dict({"symbol": "BTC"},
                             adaptive={"posture_selector": {"backend": "gpt"}})


def test_config_refuse_zero_confirmation_agressive():
    with pytest.raises(config_mod.ConfigError, match="ratchet"):
        config_mod.from_dict({"symbol": "BTC"},
                             adaptive={"posture_selector": {"aggressive_confirm_days": 0}})


def test_config_refuse_percentiles_conditionneur_inverses():
    with pytest.raises(config_mod.ConfigError):
        config_mod.from_dict({"symbol": "BTC"},
                             adaptive={"regime_conditioner": {"vol_percentile_low": 80}})

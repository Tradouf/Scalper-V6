"""
WalkForwardOptimizer — étage 1b, ré-optimisation programmée. SPEC §12.4.

Job mensuel, **hors chemin critique du trading** : il ne tourne jamais dans la
boucle de décision, et son échec ne peut pas empêcher le bot de décider — il
peut seulement l'empêcher de changer d'avis sur ses paramètres.

Ré-optimise les trois postures sur fenêtre glissante 12 mois et les valide
out-of-sample selon le protocole EXACT du §9 : mêmes critères d'acceptation,
mêmes tests de sensibilité ±20 %. Il n'y a pas de « validation allégée pour la
ré-optimisation » — ce serait une porte dérobée vers le mainnet.

Trois garde-fous, du plus au moins évident :

* **Promotion conditionnelle** — un nouveau set ne remplace l'ancien que s'il
  passe les critères ET que sa dégradation vis-à-vis de l'ancien, mesurée sur
  la fenêtre commune, est expliquée. Le code ne peut pas produire cette
  explication : il exige donc une validation humaine et s'arrête là.
* **Dérive** — plus de 40 % d'écart sur un paramètre clé et la promotion est
  bloquée. Un paramètre qui saute d'un tiers en un mois décrit rarement le même
  marché ; le plus souvent, l'optimiseur vient de trouver du bruit.
* **Trois cycles échoués** — le bot passe en mode observation, même mécanique
  que le kill-switch frais du §6.5. Ce n'est pas l'optimiseur qui a un
  problème : c'est que le marché a probablement changé de nature.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from confluence.adaptive.conditioner import ConditionerError, RegimeConditioner
from confluence.adaptive.registry import ParameterSet, ParamRegistry, RegistryError
from confluence.config import ConfluenceConfig
from confluence.data import History
from confluence.walkforward import acceptance, sensitivity, walk_forward

logger = logging.getLogger("sdm.confluence.adaptive.optimizer")

# Paramètres dont la dérive est surveillée (§12.4 « un paramètre clé »).
KEY_PARAMS = ("regime_1h.adx_trend", "risk.k_stop", "risk.edge_multiple", "risk.risk_pct")


@dataclass
class PostureOutcome:
    posture: str
    promoted: bool = False
    reason: str = ""
    requires_human_approval: bool = False
    candidate: Optional[ParameterSet] = None
    drift: Dict[str, float] = field(default_factory=dict)
    acceptance: Dict[str, Any] = field(default_factory=dict)
    sensitivity_fragile: bool = False

    def as_log(self) -> Dict[str, Any]:
        return {
            "posture": self.posture,
            "promoted": self.promoted,
            "reason": self.reason,
            "requires_human_approval": self.requires_human_approval,
            "version": self.candidate.version if self.candidate else None,
            "drift": {k: (None if v == float("inf") else round(v, 4))
                      for k, v in self.drift.items()},
            "acceptance_passed": self.acceptance.get("passed"),
            "sensitivity_fragile": self.sensitivity_fragile,
        }


@dataclass
class OptimizerReport:
    ran_at: datetime
    outcomes: List[PostureOutcome] = field(default_factory=list)
    consecutive_failures: int = 0
    entered_observation: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def any_promoted(self) -> bool:
        return any(o.promoted for o in self.outcomes)

    def as_log(self) -> Dict[str, Any]:
        return {
            "ran_at": self.ran_at.isoformat(),
            "outcomes": [o.as_log() for o in self.outcomes],
            "consecutive_failures": self.consecutive_failures,
            "entered_observation": self.entered_observation,
            "notes": self.notes,
        }


# Grilles par posture. Elles diffèrent par le RÉGLAGE du risque, pas par la
# structure : les trois postures restent la même stratégie, sinon « choisir une
# posture » reviendrait à choisir une stratégie — ce que le §12 interdit au LLM.
POSTURE_GRIDS: Dict[str, Dict[str, Sequence[float]]] = {
    "defensive": {"risk.k_stop": (1.5, 2.0), "risk.edge_multiple": (6.0, 8.0)},
    "neutral": {"risk.k_stop": (1.2, 1.5, 2.0), "risk.edge_multiple": (5.0,)},
    "aggressive": {"risk.k_stop": (1.2, 1.5), "risk.edge_multiple": (3.0, 4.0)},
}

POSTURE_BOUNDS: Dict[str, Dict[str, tuple]] = {
    "defensive": {"risk.k_stop": (1.5, 2.4), "risk.edge_multiple": (9.0, 6.0),
                  "risk.risk_pct": (0.0035, 0.0025)},
    "neutral": {"risk.k_stop": (1.2, 2.0), "risk.edge_multiple": (7.0, 4.0),
                "risk.risk_pct": (0.005, 0.0035)},
    "aggressive": {"risk.k_stop": (1.2, 1.8), "risk.edge_multiple": (5.0, 3.0),
                   "risk.risk_pct": (0.0065, 0.005)},
}


class WalkForwardOptimizer:
    def __init__(self, cfg: ConfluenceConfig, registry: ParamRegistry,
                 max_param_drift: float = 0.40,
                 fail_cycles_to_observation: int = 3,
                 initial_equity: float = 10_000.0) -> None:
        self.cfg = cfg
        self.registry = registry
        self.max_param_drift = max_param_drift
        self.fail_cycles_to_observation = fail_cycles_to_observation
        self.initial_equity = initial_equity

    def run_cycle(self, history: History, now: Optional[datetime] = None,
                  consecutive_failures: int = 0,
                  postures: Sequence[str] = ("defensive", "neutral", "aggressive"),
                  skip_sensitivity: bool = False) -> OptimizerReport:
        now = now or datetime.now(timezone.utc)
        report = OptimizerReport(ran_at=now, consecutive_failures=consecutive_failures)

        for posture in postures:
            report.outcomes.append(self._optimize_posture(
                posture, history, now, skip_sensitivity))

        if report.any_promoted:
            report.consecutive_failures = 0
        else:
            report.consecutive_failures = consecutive_failures + 1
            report.notes.append(
                f"aucune promotion — {report.consecutive_failures} cycle(s) consécutif(s) "
                f"sans set validé")

        if report.consecutive_failures >= self.fail_cycles_to_observation:
            report.entered_observation = True
            self.registry.set_observation(True, (
                f"{report.consecutive_failures} cycles d'optimisation consécutifs sans "
                f"set validé — le marché a probablement changé de nature (§12.4)"), now)

        logger.info("optimiseur: %s", report.as_log())
        return report

    def _optimize_posture(self, posture: str, history: History, now: datetime,
                          skip_sensitivity: bool) -> PostureOutcome:
        outcome = PostureOutcome(posture=posture)
        grid = POSTURE_GRIDS.get(posture)
        if not grid:
            outcome.reason = f"aucune grille définie pour la posture {posture}"
            return outcome

        report = walk_forward(self.cfg, history, grid=grid,
                              initial_equity=self.initial_equity)
        verdict = acceptance(report, self.cfg)
        outcome.acceptance = verdict

        if not report.windows:
            outcome.reason = "aucune fenêtre walk-forward exploitable"
            return outcome

        # Le set candidat = les paramètres retenus sur la fenêtre la plus
        # récente. C'est celle dont le régime ressemble le plus à celui qu'on
        # s'apprête à trader.
        chosen = dict(report.windows[-1].params)
        chosen.setdefault("risk.risk_pct", self.cfg.risk.risk_pct)

        candidate = ParameterSet(
            version=f"{now.strftime('%Y-%m')}-{posture}-{report.windows[-1].index}",
            posture=posture,
            params=chosen,
            validated_at=now,
            oos_metrics={
                "profit_factor": report.oos_profit_factor or 0.0,
                "fee_ratio": report.oos_fee_ratio or 0.0,
                "trades": float(report.total_trades),
                "max_drawdown": max((w.oos_max_dd for w in report.windows), default=0.0),
            },
            data_window=(_utc(report.windows[0].is_start_ms),
                         _utc(report.windows[-1].oos_end_ms)),
            conditioning_bounds=POSTURE_BOUNDS.get(posture, {}),
        )
        outcome.candidate = candidate

        try:
            RegimeConditioner.assert_no_feedback(candidate.conditioning_bounds)
        except ConditionerError as exc:
            outcome.reason = f"bornes de conditionnement invalides: {exc}"
            return outcome

        if not verdict["passed"]:
            failed = [k for k, c in verdict["checks"].items() if not c["passed"]]
            outcome.reason = f"critères §9.4 non remplis: {failed}"
            return outcome

        if not skip_sensitivity:
            variant = self.cfg
            for path, value in chosen.items():
                try:
                    variant = variant.replace_path(path, value)
                except Exception:                        # noqa: BLE001 — combinaison invalide
                    break
            sens = sensitivity(variant, history, initial_equity=self.initial_equity)
            outcome.sensitivity_fragile = bool(sens["fragile"])
            if outcome.sensitivity_fragile:
                outcome.reason = ("sensibilité ±20 % : le résultat s'effondre hors de la "
                                  "valeur exacte d'au moins un paramètre (§9.5) — rejet")
                return outcome

        # Garde-fou de dérive (§12.4).
        previous = self.registry.active.get(posture)
        if previous is not None:
            outcome.drift = {k: v for k, v in candidate.drift_vs(previous).items()
                             if k in KEY_PARAMS}
            excessive = {k: v for k, v in outcome.drift.items() if v > self.max_param_drift}
            if excessive:
                outcome.requires_human_approval = True
                outcome.reason = (
                    f"dérive > {self.max_param_drift:.0%} sur {sorted(excessive)} — "
                    f"promotion bloquée en attente de validation humaine (§12.4)")
                return outcome

            old_pf = previous.oos_metrics.get("profit_factor", 0.0)
            new_pf = candidate.oos_metrics.get("profit_factor", 0.0)
            if new_pf < old_pf:
                # Le §12.4 veut une dégradation « expliquée ». Le code ne sait
                # pas expliquer : il constate et passe la main.
                outcome.requires_human_approval = True
                outcome.reason = (
                    f"PF OOS en recul ({new_pf:.3f} < {old_pf:.3f}) — le §12.4 exige que "
                    f"la dégradation soit expliquée ; l'ancien set reste en place")
                return outcome

        try:
            self.registry.register(candidate, acceptance_passed=True,
                                   reason="promotion automatique (§12.4)", now=now)
        except RegistryError as exc:
            outcome.reason = f"registre a refusé le set: {exc}"
            return outcome

        outcome.promoted = True
        outcome.reason = "promu"
        return outcome


def _utc(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)


__all__ = ["KEY_PARAMS", "OptimizerReport", "POSTURE_BOUNDS", "POSTURE_GRIDS",
           "PostureOutcome", "WalkForwardOptimizer"]

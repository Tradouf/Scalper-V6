"""
Protocole de validation du MomentumAgent — SPEC §9.

Quatre étages :

1. **Deux fenêtres obligatoires (§9.3)** — récente et 2021-2023. La seconde
   contient le momentum crash de la reprise post-bear, c'est-à-dire ce que le §0
   annonce comme le risque principal de la stratégie.
2. **Gate placebo (§9.2 amendé)** — critère PRINCIPAL, α = 0,0167 sur 60 tirages.
3. **Sensibilité ±20 %** avec vérification de **dégradation progressive** : un
   pic isolé sur un paramètre est une signature de surapprentissage, pas une
   qualité.
4. **Critères §9.4** sur `net_mtm_pnl` seul.

**Le placebo, en détail.** Une permutation σ est tirée UNE FOIS par tirage et
réaffecte la série de scores de l'actif i à l'actif σ(i) pour toute la période.
Univers, structure, coûts et *persistance du classement* sont préservés ; seul
le lien entre le passé d'un actif et son propre futur est rompu. La rédaction
initiale prévoyait une permutation par date — elle aurait détruit la persistance,
donc l'hystérésis, donc fait exploser les frais du placebo. On aurait comparé
une stratégie calme à une stratégie qui churne.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from momentum.backtest import MomentumBacktester
from momentum.config import MomentumConfig
from momentum.data import MultiAssetHistory

logger = logging.getLogger("sdm.momentum.validate")


@dataclass
class WindowRun:
    label: str
    metrics: Dict[str, Any] = field(default_factory=dict)
    branches: Dict[str, int] = field(default_factory=dict)
    never_taken: List[str] = field(default_factory=list)

    @property
    def net(self) -> float:
        return float(self.metrics.get("net_mtm_pnl", 0.0))


def run_window(cfg: MomentumConfig, hist: MultiAssetHistory, label: str,
               permutation: Optional[Mapping[str, str]] = None) -> WindowRun:
    res = MomentumBacktester(cfg).run(hist, score_permutation=permutation)
    return WindowRun(label=label, metrics=res.metrics(), branches=res.branches,
                     never_taken=res.never_taken_branches())


# ── §9.2 Placebo à permutation persistante ──────────────────────────────────

def draw_permutation(symbols: Sequence[str], rng: random.Random) -> Dict[str, str]:
    """Permutation SANS point fixe autant que possible.

    Un point fixe (un actif qui garde son propre score) réintroduit du vrai
    signal dans le placebo et rapproche mécaniquement le tirage du réel — ce qui
    rend le gate plus difficile à passer pour de mauvaises raisons. Sur un
    panier de 10 à 25 actifs, un dérangement complet est presque toujours
    atteignable en quelques essais.
    """
    pool = list(symbols)
    if len(pool) < 2:
        return {s: s for s in pool}
    for _ in range(50):
        shuffled = pool[:]
        rng.shuffle(shuffled)
        if all(a != b for a, b in zip(pool, shuffled)):
            return dict(zip(pool, shuffled))
    # Repli : rotation d'un cran, qui est un dérangement par construction.
    return {a: b for a, b in zip(pool, pool[1:] + pool[:1])}


def run_placebo(cfg: MomentumConfig, hists: Mapping[str, MultiAssetHistory],
                n_draws: Optional[int] = None, alpha: Optional[float] = None,
                seed: int = 0) -> Dict[str, Any]:
    """Gate placebo. Rend p, le réel, et la distribution nulle.

    La statistique comparée est le `net_mtm_pnl` **cumulé sur les deux
    fenêtres** : c'est la métrique de décision du §7, et l'exiger sur l'ensemble
    évite qu'un tirage chanceux sur une seule fenêtre emporte le verdict.

    p = fraction des tirages placebo dont le net est ≥ au réel, avec correction
    de continuité (+1/+1) — même convention que `placebo_gate.py`.
    """
    n_draws = n_draws or cfg.backtest.placebo.n_draws
    alpha = alpha if alpha is not None else cfg.backtest.placebo.alpha

    real = sum(run_window(cfg, h, label).net for label, h in hists.items())
    logger.info("placebo: réel net_mtm_pnl = %.2f sur %d fenêtres", real, len(hists))

    rng = random.Random(seed)
    null: List[float] = []
    for draw in range(n_draws):
        total = 0.0
        for label, hist in hists.items():
            perm = draw_permutation(sorted(hist.daily), rng)
            total += run_window(cfg, hist, label, permutation=perm).net
        null.append(total)
        if (draw + 1) % 10 == 0:
            logger.info("  %d/%d tirages — médiane nulle %.2f",
                        draw + 1, n_draws, sorted(null)[len(null) // 2])

    ge = sum(1 for n in null if n >= real)
    p = (ge + 1) / (len(null) + 1)
    ordered = sorted(null)
    return {
        "real_net": round(real, 2),
        "null_median": round(ordered[len(ordered) // 2], 2),
        "null_max": round(ordered[-1], 2),
        "null_min": round(ordered[0], 2),
        "n_draws": len(null),
        "p_value": p,
        "alpha": alpha,
        "passed": p < alpha,
        "note": ("permutation persistante des séries de scores (§9.2 amendé) — "
                 "univers, structure, coûts et persistance du classement préservés"),
    }


# ── §9.3 Sensibilité, avec dégradation progressive ──────────────────────────

def sensitivity(cfg: MomentumConfig, hists: Mapping[str, MultiAssetHistory],
                ) -> Dict[str, Any]:
    """±20 % sur les paramètres clés.

    Le §9.3 demande une **dégradation progressive**. On le vérifie plutôt que de
    l'affirmer : si la valeur nominale surpasse largement ses deux voisines, la
    performance est un pic isolé — signature de surapprentissage. Un vrai edge
    varie doucement quand on bouge son paramètre de 20 %.
    """
    base = sum(run_window(cfg, h, label).net for label, h in hists.items())
    out: Dict[str, Any] = {"base_net_mtm": round(base, 2), "variants": [],
                           "isolated_peaks": []}

    for path in cfg.backtest.sensitivity.params:
        current = cfg.get_path(path)
        neighbours = []
        for delta in cfg.backtest.sensitivity.deltas:
            value = current * (1.0 + delta)
            if isinstance(current, int) and not isinstance(current, bool):
                value = max(1, int(round(value)))
            try:
                variant = cfg.replace_path(path, value)
            except Exception as exc:                       # noqa: BLE001
                out["variants"].append({"param": path, "delta": delta,
                                        "error": str(exc)[:120]})
                continue
            net = sum(run_window(variant, h, label).net for label, h in hists.items())
            neighbours.append(net)
            out["variants"].append({"param": path, "delta": delta, "value": value,
                                    "net_mtm_pnl": round(net, 2)})

        # Pic isolé : le nominal domine LARGEMENT ses deux voisins.
        if len(neighbours) == 2 and base > 0:
            worst = max(neighbours)
            if worst <= 0 or base > 2.0 * worst:
                out["isolated_peaks"].append({
                    "param": path, "base": round(base, 2),
                    "best_neighbour": round(worst, 2),
                    "verdict": "pic isolé — signature de surapprentissage (§9.3)"})

    out["fragile"] = bool(out["isolated_peaks"])
    return out


# ── §9.4 Critères d'acceptation ─────────────────────────────────────────────

def acceptance(cfg: MomentumConfig, runs: Sequence[WindowRun],
               placebo: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Les critères du §9.4, tous sur `net_mtm_pnl`.

    Le placebo est le **critère principal** : il figure en tête et un échec de
    sa part suffit à rejeter, quels que soient les autres.
    """
    acc = cfg.backtest.acceptance
    rebalances = sum(int(r.metrics.get("rebalances", 0)) for r in runs)
    worst_dd = max((float(r.metrics.get("max_drawdown_pct", 0.0)) for r in runs),
                   default=0.0)

    fees = sum(float(r.metrics.get("fees", 0.0)) for r in runs)
    gross = sum(float(r.metrics.get("gross_pnl_abs", 0.0)) for r in runs)
    fee_ratio = (fees / gross) if gross > 0 else None

    wins = sum(max(0.0, r.net) for r in runs)
    losses = sum(-min(0.0, r.net) for r in runs)
    pf = (wins / losses) if losses > 0 else None

    checks = {
        "placebo": {
            "value": None if not placebo else round(placebo["p_value"], 4),
            "threshold": acc_alpha(cfg), "principal": True,
            "passed": bool(placebo and placebo["passed"])},
        "profit_factor": {
            "value": _round(pf, 3), "threshold": acc.min_profit_factor,
            "passed": pf is not None and pf > acc.min_profit_factor},
        "max_drawdown": {
            "value": round(worst_dd, 4), "threshold": acc.max_drawdown_pct,
            "passed": worst_dd <= acc.max_drawdown_pct},
        "fee_ratio": {
            "value": _round(fee_ratio, 4), "threshold": acc.max_fee_ratio,
            "passed": fee_ratio is not None and fee_ratio < acc.max_fee_ratio},
        "min_rebalances": {
            "value": rebalances, "threshold": acc.min_rebalances,
            "passed": rebalances >= acc.min_rebalances},
    }
    return {
        "checks": checks,
        "passed": all(c["passed"] for c in checks.values()),
        # §7 : diagnostic, jamais critère.
        "edge_location": {r.label: r.metrics.get("edge_location") for r in runs},
        "funding_by_leg": {r.label: {"long": r.metrics.get("funding_long"),
                                     "short": r.metrics.get("funding_short")}
                           for r in runs},
    }


def acc_alpha(cfg: MomentumConfig) -> float:
    return cfg.backtest.placebo.alpha


def branch_alerts(runs: Sequence[WindowRun]) -> List[str]:
    """§9.3 : une branche jamais empruntée doit CRIER, pas se taire.

    Leçon directe de l'A/B fantôme du GridAgent, où le handoff n'avait jamais
    été emprunté et où le rapport concluait « B ≥ A » au centime près.
    """
    alerts = []
    for run in runs:
        for branch in run.never_taken:
            alerts.append(f"[{run.label}] branche jamais empruntée : {branch}")
    return alerts


def _round(value: Optional[float], digits: int) -> Optional[float]:
    return None if value is None else round(value, digits)


__all__ = ["WindowRun", "acceptance", "branch_alerts", "draw_permutation",
           "run_placebo", "run_window", "sensitivity"]

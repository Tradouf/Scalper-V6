"""
Protocole de validation du GridAgent — SPEC §9.

Cinq étages, dans l'ordre où ils doivent tuer le candidat :

1. **Deux fenêtres obligatoires (§9.3)** — 1100 j récents ET 2020-2023.
   « Une grille validée uniquement en marché calme est invalide par
   définition — c'est sur 2021-2022 qu'on mesure ce que coûte le §6.1. »
2. **Critères d'acceptation §9.4**, tous sur `net_mtm_pnl` : PF > 1,2, perte max
   d'une session ≤ 1,1 × `max_grid_loss_pct`, frais < 20 %, ≥ 30 sessions, et
   distribution des motifs d'arrêt rapportée.
3. **A/B du breakout handoff (§9.5)** — B n'est adoptée que si elle est ≥ A sur
   `net_mtm_pnl` out-of-sample **sur chacune des deux fenêtres**. Sinon
   `breakout_handoff: false` et on n'en parle plus.
4. **Sensibilité ±20 %** sur les paramètres clés.
5. **Gate placebo** au seuil Bonferroni du registre : α = 0,025, 40 tirages
   (entrée n°2, n = 2 candidats).

La métrique de décision est `net_mtm_pnl` et rien d'autre (§7). Le PnL réalisé
d'une grille monte quoi qu'il arrive ; l'utiliser reviendrait à noter la
stratégie sur le compteur qui ne peut pas descendre.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from confluence import indicators as ind
from confluence.config import ConfluenceConfig
from grid.backtest import GridBacktester
from grid.config import GridConfig

logger = logging.getLogger("sdm.grid.validate")


@dataclass
class WindowData:
    """Une fenêtre de test, avec ses séries déjà chargées."""

    label: str
    candles_1m: Sequence[dict]
    candles_15m: Sequence[dict]
    candles_1h: Sequence[dict]
    funding: Sequence[tuple] = ()
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None

    @property
    def days(self) -> float:
        if not self.candles_1m:
            return 0.0
        return (int(self.candles_1m[-1]["ts"]) - int(self.candles_1m[0]["ts"])) / 86_400_000.0


@dataclass
class VariantResult:
    label: str
    handoff: bool
    metrics: Dict[str, Any] = field(default_factory=dict)
    vetoes: List[tuple] = field(default_factory=list)

    @property
    def net(self) -> float:
        return float(self.metrics.get("net_mtm_pnl", 0.0))


def run_variant(cfg: GridConfig, conf_cfg: ConfluenceConfig, window: WindowData,
                handoff: bool, equity: float = 10_000.0) -> VariantResult:
    bt = GridBacktester(cfg, conf_cfg, equity)
    res = bt.run(window.candles_1m, window.candles_15m, window.candles_1h,
                 funding=window.funding, start_ms=window.start_ms,
                 end_ms=window.end_ms, breakout_handoff=handoff)
    return VariantResult(label=window.label, handoff=handoff,
                         metrics=res.metrics(), vetoes=res.veto_distribution(15))


# ── §9.4 Critères d'acceptation ─────────────────────────────────────────────

def acceptance(cfg: GridConfig, results: Sequence[VariantResult]) -> Dict[str, Any]:
    """Critères §9.4, agrégés sur TOUTES les fenêtres.

    Un critère non évaluable vaut ÉCHEC : le §9 est bloquant avant le mainnet,
    et un critère qu'on ne peut pas vérifier n'est pas un critère rempli.
    """
    acc = cfg.acceptance
    sessions = sum(int(r.metrics.get("sessions", 0)) for r in results)

    fees = sum(float(r.metrics.get("fees", 0.0)) for r in results)
    gross = sum(float(r.metrics.get("gross_pnl_abs", 0.0)) for r in results)
    worst_loss_pct = max((float(r.metrics.get("worst_session_loss_pct", 0.0))
                          for r in results), default=0.0)
    all_pf = _aggregate_pf(results)
    fee_ratio = (fees / gross) if gross > 0 else None
    loss_cap = cfg.build.max_grid_loss_pct * acc.max_session_loss_multiple

    checks = {
        "profit_factor_oos": {
            "value": _round(all_pf, 3), "threshold": acc.min_profit_factor_oos,
            "passed": all_pf is not None and all_pf > acc.min_profit_factor_oos},
        "worst_session_loss": {
            "value": round(worst_loss_pct, 5), "threshold": round(loss_cap, 5),
            "passed": worst_loss_pct <= loss_cap},
        "fee_ratio": {
            "value": _round(fee_ratio, 4), "threshold": acc.max_fee_ratio,
            "passed": fee_ratio is not None and fee_ratio < acc.max_fee_ratio},
        "min_sessions": {
            "value": sessions, "threshold": acc.min_sessions,
            "passed": sessions >= acc.min_sessions},
    }
    return {
        "checks": checks,
        "passed": all(c["passed"] for c in checks.values()),
        "stop_reasons": _merge_stop_reasons(results),   # §9.4 : distribution rapportée
    }


def _aggregate_pf(results: Sequence[VariantResult]) -> Optional[float]:
    """PF agrégé au niveau SESSION sur toutes les fenêtres.

    On repart des composantes plutôt que de moyenner les PF de fenêtre : une
    fenêtre à 3 sessions ne doit pas peser autant qu'une fenêtre à 40.
    """
    wins = sum(max(0.0, r.net) for r in results)
    losses = sum(-min(0.0, r.net) for r in results)
    return (wins / losses) if losses > 0 else None


def _merge_stop_reasons(results: Sequence[VariantResult]) -> Dict[str, int]:
    merged: Dict[str, int] = {}
    for r in results:
        for key, count in (r.metrics.get("stop_reasons") or {}).items():
            merged[key] = merged.get(key, 0) + int(count)
    return merged


# ── §9.5 A/B du breakout handoff ────────────────────────────────────────────

def ab_handoff(cfg: GridConfig, conf_cfg: ConfluenceConfig,
               windows: Sequence[WindowData], equity: float = 10_000.0) -> Dict[str, Any]:
    """A = flatten intégral, B = handoff. B adoptée seulement si ≥ A PARTOUT.

    L'exigence « sur chacune des deux fenêtres » est le point important : un
    handoff qui gagne gros sur la fenêtre récente et perd sur 2020-2023 est
    précisément un handoff qui chevauche les vrais breakouts d'un marché
    haussier et se fait piéger par les faux d'un marché baissier. Le §9.5 refuse
    cette moyenne.
    """
    per_window = []
    b_wins_everywhere = True

    for window in windows:
        a = run_variant(cfg, conf_cfg, window, handoff=False, equity=equity)
        b = run_variant(cfg, conf_cfg, window, handoff=True, equity=equity)
        better = b.net >= a.net
        b_wins_everywhere = b_wins_everywhere and better
        per_window.append({
            "window": window.label,
            "A_flatten_net_mtm": round(a.net, 2),
            "B_handoff_net_mtm": round(b.net, 2),
            "delta": round(b.net - a.net, 2),
            "B_better_or_equal": better,
            "A_sessions": a.metrics.get("sessions"),
            "B_sessions": b.metrics.get("sessions"),
            "B_handoffs": b.metrics.get("handoffs"),
        })
        logger.info("A/B %s: A=%.2f B=%.2f (%s)", window.label, a.net, b.net,
                    "B ≥ A" if better else "A > B")

    return {
        "per_window": per_window,
        "adopt_handoff": b_wins_everywhere,
        "decision": ("breakout_handoff: true — B ≥ A sur CHACUNE des fenêtres"
                     if b_wins_everywhere else
                     "breakout_handoff: false — B n'est pas ≥ A partout (§9.5), "
                     "et on n'en parle plus"),
    }


# ── §9 Sensibilité ──────────────────────────────────────────────────────────

def sensitivity(cfg: GridConfig, conf_cfg: ConfluenceConfig,
                windows: Sequence[WindowData], handoff: bool,
                equity: float = 10_000.0) -> Dict[str, Any]:
    """±20 % sur les paramètres clés, mesuré sur `net_mtm_pnl` agrégé."""
    base_net = sum(run_variant(cfg, conf_cfg, w, handoff, equity).net for w in windows)
    out: Dict[str, Any] = {"base_net_mtm": round(base_net, 2), "variants": [],
                           "fragile": False}

    for path in cfg.backtest.sensitivity.params:
        current = cfg.get_path(path)
        for delta in cfg.backtest.sensitivity.deltas:
            value = current * (1.0 + delta)
            if isinstance(current, int) and not isinstance(current, bool):
                value = int(round(value))
            try:
                variant_cfg = cfg.replace_path(path, value)
            except Exception as exc:                    # noqa: BLE001
                out["variants"].append({"param": path, "delta": delta,
                                        "error": str(exc)[:120]})
                continue
            net = sum(run_variant(variant_cfg, conf_cfg, w, handoff, equity).net
                      for w in windows)
            collapsed = base_net > 0 and net <= 0
            out["fragile"] = out["fragile"] or collapsed
            out["variants"].append({
                "param": path, "delta": delta, "value": value,
                "net_mtm_pnl": round(net, 2), "collapsed": collapsed})
    return out


# ── Gate placebo ────────────────────────────────────────────────────────────

_GATE: Dict[str, Any] = {}


def placebo_selector(candles_by_symbol: Dict[str, List[dict]]) -> int:
    """« Ce qui est sélectionné » = le nombre de SESSIONS de grille rentables.

    À configuration figée, le seul ingrédient testé est l'existence d'un range
    exploitable. Si des séries dont l'autocorrélation a été détruite produisent
    autant de sessions gagnantes que le réel, alors les sessions gagnantes du
    réel sont du bruit — et la prime de liquidité que l'hypothèse postule n'est
    pas mesurable ici.
    """
    cfg: GridConfig = _GATE["cfg"]
    conf_cfg: ConfluenceConfig = _GATE["conf_cfg"]
    symbol = _GATE["symbol"]
    equity = _GATE["equity"]
    handoff = _GATE["handoff"]

    c1 = candles_by_symbol[symbol]
    # Les TF supérieurs sont RECONSTRUITS depuis la série permutée : les
    # réutiliser laisserait l'autocorrélation intacte au-dessus de la minute.
    c15 = ind.aggregate(c1, "1m", "15m")
    c1h = ind.aggregate(c1, "1m", "1h")
    if not c15 or not c1h:
        return 0

    bt = GridBacktester(cfg, conf_cfg, equity)
    res = bt.run(c1, c15, c1h, funding=_GATE.get("funding", ()),
                 breakout_handoff=handoff)
    return sum(1 for s in res.sessions if s.pnl.net > 0)


def run_placebo(cfg: GridConfig, conf_cfg: ConfluenceConfig, window: WindowData,
                handoff: bool, n_draws: Optional[int] = None,
                alpha: Optional[float] = None, jobs: int = 1,
                equity: float = 10_000.0):
    """Gate placebo au seuil du registre (α = 0,025, 40 tirages pour n = 2).

    Rappel : le pipeline doit être FIGÉ avant le tirage. Re-tester après avoir
    modifié un seuil est du multiple-testing, et le p obtenu ne veut plus rien
    dire.
    """
    from placebo_gate import run_gate

    _GATE.update({"cfg": cfg, "conf_cfg": conf_cfg, "symbol": "BTC",
                  "equity": equity, "handoff": handoff,
                  "funding": window.funding})
    return run_gate({"BTC": list(window.candles_1m)}, placebo_selector,
                    n_placebo=n_draws or cfg.backtest.placebo.n_draws,
                    alpha=alpha if alpha is not None else cfg.backtest.placebo.alpha,
                    jobs=jobs)


def _round(value: Optional[float], digits: int) -> Optional[float]:
    return None if value is None else round(value, digits)


__all__ = ["VariantResult", "WindowData", "ab_handoff", "acceptance",
           "placebo_selector", "run_placebo", "run_variant", "sensitivity"]

"""
Protocole de validation — SPEC §9.2 à §9.6.

Quatre étages, dans l'ordre où ils doivent tuer un candidat :

1. **Walk-forward** — optimisation sur 12 mois glissants, test out-of-sample
   sur les 3 mois suivants, pas de 3 mois. Seul l'OOS compte ; l'in-sample ne
   sert qu'à choisir les paramètres.
2. **Critères d'acceptation §9.4** — PF net > 1,3 OOS, frais < 15 % du PnL
   brut, ≥ 100 trades et ≤ 3/jour, aucune fenêtre OOS dont le drawdown dépasse
   2× le max drawdown in-sample.
3. **Sensibilité §9.5** — ±20 % sur chaque paramètre clé. Un résultat qui ne
   survit qu'à sa valeur exacte est du surapprentissage, pas un edge.
4. **Gate placebo** — `placebo_gate.py`, à la racine du repo, imposé depuis le
   verdict SimpleBot du 2026-08-07 : un pipeline de sélection n'a de valeur que
   s'il sélectionne PLUS sur les vraies données que sur des séries à edge nul
   par construction. C'est ce test, et pas le backtest, qui a condamné
   l'optimiseur EMA-cross (p = 0,90).

Le §9.5 mérite d'être pris au mot : la sensibilité n'est pas un rapport
d'accompagnement, c'est un critère de rejet.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from confluence import indicators as ind
from confluence.backtest import BacktestResult, Backtester
from confluence.config import ConfluenceConfig
from confluence.data import History

logger = logging.getLogger("sdm.confluence.walkforward")

# Grille d'optimisation in-sample par défaut. Volontairement PETITE : chaque
# point de grille est une chance supplémentaire de trouver du bruit qui a l'air
# d'un edge. Trois valeurs sur deux paramètres, c'est 9 essais par fenêtre —
# assez pour montrer qu'un optimum existe, trop peu pour le fabriquer.
DEFAULT_GRID: Dict[str, Sequence[Any]] = {
    "regime_1h.adx_trend": (22.0, 25.0, 28.0),
    "risk.k_stop": (1.2, 1.5, 2.0),
}


@dataclass
class WindowResult:
    index: int
    is_start_ms: int
    is_end_ms: int
    oos_start_ms: int
    oos_end_ms: int
    params: Dict[str, Any]
    is_metrics: Dict[str, Any]
    oos_metrics: Dict[str, Any]
    oos_trades: int = 0
    oos_net_pnl: float = 0.0
    oos_gross_abs: float = 0.0
    oos_fees: float = 0.0
    oos_wins: float = 0.0
    oos_losses: float = 0.0
    oos_max_dd: float = 0.0
    is_max_dd: float = 0.0

    def as_log(self) -> Dict[str, Any]:
        return {
            "window": self.index,
            "is": [_iso(self.is_start_ms), _iso(self.is_end_ms)],
            "oos": [_iso(self.oos_start_ms), _iso(self.oos_end_ms)],
            "params": self.params,
            "is_metrics": self.is_metrics,
            "oos_metrics": self.oos_metrics,
        }


@dataclass
class WalkForwardReport:
    windows: List[WindowResult] = field(default_factory=list)
    days: float = 0.0
    notes: List[str] = field(default_factory=list)

    @property
    def total_trades(self) -> int:
        return sum(w.oos_trades for w in self.windows)

    @property
    def oos_profit_factor(self) -> Optional[float]:
        """PF agrégé sur TOUT l'OOS.

        Agrégé sur les gains et pertes cumulés, pas comme moyenne des PF par
        fenêtre : une fenêtre à 2 trades et PF=8 ne doit pas peser autant
        qu'une fenêtre à 40 trades.
        """
        wins = sum(w.oos_wins for w in self.windows)
        losses = sum(w.oos_losses for w in self.windows)
        return (wins / losses) if losses > 0 else None

    @property
    def oos_fee_ratio(self) -> Optional[float]:
        gross = sum(w.oos_gross_abs for w in self.windows)
        return (sum(w.oos_fees for w in self.windows) / gross) if gross > 0 else None

    @property
    def oos_net_pnl(self) -> float:
        return sum(w.oos_net_pnl for w in self.windows)

    @property
    def oos_days(self) -> float:
        return sum((w.oos_end_ms - w.oos_start_ms) / 86_400_000.0 for w in self.windows)

    @property
    def trades_per_day(self) -> float:
        d = self.oos_days
        return self.total_trades / d if d > 0 else 0.0

    @property
    def worst_dd_ratio(self) -> Optional[float]:
        """Pire rapport (drawdown OOS / drawdown in-sample) sur les fenêtres.

        Le critère §9.4 vise la fenêtre la PIRE, pas la moyenne : une seule
        fenêtre OOS à 2× le drawdown in-sample signale que l'optimisation a
        appris un régime qui ne s'est pas reproduit.
        """
        ratios = [w.oos_max_dd / w.is_max_dd for w in self.windows if w.is_max_dd > 0]
        return max(ratios) if ratios else None

    def summary(self) -> Dict[str, Any]:
        return {
            "windows": len(self.windows),
            "oos_days": round(self.oos_days, 1),
            "oos_trades": self.total_trades,
            "oos_trades_per_day": round(self.trades_per_day, 3),
            "oos_net_pnl": round(self.oos_net_pnl, 2),
            "oos_profit_factor": _round(self.oos_profit_factor, 3),
            "oos_fee_ratio": _round(self.oos_fee_ratio, 4),
            "worst_oos_dd_vs_is": _round(self.worst_dd_ratio, 3),
        }


# ── Walk-forward ─────────────────────────────────────────────────────────────

def add_months(ts_ms: int, months: int) -> int:
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    day = min(dt.day, _days_in_month(year, month))
    return int(dt.replace(year=year, month=month, day=day).timestamp() * 1000)


def _days_in_month(year: int, month: int) -> int:
    import calendar

    return calendar.monthrange(year, month)[1]


def windows_for(history: History, cfg: ConfluenceConfig,
                start_ms: Optional[int] = None) -> List[Tuple[int, int, int, int]]:
    """Fenêtres (is_start, is_end, oos_start, oos_end) du §9.2.

    On démarre après le warmup le plus long : optimiser sur une fenêtre dont la
    première moitié ne peut produire aucune décision reviendrait à optimiser
    sur 6 mois en croyant en avoir 12.

    `start_ms` borne en plus le début de la FENÊTRE DE DÉCISION. Sans lui,
    demander « 1100 jours » chargeait 1100 jours plus le warmup, puis décidait
    sur tout ce qui suivait le warmup — soit 1301 jours. L'écart n'est pas
    anodin : il ramenait la période testée dans une zone où le funding natif
    n'existe pas encore.
    """
    bars = history.candles.get("15m", [])
    if not bars:
        return []
    wf = cfg.backtest.walkforward
    warm = Backtester(cfg)._warmup_end_ms(history)      # noqa: SLF001 — même paquet
    start = max(warm, int(bars[0]["ts"]), start_ms or 0)
    end = int(bars[-1]["ts"]) + ind.INTERVAL_MS["15m"]

    out = []
    is_start = start
    while True:
        is_end = add_months(is_start, wf.is_months)
        oos_end = add_months(is_end, wf.oos_months)
        if oos_end > end:
            break
        out.append((is_start, is_end, is_end, oos_end))
        is_start = add_months(is_start, wf.step_months)
    return out


def grid_points(grid: Dict[str, Sequence[Any]]) -> List[Dict[str, Any]]:
    keys = sorted(grid)
    return [dict(zip(keys, combo)) for combo in itertools.product(*(grid[k] for k in keys))]


def apply_params(cfg: ConfluenceConfig, params: Dict[str, Any]) -> Optional[ConfluenceConfig]:
    """Applique un point de grille. Rend None si la combinaison est invalide
    (ex. `adx_trend` descendu sous `adx_range`) — une combinaison rejetée par
    la validation est écartée, jamais silencieusement corrigée."""
    from confluence.config import ConfigError

    out = cfg
    try:
        for path, value in params.items():
            out = out.replace_path(path, value)
    except ConfigError as exc:
        logger.info("point de grille écarté %s: %s", params, exc)
        return None
    return out


def _score(result: BacktestResult, min_trades: int) -> float:
    """Score in-sample. Un PF sur 3 trades ne veut rien dire : en dessous du
    minimum, le point est disqualifié plutôt que classé."""
    if len(result.trades) < min_trades:
        return float("-inf")
    pf = result.profit_factor
    if pf is None:
        return float("-inf")
    return pf


def walk_forward(cfg: ConfluenceConfig, history: History,
                 grid: Optional[Dict[str, Sequence[Any]]] = None,
                 initial_equity: float = 10_000.0,
                 min_is_trades: int = 10,
                 start_ms: Optional[int] = None) -> WalkForwardReport:
    grid = DEFAULT_GRID if grid is None else grid
    points = grid_points(grid)
    report = WalkForwardReport()
    wins = windows_for(history, cfg, start_ms=start_ms)
    if not wins:
        report.notes.append("aucune fenêtre walk-forward: historique trop court")
        return report

    total_days = sum(len(history.candles.get(tf, [])) for tf in ("15m",)) * 900_000 / 86_400_000
    report.days = total_days
    logger.info("walk-forward: %d fenêtres × %d points de grille = %d backtests in-sample",
                len(wins), len(points), len(wins) * len(points))

    for idx, (is_start, is_end, oos_start, oos_end) in enumerate(wins):
        best_params, best_result, best_score = None, None, float("-inf")
        # Repli explicite sur le premier point valide. Sans lui, une fenêtre où
        # AUCUN point n'atteint `min_is_trades` voit tous ses scores à -inf,
        # `s > best_score` reste faux dès le premier tour, et la fenêtre est
        # écartée en silence. Or ces fenêtres-là sont précisément celles où
        # l'in-sample n'a rien donné : les retirer de l'agrégat OOS reviendrait
        # à ne noter la stratégie que sur les périodes qui lui réussissaient
        # déjà — un biais de survivance à l'intérieur du protocole censé le
        # détecter.
        fallback_params, fallback_result = None, None
        for params in points:
            variant = apply_params(cfg, params)
            if variant is None:
                continue
            res = Backtester(variant, initial_equity).run(history, is_start, is_end)
            if fallback_params is None:
                fallback_params, fallback_result = params, res
            s = _score(res, min_is_trades)
            if s > best_score:
                best_params, best_result, best_score = params, res, s

        if best_params is None:
            best_params, best_result = fallback_params, fallback_result
            if best_params is None:
                report.notes.append(f"fenêtre {idx}: aucun point de grille exploitable")
                continue
            report.notes.append(
                f"fenêtre {idx}: aucun point n'atteint {min_is_trades} trades in-sample "
                f"— repli sur {best_params}, la fenêtre est tout de même testée OOS")

        variant = apply_params(cfg, best_params)
        oos = Backtester(variant, initial_equity).run(history, oos_start, oos_end)

        report.windows.append(WindowResult(
            index=idx,
            is_start_ms=is_start, is_end_ms=is_end,
            oos_start_ms=oos_start, oos_end_ms=oos_end,
            params=best_params,
            is_metrics=best_result.metrics(),
            oos_metrics=oos.metrics(),
            oos_trades=len(oos.trades),
            oos_net_pnl=oos.net_pnl,
            oos_gross_abs=oos.gross_pnl_abs,
            oos_fees=oos.fees_paid,
            oos_wins=sum(t.net_pnl for t in oos.trades if t.net_pnl > 0),
            oos_losses=-sum(t.net_pnl for t in oos.trades if t.net_pnl < 0),
            oos_max_dd=oos.max_drawdown,
            is_max_dd=best_result.max_drawdown,
        ))
        logger.info("fenêtre %d: params=%s OOS PF=%s trades=%d",
                    idx, best_params, _round(oos.profit_factor, 2), len(oos.trades))
    return report


# ── §9.4 Critères d'acceptation ──────────────────────────────────────────────

def acceptance(report: WalkForwardReport, cfg: ConfluenceConfig) -> Dict[str, Any]:
    """Les cinq critères du §9.4, évalués séparément et sans indulgence.

    Un critère non évaluable (PF indéfini faute de perte, ratio de drawdown
    sans in-sample) vaut ÉCHEC, pas « on verra » : le §9 est bloquant avant le
    mainnet, et un critère qu'on ne peut pas vérifier n'est pas un critère
    rempli.
    """
    acc = cfg.backtest.acceptance
    pf = report.oos_profit_factor
    fee = report.oos_fee_ratio
    dd = report.worst_dd_ratio

    checks = {
        "profit_factor_oos": {
            "value": _round(pf, 3), "threshold": acc.min_profit_factor_oos,
            "passed": pf is not None and pf > acc.min_profit_factor_oos,
        },
        "fee_ratio": {
            "value": _round(fee, 4), "threshold": acc.max_fee_ratio,
            "passed": fee is not None and fee < acc.max_fee_ratio,
        },
        "min_trades": {
            "value": report.total_trades, "threshold": acc.min_trades_total,
            "passed": report.total_trades >= acc.min_trades_total,
        },
        "trades_per_day": {
            "value": _round(report.trades_per_day, 3), "threshold": acc.max_trades_per_day_avg,
            "passed": report.trades_per_day <= acc.max_trades_per_day_avg,
        },
        "oos_drawdown_vs_is": {
            "value": _round(dd, 3), "threshold": acc.max_oos_dd_vs_is_multiple,
            "passed": dd is not None and dd <= acc.max_oos_dd_vs_is_multiple,
        },
    }
    return {
        "checks": checks,
        "passed": all(c["passed"] for c in checks.values()),
        "summary": report.summary(),
    }


# ── §9.5 Sensibilité ─────────────────────────────────────────────────────────

def sensitivity(cfg: ConfluenceConfig, history: History,
                params: Optional[Sequence[str]] = None,
                deltas: Optional[Sequence[float]] = None,
                initial_equity: float = 10_000.0) -> Dict[str, Any]:
    """Variation relative de chaque paramètre clé, et effet sur le PF net.

    Le §9.5 demande de « rapporter la sensibilité » ; on va un cran plus loin
    en marquant `fragile` toute variation qui fait passer le PF sous 1 — c'est
    le seuil au-delà duquel « le résultat s'effondre hors de sa valeur exacte »
    et où le §9.5 impose le rejet.
    """
    params = params or cfg.backtest.sensitivity.params
    deltas = deltas or cfg.backtest.sensitivity.deltas

    base = Backtester(cfg, initial_equity).run(history)
    base_pf = base.profit_factor
    out: Dict[str, Any] = {"base": base.metrics(), "variants": [], "fragile": False}

    for path in params:
        current = cfg.get_path(path)
        for delta in deltas:
            value = current * (1.0 + delta)
            if isinstance(current, int) and not isinstance(current, bool):
                value = int(round(value))
            variant = apply_params(cfg, {path: value})
            if variant is None:
                out["variants"].append({"param": path, "delta": delta, "value": value,
                                        "error": "combinaison invalide"})
                continue
            res = Backtester(variant, initial_equity).run(history)
            pf = res.profit_factor
            collapsed = (pf is None or pf < 1.0) and (base_pf is not None and base_pf >= 1.0)
            out["fragile"] = out["fragile"] or collapsed
            out["variants"].append({
                "param": path, "delta": delta, "value": value,
                "profit_factor": _round(pf, 3), "trades": len(res.trades),
                "net_pnl": round(res.net_pnl, 2), "collapsed": collapsed,
            })
    return out


# ── Gate placebo ─────────────────────────────────────────────────────────────

_GATE_STATE: Dict[str, Any] = {}


def placebo_selector(candles_by_symbol: Dict[str, List[dict]]) -> int:
    """Sélecteur pour `placebo_gate.run_gate`.

    « Ce qui est sélectionné » = le nombre de fenêtres OOS rentables (PF > 1)
    en rejouant la stratégie à paramètres FIXES. À paramètres fixes, le seul
    ingrédient testé est l'edge lui-même : si des séries dont l'autocorrélation
    a été détruite produisent autant de fenêtres rentables, alors les fenêtres
    rentables du réel sont du bruit.

    Fonction de module (donc picklable) et lisant sa config dans `_GATE_STATE` :
    `run_gate` distribue le travail par `multiprocessing.Pool`, qui hérite ces
    globales par fork.
    """
    cfg: ConfluenceConfig = _GATE_STATE["cfg"]
    symbol = _GATE_STATE["symbol"]
    funding = _GATE_STATE["funding"]
    equity = _GATE_STATE["equity"]

    c15 = candles_by_symbol[symbol]
    hist = History(symbol=symbol)
    hist.candles["15m"] = c15
    # Les TF supérieurs sont RECONSTRUITS depuis la série permutée : les
    # réutiliser tels quels laisserait l'autocorrélation intacte au-dessus du
    # 15m, et le placebo ne testerait plus rien.
    hist.candles["1h"] = ind.aggregate(c15, "15m", "1h")
    hist.candles["1d"] = ind.aggregate(c15, "15m", "1d")
    hist.candles["1m"] = []
    hist.funding = funding

    profitable = 0
    for _, _, oos_start, oos_end in windows_for(hist, cfg):
        res = Backtester(cfg, equity).run(hist, oos_start, oos_end)
        pf = res.profit_factor
        if pf is not None and pf > 1.0:
            profitable += 1
    return profitable


def run_placebo(cfg: ConfluenceConfig, history: History, n_draws: int = 30,
                alpha: float = 0.05, jobs: int = 1, seed: int = 0,
                initial_equity: float = 10_000.0):
    """Lance le gate placebo de la racine du repo sur la série 15m.

    Rappel de `placebo_gate` : le pipeline doit être FIGÉ avant le tirage.
    Re-tester après avoir modifié un seuil, c'est du multiple-testing, et le
    p obtenu ne veut plus rien dire.

    Réel et placebo passent tous deux par `placebo_selector`, donc tous deux
    reconstruisent 1d et 1h depuis la série 15m : la comparaison est
    symétrique. En contrepartie, le 15m doit être assez long pour absorber le
    warmup du biais 1d (202 jours) PUIS contenir des fenêtres — sinon les deux
    côtés rendent zéro et le gate échoue faute de matière, ce qui est signalé
    ci-dessous plutôt que confondu avec un vrai échec.
    """
    from placebo_gate import run_gate

    span_days = 0.0
    c15 = history.candles.get("15m", [])
    if c15:
        span_days = (int(c15[-1]["ts"]) - int(c15[0]["ts"])) / 86_400_000.0
    wf = cfg.backtest.walkforward
    needed = cfg.bias_1d.warmup_bars + 30 * (wf.is_months + wf.oos_months)
    if span_days < needed:
        logger.warning(
            "gate placebo: la série 15m couvre %.0f j, il en faut ~%.0f "
            "(warmup du biais 1d + une fenêtre) — le gate va manquer de matière",
            span_days, needed)

    _GATE_STATE.update({
        "cfg": cfg, "symbol": history.symbol,
        "funding": history.funding, "equity": initial_equity,
    })
    return run_gate({history.symbol: history.candles["15m"]}, placebo_selector,
                    n_placebo=n_draws, alpha=alpha, seed=seed, jobs=jobs)


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).isoformat()


def _round(value: Optional[float], digits: int) -> Optional[float]:
    return None if value is None else round(value, digits)


__all__ = [
    "DEFAULT_GRID", "WalkForwardReport", "WindowResult", "acceptance",
    "add_months", "grid_points", "placebo_selector", "run_placebo",
    "sensitivity", "walk_forward", "windows_for",
]

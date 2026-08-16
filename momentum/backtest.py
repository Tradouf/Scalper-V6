"""
Moteur de backtest du MomentumAgent — SPEC §9.

Cadence : **une décision tous les `every_d` jours à heure fixe**, exactement
comme le live (§4). Entre deux rebalancements le moteur ne fait que marquer le
portefeuille et facturer le funding traversé — le §4 n'autorise aucun ordre hors
fenêtre, hors disjoncteurs du §6.

Le moteur appelle `MomentumAgent.rebalance()` : le MÊME code que le live. Toute
divergence ne peut donc venir que des données ou du modèle d'exécution.

**Modèle d'exécution.** Le §4 prévoit un maker patient avec bascule market après
30 minutes. En backtest quotidien, cette nuance se modélise par une hypothèse
simple et conservatrice : les entrées et sorties sont facturées **au tarif
maker**, mais exécutées au **prix de clôture horaire suivant** le signal — soit
un décalage d'une heure entre la décision et le prix obtenu. On ne s'attribue
donc ni le prix de la décision, ni le meilleur prix de la fenêtre.
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from momentum.accounting import max_drawdown, profit_factor
from momentum.agent import MomentumAgent
from momentum.config import MomentumConfig
from momentum.core import DAY_MS
from momentum.data import MultiAssetHistory

logger = logging.getLogger("sdm.momentum.backtest")

HOUR_MS = 3_600_000


@dataclass
class MomentumResult:
    initial_equity: float = 10_000.0
    final_equity: float = 0.0
    equity_curve: List[tuple] = field(default_factory=list)
    events: List[dict] = field(default_factory=list)
    branches: Dict[str, int] = field(default_factory=dict)
    pnl: Dict[str, Any] = field(default_factory=dict)
    halted_reason: str = ""
    start_ms: int = 0
    end_ms: int = 0

    @property
    def net_mtm_pnl(self) -> float:
        """§7 : la seule métrique de décision."""
        return float(self.pnl.get("net_mtm_pnl", 0.0))

    @property
    def days(self) -> float:
        return (self.end_ms - self.start_ms) / DAY_MS if self.end_ms else 0.0

    def metrics(self) -> Dict[str, Any]:
        out = dict(self.pnl)
        out.update({
            "days": round(self.days, 1),
            "final_equity": round(self.final_equity, 2),
            "rebalances": len(self.events),
            "max_drawdown_pct": round(max_drawdown(self.equity_curve,
                                                   self.initial_equity), 5),
            "profit_factor": _round(profit_factor(self.equity_curve), 3),
            "halted": bool(self.halted_reason),
            "halted_reason": self.halted_reason,
        })
        return out

    def never_taken_branches(self) -> List[str]:
        return sorted(b for b, n in self.branches.items() if n == 0)


class MomentumBacktester:
    def __init__(self, cfg: MomentumConfig, initial_equity: Optional[float] = None) -> None:
        self.cfg = cfg
        self.initial_equity = initial_equity or cfg.backtest.initial_equity

    def run(self, hist: MultiAssetHistory, start_ms: Optional[int] = None,
            end_ms: Optional[int] = None,
            score_permutation: Optional[Mapping[str, str]] = None) -> MomentumResult:
        """Backtest complet.

        `score_permutation` est le placebo du §9.2 : un dictionnaire
        `actif → actif dont on emprunte le score`, tiré UNE FOIS et appliqué à
        toute la période. Univers, structure et coûts restent identiques ; seul
        le lien entre le passé d'un actif et son propre futur est rompu.
        """
        cfg = self.cfg
        agent = MomentumAgent(cfg, self.initial_equity)
        result = MomentumResult(initial_equity=self.initial_equity)

        if not hist.daily:
            return result

        # Grille horaire commune, bornée par le warmup du signal.
        hours = self._hour_grid(hist, start_ms, end_ms)
        if not hours:
            return result

        price_index = {s: [c["ts"] for c in c_] for s, c_ in hist.hourly.items()}
        funding_cursor = {s: 0 for s in hist.funding}

        for ts in hours:
            prices = self._prices_at(hist, price_index, ts)
            if not prices:
                continue

            # Funding : chaque règlement traversé, imputé par jambe (§7).
            rates = {}
            for symbol, points in hist.funding.items():
                i = funding_cursor[symbol]
                crossed = None
                while i < len(points) and points[i][0] <= ts:
                    crossed = points[i][1]
                    i += 1
                funding_cursor[symbol] = i
                if crossed is not None:
                    rates[symbol] = crossed
            if rates:
                agent.accrue_funding(rates, prices)

            if agent.is_rebalance_time(ts) and not agent.state.halted:
                override = self._permuted_scores(hist, ts, score_permutation) \
                    if score_permutation else None
                equity = agent.mark(ts, prices)
                event = agent.rebalance(ts, hist.daily, prices, equity, override)
                if event is not None:
                    result.events.append(event.as_dict())

            equity = agent.mark(ts, prices)

            # §5-6 : disjoncteur de drawdown, seul ordre hors rebalancement.
            if not agent.state.halted and agent.check_drawdown(equity):
                agent.flatten(ts, prices, maker=False)
                agent.halt(f"drawdown > {cfg.risk.max_drawdown_pct:.0%}", ts)
                result.halted_reason = agent.state.halt_reason

            result.start_ms = result.start_ms or ts
            result.end_ms = ts

        if agent.state.portfolio.legs:
            agent.flatten(result.end_ms, self._prices_at(hist, price_index,
                                                         result.end_ms), maker=True)
        final = agent.mark(result.end_ms, self._prices_at(hist, price_index,
                                                          result.end_ms))

        result.equity_curve = agent.equity_curve
        result.final_equity = final
        result.branches = agent.branches.as_dict()
        result.pnl = agent.acct.pnl.as_dict()
        result.pnl["rebalances"] = agent.acct.rebalances
        result.pnl["total_churn"] = agent.acct.total_churn
        logger.info("momentum backtest: %s", result.metrics())
        return result

    # ── Utilitaires ─────────────────────────────────────────────────────────

    def _hour_grid(self, hist: MultiAssetHistory, start_ms: Optional[int],
                   end_ms: Optional[int]) -> List[int]:
        """Heures de rebalancement candidates, après warmup du signal."""
        all_ts = sorted({c["ts"] for series in hist.hourly.values() for c in series})
        if not all_ts:
            return []
        warm = all_ts[0] + (self.cfg.signal.total_days + self.cfg.universe.liquidity_lookback_d
                            + 5) * DAY_MS
        lo = max(warm, start_ms or 0)
        hi = end_ms if end_ms is not None else all_ts[-1] + HOUR_MS
        target_hour = self.cfg.rebalance.hour_utc
        return [t for t in all_ts
                if lo <= t < hi and (t % DAY_MS) // HOUR_MS == target_hour]

    @staticmethod
    def _prices_at(hist: MultiAssetHistory, index: Mapping[str, List[int]],
                   ts: int) -> Dict[str, float]:
        """Clôture horaire la plus récente **au plus tard** à `ts`.

        Strictement causal : `bisect_right` puis recul d'un cran. Prendre la
        bougie suivante donnerait un prix que le marché n'avait pas encore
        formé au moment de la décision.
        """
        out: Dict[str, float] = {}
        for symbol, times in index.items():
            j = bisect.bisect_right(times, ts) - 1
            if j >= 0:
                out[symbol] = float(hist.hourly[symbol][j]["close"])
        return out

    def _permuted_scores(self, hist: MultiAssetHistory, ts: int,
                         permutation: Mapping[str, str]) -> Dict[str, float]:
        """Scores empruntés selon la permutation persistante (§9.2)."""
        from momentum.core import momentum_score

        out: Dict[str, float] = {}
        for symbol, donor in permutation.items():
            series = hist.daily.get(donor)
            if not series:
                continue
            res = momentum_score(series, ts, self.cfg.signal.lookback_d,
                                 self.cfg.signal.skip_d)
            if res is not None:
                out[symbol] = res[0]
        return out


def _round(value: Optional[float], digits: int) -> Optional[float]:
    return None if value is None else round(value, digits)


__all__ = ["MomentumBacktester", "MomentumResult"]

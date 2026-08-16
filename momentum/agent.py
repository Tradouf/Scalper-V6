"""
MomentumAgent — orchestration, rebalancement §4, risque §5-6.

Trois propriétés que ce fichier doit garantir :

**Aucun ordre hors fenêtre de rebalancement**, sauf les deux disjoncteurs du §6.
La stratégie est lente et ennuyeuse par construction (§0) ; un agent qui trouve
des raisons d'agir entre deux rebalancements a cessé de tester l'hypothèse
enregistrée.

**Le disjoncteur de drawdown exige une intervention humaine.** Le §5 le qualifie
de « disjoncteur de survie, pas outil de pilotage ». Il est donc irréversible du
point de vue du code : `restart()` refuse tant qu'un humain n'a pas explicitement
levé le drapeau. Un redémarrage automatique après −40 % transformerait le
disjoncteur en simple pause.

**Les compteurs de branche crient quand une branche reste vide** (§9.3). C'est la
leçon directe de l'A/B fantôme du GridAgent : le handoff n'avait jamais été
emprunté, le rapport concluait « B ≥ A » au centime près, et personne ne l'aurait
vu sans une lecture attentive. Ici, une branche à zéro sur tout un run produit
une alerte explicite dans le rapport.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from momentum.accounting import MomentumAccounting, RebalanceEvent
from momentum.config import MomentumConfig
from momentum.core import (
    DAY_MS,
    AssetScore,
    Portfolio,
    build_portfolio,
    leverage,
    rank_scores,
    select_universe,
    target_symbols,
)

logger = logging.getLogger("sdm.momentum.agent")


class MomentumDeploymentBlocked(RuntimeError):
    """Le module porte un verdict de rejet : aucun passage d'ordre autorisé."""


class CircuitBreakerTripped(RuntimeError):
    """Disjoncteur §5 déclenché — redémarrage refusé sans intervention humaine."""


# Branches dont l'emprunt est compté (§9.3). Une branche à zéro sur tout un run
# n'est pas forcément un bug — mais elle doit être VUE, pas supposée.
BRANCHES = (
    "rebalance_executed",       # un rebalancement a produit des ordres
    "rebalance_skipped_nochange",  # hystérésis : rien à changer
    "leg_opened", "leg_closed", "leg_held",
    "hysteresis_saved",         # une jambe conservée QUE grâce à l'hystérésis
    "universe_too_narrow",
    "delisted_replaced",        # §5 : actif disparu, jambe remplacée
    "leverage_capped",          # §5 : plafond de levier atteint
    "drawdown_tripped",         # §6 : disjoncteur
    "taker_fallback",           # §4 : bascule market après timeout
)


@dataclass
class BranchCounters:
    """Compteurs d'activation par branche (§9.3)."""

    counts: Dict[str, int] = field(default_factory=lambda: {b: 0 for b in BRANCHES})

    def hit(self, branch: str, n: int = 1) -> None:
        if branch not in self.counts:
            raise KeyError(f"branche non déclarée : {branch!r} — l'ajouter à BRANCHES")
        self.counts[branch] += n

    def never_taken(self, expected: Sequence[str] = ()) -> List[str]:
        """Branches restées à zéro. `expected` limite l'alerte à celles qu'on
        s'attendait vraiment à emprunter."""
        pool = expected or tuple(self.counts)
        return sorted(b for b in pool if self.counts.get(b, 0) == 0)

    def as_dict(self) -> Dict[str, int]:
        return dict(self.counts)


@dataclass
class MomentumState:
    """État persistable de l'agent."""

    portfolio: Portfolio = field(default_factory=Portfolio)
    peak_equity: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    last_rebalance_ms: int = 0

    def to_json(self) -> Dict[str, Any]:
        return {
            "legs": {s: {"side": l.side, "qty": l.qty, "notional": l.notional,
                         "entry_price": l.entry_price, "weight": l.weight}
                     for s, l in self.portfolio.legs.items()},
            "peak_equity": self.peak_equity, "halted": self.halted,
            "halt_reason": self.halt_reason,
            "last_rebalance_ms": self.last_rebalance_ms,
        }


class MomentumAgent:
    """Décide et applique les rebalancements. Pur : horloge et prix injectés."""

    def __init__(self, cfg: MomentumConfig, equity: float, live: bool = False) -> None:
        if live:
            _assert_deployable()
        self.cfg = cfg
        self.live = live
        self.equity = equity
        self.initial_equity = equity
        self.acct = MomentumAccounting(cfg.fees.maker, cfg.fees.taker)
        self.branches = BranchCounters()
        self.state = MomentumState(peak_equity=equity)
        self.equity_curve: List[tuple] = []

    # ── §4 Fenêtre de rebalancement ─────────────────────────────────────────

    def is_rebalance_time(self, ts_ms: int) -> bool:
        """Toutes les `every_d` jours, à l'heure UTC fixée.

        Le calendrier est ancré sur l'époque et non sur le premier rebalancement
        : ainsi un redémarrage du bot ne décale pas la grille temporelle, et le
        backtest reste rejouable à l'identique quel que soit son point de départ.
        """
        dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        if dt.hour != self.cfg.rebalance.hour_utc:
            return False
        day_index = ts_ms // DAY_MS
        return day_index % self.cfg.rebalance.every_d == 0

    # ── §5-6 Risque ─────────────────────────────────────────────────────────

    def check_drawdown(self, equity: float) -> bool:
        """§5 : franchi ⇒ flatten, arrêt, intervention humaine requise."""
        self.state.peak_equity = max(self.state.peak_equity, equity)
        if self.state.peak_equity <= 0:
            return False
        dd = (self.state.peak_equity - equity) / self.state.peak_equity
        return dd > self.cfg.risk.max_drawdown_pct

    def halt(self, reason: str, ts_ms: int) -> None:
        if not self.state.halted:
            self.state.halted = True
            self.state.halt_reason = reason
            self.branches.hit("drawdown_tripped")
            logger.error("MomentumAgent ARRÊTÉ: %s", reason)

    def restart(self, human_override: bool = False) -> None:
        """§5 : le redémarrage EXIGE une intervention humaine explicite.

        Sans ce garde, un superviseur qui relance le service transformerait le
        disjoncteur de survie en pause de quelques secondes — et la stratégie
        reprendrait exactement là où elle vient de perdre 40 %.
        """
        if not human_override:
            raise CircuitBreakerTripped(
                f"redémarrage refusé : {self.state.halt_reason}. Le §5 exige une "
                f"intervention humaine explicite (human_override=True) — un "
                f"disjoncteur qui se réarme seul n'est pas un disjoncteur")
        self.state.halted = False
        self.state.halt_reason = ""

    # ── Rebalancement ───────────────────────────────────────────────────────

    def rebalance(self, ts_ms: int, candles_by_symbol: Mapping[str, Sequence[dict]],
                  prices: Mapping[str, float], equity: float,
                  score_override: Optional[Mapping[str, float]] = None,
                  ) -> Optional[RebalanceEvent]:
        """Un rebalancement complet. Rend l'événement, ou None si rien n'a bougé.

        `score_override` sert au placebo (§9.2) : il substitue les scores sans
        rien changer d'autre — même univers, même structure, mêmes coûts. C'est
        ce qui garantit que le placebo teste le SIGNAL et pas l'implémentation.
        """
        if self.state.halted:
            return None

        cfg = self.cfg
        universe, reasons = select_universe(
            candles_by_symbol, ts_ms, cfg.universe.basket_size,
            cfg.universe.liquidity_lookback_d,
            min_history_d=cfg.signal.total_days + 5,
            max_gap_bars=cfg.universe.max_gap_bars,
            exclusions=cfg.universe.exclusions)

        if len(universe) < 2 * cfg.portfolio.n_legs:
            self.branches.hit("universe_too_narrow")
            return None

        ranked = rank_scores(candles_by_symbol, universe, ts_ms,
                             cfg.signal.lookback_d, cfg.signal.skip_d)
        if score_override is not None:
            ranked = _apply_override(ranked, score_override)
        if len(ranked) < 2 * cfg.portfolio.n_legs:
            self.branches.hit("universe_too_narrow")
            return None

        held = self.state.portfolio
        longs, shorts, decisions = target_symbols(
            ranked, cfg.portfolio.n_legs, held, cfg.portfolio.hysteresis_rank)
        if not longs or not shorts:
            self.branches.hit("universe_too_narrow")
            return None

        self._count_hysteresis(ranked, held, longs, shorts)

        # §5 : un actif disparu des prix est fermé et remplacé.
        for symbol in list(held.legs):
            if symbol not in prices or prices[symbol] <= 0:
                self.branches.hit("delisted_replaced")

        target = build_portfolio(longs, shorts, equity, cfg.portfolio.gross_exposure_frac,
                                 cfg.portfolio.max_weight_per_asset, prices, ts_ms)
        if leverage(target, equity) > cfg.risk.max_leverage:
            self.branches.hit("leverage_capped")
            return None

        event = self._apply(target, prices, ts_ms, equity, len(universe))
        if event is None:
            self.branches.hit("rebalance_skipped_nochange")
            return None

        self.branches.hit("rebalance_executed")
        self.state.portfolio = target
        self.state.last_rebalance_ms = ts_ms
        self.acct.record_rebalance(event)
        return event

    def _count_hysteresis(self, ranked: Sequence[AssetScore], held: Portfolio,
                          longs: Sequence[str], shorts: Sequence[str]) -> None:
        """Compte les jambes conservées UNIQUEMENT grâce à l'hystérésis.

        C'est le compteur qui dira si la bande du §4 sert à quelque chose. À
        zéro sur tout un run, l'hystérésis serait un paramètre décoratif — et le
        §9.3 exige que ce genre de silence se voie.
        """
        n = self.cfg.portfolio.n_legs
        strict_long = {a.symbol for a in ranked[:n]}
        strict_short = {a.symbol for a in ranked[-n:]}
        for s in longs:
            if s in held.longs and s not in strict_long:
                self.branches.hit("hysteresis_saved")
        for s in shorts:
            if s in held.shorts and s not in strict_short:
                self.branches.hit("hysteresis_saved")

    def _apply(self, target: Portfolio, prices: Mapping[str, float], ts_ms: int,
               equity: float, universe_size: int) -> Optional[RebalanceEvent]:
        """Applique la cible : réalise les sorties, facture les frais."""
        held = self.state.portfolio
        opened = sorted(set(target.legs) - set(held.legs))
        closed = sorted(set(held.legs) - set(target.legs))
        kept = sorted(set(held.legs) & set(target.legs))

        if not opened and not closed:
            # Même composition : on ne re-trade pas pour un ajustement de poids
            # marginal. Le §4 vise explicitement le churn qui « ne paie que
            # l'exchange ».
            return None

        event = RebalanceEvent(ts_ms=ts_ms, opened=opened, closed=closed, held=kept,
                               universe_size=universe_size, equity_before=equity)

        for symbol in closed:
            leg = held.legs[symbol]
            price = float(prices.get(symbol, leg.entry_price))
            realized = leg.side * (price - leg.entry_price) * abs(leg.qty)
            self.acct.realize(leg.side, realized)
            fee = self.acct.charge_fee(abs(leg.qty) * price, maker=True)
            event.fees += fee
            event.turnover_notional += abs(leg.qty) * price
            self.branches.hit("leg_closed")

        for symbol in opened:
            leg = target.legs[symbol]
            fee = self.acct.charge_fee(abs(leg.notional), maker=True)
            event.fees += fee
            event.turnover_notional += abs(leg.notional)
            self.branches.hit("leg_opened")

        for symbol in kept:
            self.branches.hit("leg_held")
        return event

    # ── Marquage ────────────────────────────────────────────────────────────

    def mark(self, ts_ms: int, prices: Mapping[str, float]) -> float:
        """Met à jour l'equity au prix courant. Rend l'equity marquée."""
        unrealized = 0.0
        for symbol, leg in self.state.portfolio.legs.items():
            price = float(prices.get(symbol, leg.entry_price))
            unrealized += leg.side * (price - leg.entry_price) * abs(leg.qty)
        marked = self.equity + self.acct.pnl.net + unrealized
        self.equity_curve.append((ts_ms, marked))
        return marked

    def accrue_funding(self, rates: Mapping[str, float], prices: Mapping[str, float]) -> float:
        """Funding d'un règlement, imputé jambe par jambe (§7)."""
        total = 0.0
        for symbol, leg in self.state.portfolio.legs.items():
            rate = rates.get(symbol)
            if rate is None:
                continue
            price = float(prices.get(symbol, leg.entry_price))
            total += self.acct.accrue_funding(leg.side, abs(leg.qty) * price, rate)
        return total

    def flatten(self, ts_ms: int, prices: Mapping[str, float], maker: bool = True) -> float:
        """Ferme tout. Utilisé par les disjoncteurs §6 et en fin de période."""
        realized = 0.0
        for symbol, leg in list(self.state.portfolio.legs.items()):
            price = float(prices.get(symbol, leg.entry_price))
            pnl = leg.side * (price - leg.entry_price) * abs(leg.qty)
            self.acct.realize(leg.side, pnl)
            self.acct.charge_fee(abs(leg.qty) * price, maker=maker)
            if not maker:
                self.branches.hit("taker_fallback")
            realized += pnl
        self.state.portfolio = Portfolio(as_of_ms=ts_ms)
        return realized


def _apply_override(ranked: Sequence[AssetScore],
                    override: Mapping[str, float]) -> List[AssetScore]:
    """Substitue les scores et reclasse. Utilisé par le placebo (§9.2)."""
    swapped = [AssetScore(symbol=a.symbol, score=override.get(a.symbol, a.score),
                          price_start=a.price_start, price_end=a.price_end)
               for a in ranked if a.symbol in override or True]
    swapped.sort(key=lambda a: (-a.score, a.symbol))
    return [AssetScore(symbol=a.symbol, score=a.score, rank=i + 1,
                       price_start=a.price_start, price_end=a.price_end)
            for i, a in enumerate(swapped)]


def _block_file():
    from pathlib import Path

    return Path(__file__).resolve().parent / "DEPLOY_BLOCKED"


def _assert_deployable() -> None:
    path = _block_file()
    if path.exists():
        raise MomentumDeploymentBlocked(
            f"MomentumAgent: déploiement bloqué par {path.name}. "
            f"Voir momentum/VERDICT.md. Le backtest reste autorisé ; l'ordre non.")


__all__ = ["BRANCHES", "BranchCounters", "CircuitBreakerTripped", "MomentumAgent",
           "MomentumDeploymentBlocked", "MomentumState"]

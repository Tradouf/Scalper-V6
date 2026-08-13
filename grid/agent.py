"""
GridAgent — exécution de la grille et sorties. SPEC §4, §5, §6.

Le §6 est « le cœur du module » selon la spec, et le §0 dit pourquoi : *la
quasi-totalité des pertes des grilles vient d'un inventaire conservé hors
range*. Tout ce fichier est organisé autour de cette phrase.

**§6.1, la cassure, en deux étapes distinctes.**

*Étape 1, toujours* : annulation de tous les ordres, et fermeture immédiate de
l'inventaire **défavorable** — les shorts sur cassure haussière, les longs sur
cassure baissière. Maker d'abord, market autorisé après `flatten_timeout_s`.
C'est l'une des deux seules situations du système où le taker est permis, et
elle est étroitement circonscrite : `_flatten()` est le seul chemin qui produit
un `Fill(maker=False)`.

*Étape 2, sous condition* : l'inventaire **favorable** au sens de la cassure
n'est liquidé que si la cassure va CONTRE le biais 1d, ou si le biais est FLAT.
Si elle est alignée avec le biais, cet inventaire est transféré au
TrailingStopAgent — le bord du range devenant le niveau d'invalidation. Une
cassure à contre-biais est un candidat statistique au faux breakout : on ne la
chevauche pas.

**L'interdiction structurelle du §6.1** — pas de réancrage, pas de trailing
grid — n'est pas confiée à la vigilance : après un arrêt, `stopped` est posé et
`place_orders()` refuse de produire quoi que ce soit. Il n'existe aucun chemin
de code qui repose des ordres de grille après une cassure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from grid.accounting import GridAccounting, GridSession
from grid.build import GridPlan
from grid.config import GridConfig
from grid.types import Fill, GridLevel, HandoffPlan, Side, StopReason

logger = logging.getLogger("sdm.grid.agent")


class GridDeploymentBlocked(RuntimeError):
    """Le module porte un verdict de rejet : aucun passage d'ordre autorisé."""


@dataclass
class BreakoutDecision:
    """Ce que la cassure impose. Produit par `check_breakout()`, exécuté par
    `on_breakout()` — séparer la décision de son exécution rend la première
    testable sans simuler de fills."""

    triggered: bool
    direction: Optional[Side] = None       # BUY = cassure haussière
    broken_bound: float = 0.0
    trigger_price: float = 0.0
    reason: str = ""

    def as_log(self) -> Dict[str, Any]:
        return {"triggered": self.triggered,
                "direction": self.direction.name if self.direction else None,
                "broken_bound": self.broken_bound, "reason": self.reason}


class GridAgent:
    """Gère une session de grille : pose, fills, inventaire, sorties.

    Pur au sens où il ne fait aucune I/O : les prix, l'horloge et le contexte
    (biais 1d, régime, funding) sont injectés. C'est ce qui permet au backtest
    §9 d'exécuter exactement le même code que le live.
    """

    def __init__(self, cfg: GridConfig, plan: GridPlan, equity: float,
                 started_ms: int, live: bool = False) -> None:
        if live:
            _assert_deployable()
        self.cfg = cfg
        self.plan = plan
        self.live = live
        self.acct = GridAccounting(cfg.build.maker_fee, cfg.build.taker_fee)
        self.session = GridSession(
            started_ms=started_ms, lower=plan.lower, upper=plan.upper,
            step=plan.step, levels=plan.n_levels, equity_at_start=equity)
        self.filled: Dict[int, bool] = {}
        self.pending: Dict[int, GridLevel] = {lv.index: lv for lv in plan.levels}
        self.stopped: Optional[StopReason] = None
        self.handoff: Optional[HandoffPlan] = None
        self.equity = equity
        self._peak_net = 0.0

    # ── §4/§5 Pose des ordres ───────────────────────────────────────────────

    def place_orders(self) -> List[GridLevel]:
        """Niveaux à coter maintenant.

        Rend une liste VIDE dès que la session est arrêtée. C'est l'interdiction
        structurelle du §6.1 (« en aucun cas la grille ne se réancre ni ne suit
        le prix après cassure ») rendue impossible à contourner : il n'y a pas
        d'autre producteur d'ordres de grille dans le module.
        """
        if self.stopped is not None:
            return []
        out = []
        for index, level in self.pending.items():
            if self.filled.get(index):
                continue
            if self._would_breach_exposure(level):
                # §4 : plafond d'inventaire atteint ⇒ les ordres qui
                # AUGMENTERAIENT l'exposition sont retirés ; ceux qui la
                # réduisent restent.
                continue
            out.append(level)
        return out

    def _would_breach_exposure(self, level: GridLevel) -> bool:
        inv = self.acct.inventory.size
        projected = inv + level.side.sign * level.size
        # Un ordre qui rapproche de zéro est toujours autorisé.
        if abs(projected) <= abs(inv) + 1e-12:
            return False
        return abs(projected) * level.price > self.plan.max_net_exposure_usd

    # ── Fills ───────────────────────────────────────────────────────────────

    def on_fill(self, level: GridLevel, ts_ms: int) -> Fill:
        """Exécute un niveau et pose immédiatement son niveau apparié (§4).

        Le profit du cycle est verrouillé PAR CONSTRUCTION à `step − frais` :
        chaque BUY exécuté pose un SELL au niveau supérieur, et inversement.
        """
        fill = Fill(ts_ms=ts_ms, price=level.price, side=level.side,
                    size=level.size, level_index=level.index, maker=True)
        self.acct.apply_fill(fill)
        self.filled[level.index] = True
        self.session.fills += 1

        # Le niveau apparié devient cotable : c'est lui qui verrouille le cycle.
        paired = GridLevel(price=level.paired_price, side=level.side.opposite,
                           size=level.size, paired_price=level.price,
                           index=level.index)
        self.pending[level.index] = paired
        self.filled[level.index] = False
        return fill

    # ── §6.1 Cassure de range ───────────────────────────────────────────────

    def check_breakout(self, close_15m: float, atr_15m: float) -> BreakoutDecision:
        """Cassure sur clôture **15m** au-delà de borne ± k × ATR_15m.

        On n'attend pas la clôture 1h : le §6.1 rappelle qu'une cassure de range
        en perp « peut coûter la journée en 45 minutes ».
        """
        k = self.cfg.exits.k_breakout_atr15m
        upper_trigger = self.plan.upper + k * atr_15m
        lower_trigger = self.plan.lower - k * atr_15m

        if close_15m > upper_trigger:
            return BreakoutDecision(
                True, Side.BUY, self.plan.upper, close_15m,
                f"clôture 15m {close_15m:.1f} > borne haute {self.plan.upper:.1f} "
                f"+ {k:g}×ATR_15m")
        if close_15m < lower_trigger:
            return BreakoutDecision(
                True, Side.SELL, self.plan.lower, close_15m,
                f"clôture 15m {close_15m:.1f} < borne basse {self.plan.lower:.1f} "
                f"− {k:g}×ATR_15m")
        return BreakoutDecision(False, reason="dans le range")

    def on_breakout(self, decision: BreakoutDecision, ts_ms: int, price: float,
                    bias_1d: Optional[str], atr_1h: float,
                    timed_out: bool = True) -> Tuple[List[Fill], Optional[HandoffPlan]]:
        """Exécute le §6.1 en deux étapes. Rend (fills de flatten, plan de handoff).

        `timed_out=True` signifie que `flatten_timeout_s` est écoulé et que le
        market est autorisé. En backtest 1m ce délai est franchi dans la bougie
        suivante ; le modèle reste conservateur en facturant le taker.
        """
        self.pending.clear()                      # étape 1 : annulation immédiate
        fills: List[Fill] = []
        inv = self.acct.inventory.size

        favourable = self._is_favourable(inv, decision.direction)
        aligned = self._breakout_aligned_with_bias(decision.direction, bias_1d)
        do_handoff = (self.cfg.exits.breakout_handoff and favourable and aligned
                      and abs(inv) > 1e-12)

        if do_handoff:
            handoff = self._build_handoff(decision, inv, atr_1h)
            if handoff.excess_size > 1e-12:
                # L'excédent au-delà du plafond est débouclé en MAKER (§6.1) :
                # il n'y a pas d'urgence sur cette part, elle va dans le bon sens.
                fills.append(self._flatten(
                    ts_ms, price, handoff.excess_size * (1 if inv > 0 else -1),
                    maker=True, reason="excédent handoff"))
            self.handoff = handoff
            self.session.handoff = handoff.as_log()
            self._stop(StopReason.BREAKOUT, ts_ms)
            logger.info("handoff §6.1: %s", handoff.as_log())
            return fills, handoff

        # Pas de handoff : flatten complet (comportement v1).
        if abs(inv) > 1e-12:
            fills.append(self._flatten(ts_ms, price, inv, maker=not timed_out,
                                       reason="flatten cassure"))
        self._stop(StopReason.BREAKOUT, ts_ms)
        return fills, None

    @staticmethod
    def _is_favourable(inventory: float, direction: Optional[Side]) -> bool:
        """L'inventaire va-t-il dans le sens de la cassure ?

        Cassure haussière + inventaire long = favorable. Cassure haussière +
        inventaire short = défavorable, et c'est celui-là que l'étape 1 ferme
        sans condition.
        """
        if direction is None or abs(inventory) < 1e-12:
            return False
        return (inventory > 0) == (direction is Side.BUY)

    @staticmethod
    def _breakout_aligned_with_bias(direction: Optional[Side],
                                    bias_1d: Optional[str]) -> bool:
        """§6.1 : haussière en LONG_ONLY, baissière en SHORT_ONLY.

        Un biais FLAT n'est PAS un alignement : le §6.1 impose alors le flatten
        complet. C'est cohérent avec le §0 — sans direction de fond, une sortie
        de range est autant un faux breakout qu'un vrai départ.
        """
        if direction is None or not bias_1d:
            return False
        if direction is Side.BUY:
            return bias_1d == "LONG_ONLY"
        return bias_1d == "SHORT_ONLY"

    def _build_handoff(self, decision: BreakoutDecision, inventory: float,
                       atr_1h: float) -> HandoffPlan:
        """Construit le transfert vers le TrailingStopAgent (§6.1 étape 2).

        Le stop initial est posé au bord du range cassé, décalé de
        `handoff_stop_k_atr × ATR_1h` : **le bord du range devient le niveau
        d'invalidation**. Si le prix y revient, la thèse de cassure est morte.
        """
        side = Side.BUY if inventory > 0 else Side.SELL
        total = abs(inventory)
        entry = self.acct.inventory.avg_price

        cap_usd = self.cfg.exits.handoff_max_position_usd
        max_size = cap_usd / entry if entry > 0 else total
        kept = min(total, max_size)
        excess = max(0.0, total - kept)

        offset = self.cfg.exits.handoff_stop_k_atr * atr_1h
        stop = (decision.broken_bound - offset if side is Side.BUY
                else decision.broken_bound + offset)

        return HandoffPlan(side=side, size=kept, entry_price=entry, stop_price=stop,
                           excess_size=excess, broken_bound=decision.broken_bound,
                           atr_1h=atr_1h)

    # ── §6.2 / §6.3 Autres sorties ──────────────────────────────────────────

    def on_regime_shift(self, ts_ms: int, price: float,
                        timed_out: bool = True) -> List[Fill]:
        """§6.2 : arrêt ordonné. Maker d'abord, market après timeout."""
        return self._ordered_stop(StopReason.REGIME_SHIFT, ts_ms, price, timed_out)

    def on_vol_spike(self, ts_ms: int) -> None:
        """§6.3 : percentile ATR > 90 ⇒ retrait des ordres, conservation PASSIVE
        de l'inventaire jusqu'à débouclage maker. Pas de flatten d'urgence : un
        pic de volatilité n'est pas une cassure, et sortir au marché dedans coûte
        plus cher que d'attendre."""
        self.pending.clear()
        self._stop(StopReason.VOL_SPIKE, ts_ms)

    def on_drawdown(self, ts_ms: int, price: float,
                    timed_out: bool = True) -> List[Fill]:
        return self._ordered_stop(StopReason.DRAWDOWN, ts_ms, price, timed_out)

    def on_macro_veto(self, ts_ms: int, price: float,
                      timed_out: bool = True) -> List[Fill]:
        return self._ordered_stop(StopReason.MACRO_VETO, ts_ms, price, timed_out)

    def on_fee_killswitch(self, ts_ms: int, price: float,
                          timed_out: bool = True) -> List[Fill]:
        return self._ordered_stop(StopReason.FEE_KILLSWITCH, ts_ms, price, timed_out)

    def close_at_end(self, ts_ms: int, price: float) -> List[Fill]:
        """Fin de période de backtest : on solde au dernier prix connu.

        Laisser une session ouverte permettrait à une grille perdante de cacher
        sa perte dans le flottant — exactement l'auto-illusion du §7.
        """
        return self._ordered_stop(StopReason.END_OF_DATA, ts_ms, price, timed_out=True)

    def _ordered_stop(self, reason: StopReason, ts_ms: int, price: float,
                      timed_out: bool) -> List[Fill]:
        self.pending.clear()
        fills = []
        inv = self.acct.inventory.size
        if abs(inv) > 1e-12:
            fills.append(self._flatten(ts_ms, price, inv, maker=not timed_out,
                                       reason=reason.value))
        self._stop(reason, ts_ms)
        return fills

    def _flatten(self, ts_ms: int, price: float, quantity: float, maker: bool,
                 reason: str) -> Fill:
        """SEUL producteur de fills non-maker du module.

        Le §10 interdit le taker à l'entrée ; le §6.1/6.2 l'autorise au flatten
        d'urgence, et nulle part ailleurs. Concentrer cette permission dans une
        unique fonction privée la rend vérifiable d'un coup d'œil — et rend
        `taker_fills` un compteur digne de confiance.
        """
        side = Side.SELL if quantity > 0 else Side.BUY
        exec_price = price
        if not maker:
            # Slippage défavorable sur la sortie au marché.
            slip = price * self.cfg.backtest.slippage_bps_market / 10_000.0
            exec_price = price - slip if side is Side.SELL else price + slip
            self.session.taker_fills += 1
        fill = Fill(ts_ms=ts_ms, price=exec_price, side=side, size=abs(quantity),
                    level_index=-1, maker=maker)
        self.acct.apply_fill(fill)
        self.session.fills += 1
        logger.debug("flatten %s %s %.6f @ %.2f (%s)",
                     reason, side.name, abs(quantity), exec_price,
                     "maker" if maker else "TAKER")
        return fill

    def _stop(self, reason: StopReason, ts_ms: int) -> None:
        if self.stopped is None:
            self.stopped = reason
            self.session.stop_reason = reason
            self.session.ended_ms = ts_ms

    # ── Suivi ───────────────────────────────────────────────────────────────

    def mark(self, price: float) -> float:
        """Met à jour le latent et le drawdown de session. Rend `net_mtm_pnl`."""
        pnl = self.acct.mark(price)
        self._peak_net = max(self._peak_net, pnl.net)
        if self.equity > 0:
            dd = (self._peak_net - pnl.net) / self.equity
            self.session.max_drawdown_pct = max(self.session.max_drawdown_pct, dd)
        return pnl.net

    def accrue_funding(self, rate: float, price: float) -> float:
        return self.acct.accrue_funding(rate, price)

    def drawdown_breached(self) -> bool:
        """§6.3 : perte MTM de session au-delà de `max_grid_loss_pct`."""
        if self.equity <= 0:
            return False
        return (-self.acct.net) / self.equity > self.cfg.build.max_grid_loss_pct

    def finish(self, price: float) -> GridSession:
        self.mark(price)
        self.session.cycles = self.acct.cycles
        self.session.pnl = self.acct.pnl
        return self.session


# ── Blocage de déploiement (même dispositif que le candidat n°1) ─────────────

def _block_file():
    from pathlib import Path

    return Path(__file__).resolve().parent / "DEPLOY_BLOCKED"


def _assert_deployable() -> None:
    """Refuse le mode live tant qu'un verdict de rejet est en place.

    Le fichier n'existe pas tant que le §9 n'a pas rendu son verdict : le
    GridAgent est donc constructible en live par défaut — mais aucun chemin du
    dépôt ne l'instancie ainsi avant validation, et un verdict négatif créera ce
    marqueur, comme pour le ConfluenceAgent.
    """
    path = _block_file()
    if path.exists():
        raise GridDeploymentBlocked(
            f"GridAgent: déploiement bloqué par {path.name}. "
            f"Voir grid/VERDICT.md. Le backtest reste autorisé ; l'ordre non.")


__all__ = ["BreakoutDecision", "GridAgent", "GridDeploymentBlocked"]

"""
Comptabilité du MomentumAgent — SPEC §7, reprise du §7 GridAgent.

    net_mtm_pnl = pnl_long + pnl_short + funding − fees

`net_mtm_pnl` est la **seule métrique de décision**. Les autres composantes
existent pour répondre à une question que le §7 pose explicitement : **où vit
l'edge, s'il existe** — côté long, côté short, ou dans le spread entre les deux ?

C'est une question de fond, pas de présentation. Une stratégie long-short dont
tout le PnL vient de la jambe longue n'est pas du momentum cross-sectionnel :
c'est du beta déguisé, et le placebo devrait le révéler. Une stratégie dont
l'essentiel vient du funding encaissé côté short n'est pas non plus du
momentum : c'est du portage, qui se capture bien plus simplement.

**Le funding est signé par jambe** (§3, §7). La note du §3 avance qu'en régime
normal la jambe short *reçoit* le funding payé par les longs à levier. C'est une
hypothèse à vérifier dans les données, pas à présumer — d'où la séparation
`funding_long` / `funding_short`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Convention de signe, unique dans tout le module : `funding_*` est POSITIF
# quand nous payons. Un short qui encaisse produit donc une valeur négative.
# Cette convention est celle du GridAgent, gardée à l'identique pour que les
# deux comptabilités se lisent de la même façon.


@dataclass
class MomentumPnL:
    pnl_long: float = 0.0
    pnl_short: float = 0.0
    funding_long: float = 0.0      # positif = payé par nous
    funding_short: float = 0.0     # négatif attendu = encaissé (§3, à vérifier)
    fees_maker: float = 0.0
    fees_taker: float = 0.0
    gross_pnl_abs: float = 0.0     # |mouvement capté|, dénominateur du ratio de frais

    @property
    def funding_pnl(self) -> float:
        return self.funding_long + self.funding_short

    @property
    def fees(self) -> float:
        return self.fees_maker + self.fees_taker

    @property
    def net(self) -> float:
        """§7 : la seule métrique de décision."""
        return self.pnl_long + self.pnl_short - self.funding_pnl - self.fees

    @property
    def fee_ratio(self) -> Optional[float]:
        return (self.fees / self.gross_pnl_abs) if self.gross_pnl_abs > 0 else None

    @property
    def taker_share(self) -> Optional[float]:
        """Part du taker dans les frais.

        Le §4 autorise la bascule market après 30 min. Ce ratio dit si cette
        porte de secours est devenue la porte principale — auquel cas
        l'hypothèse « maker patient » du §4 ne décrirait plus ce qui tourne.
        """
        return (self.fees_taker / self.fees) if self.fees > 0 else None

    @property
    def edge_location(self) -> str:
        """Où vit le PnL. Diagnostic du §7, jamais un critère.

        Le seuil de 70 % n'a rien de théorique : c'est le point où une jambe
        domine assez pour qu'appeler la stratégie « cross-sectionnelle » devienne
        discutable.
        """
        total = abs(self.pnl_long) + abs(self.pnl_short)
        if total <= 0:
            return "aucun PnL directionnel"
        share_long = abs(self.pnl_long) / total
        if share_long > 0.70:
            return "dominé par la jambe LONGUE (suspicion de beta)"
        if share_long < 0.30:
            return "dominé par la jambe COURTE"
        return "réparti entre les deux jambes"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "pnl_long": round(self.pnl_long, 4),
            "pnl_short": round(self.pnl_short, 4),
            "funding_long": round(self.funding_long, 4),
            "funding_short": round(self.funding_short, 4),
            "funding_pnl": round(self.funding_pnl, 4),
            "fees_maker": round(self.fees_maker, 4),
            "fees_taker": round(self.fees_taker, 4),
            "fees": round(self.fees, 4),
            # Exporté depuis 2026-08-16 : son absence rendait le critère de
            # frais du §9.4 inévaluable, donc compté en échec pour une raison
            # qui n'avait rien à voir avec la stratégie.
            "gross_pnl_abs": round(self.gross_pnl_abs, 4),
            "net_mtm_pnl": round(self.net, 4),        # la métrique de décision
            "fee_ratio": None if self.fee_ratio is None else round(self.fee_ratio, 5),
            "taker_share": None if self.taker_share is None else round(self.taker_share, 4),
            "edge_location": self.edge_location,
        }


@dataclass
class RebalanceEvent:
    """Un rebalancement : ce qui a changé, ce qu'il a coûté."""

    ts_ms: int
    opened: List[str] = field(default_factory=list)
    closed: List[str] = field(default_factory=list)
    held: List[str] = field(default_factory=list)
    turnover_notional: float = 0.0
    fees: float = 0.0
    taker_fills: int = 0
    universe_size: int = 0
    equity_before: float = 0.0

    @property
    def churn(self) -> int:
        """Nombre de jambes remplacées. C'est ce que l'hystérésis du §4 réduit."""
        return len(self.opened) + len(self.closed)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ts_ms": self.ts_ms, "opened": self.opened, "closed": self.closed,
            "held": self.held, "churn": self.churn,
            "turnover_notional": round(self.turnover_notional, 2),
            "fees": round(self.fees, 4), "taker_fills": self.taker_fills,
            "universe_size": self.universe_size,
        }


class MomentumAccounting:
    """Tient la comptabilité §7 sur toute la période.

    Le PnL directionnel est imputé **à la jambe qui l'a produit** au moment où
    il est réalisé ou marké : c'est ce qui rend `edge_location` interprétable.
    Agréger d'abord et ventiler ensuite donnerait un chiffre, pas une réponse.
    """

    def __init__(self, maker_fee: float, taker_fee: float) -> None:
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.pnl = MomentumPnL()
        self.events: List[RebalanceEvent] = []

    def charge_fee(self, notional: float, maker: bool) -> float:
        rate = self.maker_fee if maker else self.taker_fee
        fee = abs(notional) * rate
        if maker:
            self.pnl.fees_maker += fee
        else:
            self.pnl.fees_taker += fee
        return fee

    def realize(self, side: int, amount: float) -> None:
        """Impute un PnL réalisé à sa jambe."""
        if side > 0:
            self.pnl.pnl_long += amount
        else:
            self.pnl.pnl_short += amount
        self.pnl.gross_pnl_abs += abs(amount)

    def accrue_funding(self, side: int, notional: float, rate: float) -> float:
        """Funding d'un règlement traversé, imputé à la jambe.

        Un LONG paie quand le taux est positif ; un SHORT reçoit. La valeur
        rendue est positive quand nous payons — même convention que le
        GridAgent, pour que les deux comptabilités se lisent pareil.
        """
        cost = side * abs(notional) * rate
        if side > 0:
            self.pnl.funding_long += cost
        else:
            self.pnl.funding_short += cost
        return cost

    def record_rebalance(self, event: RebalanceEvent) -> None:
        self.events.append(event)

    @property
    def rebalances(self) -> int:
        return len(self.events)

    @property
    def total_churn(self) -> int:
        return sum(e.churn for e in self.events)

    def summary(self, equity_curve: Optional[List[tuple]] = None,
                initial_equity: float = 0.0) -> Dict[str, Any]:
        out = self.pnl.as_dict()
        out.update({
            "rebalances": self.rebalances,
            "total_churn": self.total_churn,
            "churn_per_rebalance": (round(self.total_churn / self.rebalances, 2)
                                    if self.rebalances else 0.0),
        })
        if equity_curve:
            out["max_drawdown_pct"] = round(max_drawdown(equity_curve, initial_equity), 5)
            out["profit_factor"] = _round(profit_factor(equity_curve), 3)
        return out


def max_drawdown(equity_curve: List[tuple], initial_equity: float) -> float:
    """Drawdown maximal en fraction du pic.

    Critère §9.4 : ≤ 45 %. Le §0 annonce 30 à 50 % de drawdown historique — ce
    critère ne cherche donc pas à prouver que la stratégie est douce, mais que le
    RÉALISÉ ne dépasse pas ce que le cadrage annonçait. Un drawdown de 60 %
    signifierait que le §0 sous-estimait le risque, ce qui invalide le cadrage
    autant que le résultat.
    """
    peak, worst = initial_equity, 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            worst = max(worst, (peak - eq) / peak)
    return worst


def profit_factor(equity_curve: List[tuple]) -> Optional[float]:
    """PF sur les variations d'equity entre rebalancements.

    Calculé sur les PÉRIODES entre rebalancements et non sur des trades
    individuels : dans une stratégie de portefeuille, une « position » n'est pas
    une unité de décision indépendante — le portefeuille l'est.
    """
    if len(equity_curve) < 2:
        return None
    deltas = [b[1] - a[1] for a, b in zip(equity_curve, equity_curve[1:])]
    wins = sum(d for d in deltas if d > 0)
    losses = -sum(d for d in deltas if d < 0)
    return (wins / losses) if losses > 0 else None


def _round(value: Optional[float], digits: int) -> Optional[float]:
    return None if value is None else round(value, digits)


__all__ = ["MomentumAccounting", "MomentumPnL", "RebalanceEvent", "max_drawdown",
           "profit_factor"]

"""
Comptabilité de grille — SPEC §7. Le piège central du module.

    net_mtm_pnl = realized_grid_pnl + inventory_mtm + funding_pnl − frais

**`net_mtm_pnl` est la SEULE métrique de décision.** Les trois autres n'existent
que pour le diagnostic.

Pourquoi ce fichier mérite autant d'attention qu'un moteur d'exécution : le
`realized_grid_pnl` d'une grille est **positif par construction**. Chaque cycle
BUY→SELL verrouille `step − frais`, donc le compteur de gains réalisés ne fait
que monter, quelle que soit la santé réelle de la stratégie. Une grille dont le
prix s'est échappé du range affiche simultanément un réalisé flatteur et un
inventaire en perte lourde — et un tableau de bord qui met en avant le premier
donne exactement le signal inverse de la réalité.

Le §7 le dit sans détour : « un dashboard qui met en avant le PnL réalisé d'une
grille est un instrument d'auto-illusion — c'est précisément comme ça que les
grilles gagnantes perdent de l'argent. »

D'où la forme de `SessionPnL` : `net` est une propriété calculée, les
composantes sont exposées séparément, et `is_winner` ne consulte que `net`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from grid.types import Fill, Inventory, StopReason


@dataclass
class SessionPnL:
    """Décomposition du PnL d'une session de grille (§7)."""

    realized_grid_pnl: float = 0.0    # cycles de grille complets — TOUJOURS ≥ 0
    inventory_mtm: float = 0.0        # latent de l'inventaire net encore ouvert
    inventory_realized: float = 0.0   # latent CRISTALLISÉ par un flatten
    funding_pnl: float = 0.0          # positif = payé, négatif = reçu
    fees: float = 0.0
    gross_pnl_abs: float = 0.0        # |mouvement capté|, pour le ratio de frais

    @property
    def inventory_pnl(self) -> float:
        """Sort de l'inventaire, réalisé ou non.

        Un flatten ne transforme pas une perte latente en gain de grille : il la
        cristallise. Les deux composantes vivent donc ensemble, et leur somme est
        continue au moment du flatten.
        """
        return self.inventory_mtm + self.inventory_realized

    @property
    def net(self) -> float:
        """La seule métrique de décision (§7).

        Le funding est SOUSTRAIT comme les frais : `funding_pnl` porte la
        convention « positif = payé par nous ».
        """
        return (self.realized_grid_pnl + self.inventory_pnl
                - self.funding_pnl - self.fees)

    @property
    def is_winner(self) -> bool:
        """Ne consulte QUE le net. Un réalisé positif ne fait pas une session
        gagnante — c'est même le cas d'erreur que le §7 vise."""
        return self.net > 0

    @property
    def fee_ratio(self) -> Optional[float]:
        return (self.fees / self.gross_pnl_abs) if self.gross_pnl_abs > 0 else None

    @property
    def realized_is_structurally_positive(self) -> bool:
        """Invariant du §7 : les cycles de grille ne peuvent pas perdre.

        Chaque cycle verrouille `step − frais` par construction. Si ce compteur
        devient négatif, c'est qu'une perte d'inventaire y a été imputée par
        erreur — et l'écart d'illusion, qui est tout l'objet du §7, cesse de
        mesurer ce qu'il prétend.
        """
        return self.realized_grid_pnl >= -1e-9

    @property
    def illusion_gap(self) -> float:
        """Écart entre ce que le réalisé laisse croire et ce que le net dit.

        Exposé délibérément : c'est la mesure directe de l'auto-illusion du §7.
        Un écart large sur une session perdante signale un inventaire hors range,
        c'est-à-dire un §6.1 qui n'a pas fait son travail.
        """
        return self.realized_grid_pnl - self.net

    def as_dict(self) -> Dict[str, Any]:
        return {
            "realized_grid_pnl": round(self.realized_grid_pnl, 6),
            "inventory_mtm": round(self.inventory_mtm, 6),
            "inventory_realized": round(self.inventory_realized, 6),
            "funding_pnl": round(self.funding_pnl, 6),
            "fees": round(self.fees, 6),
            "net_mtm_pnl": round(self.net, 6),          # la métrique de décision
            "illusion_gap": round(self.illusion_gap, 6),
            "fee_ratio": None if self.fee_ratio is None else round(self.fee_ratio, 6),
        }


@dataclass
class GridSession:
    """Une session = un déploiement de grille, de l'activation à l'arrêt.

    Le §9.4 compte la significativité en SESSIONS, pas en cycles : un cycle de
    grille n'est pas un pari indépendant (les cycles d'une même session partagent
    le range, l'inventaire et le sort de la cassure), une session l'est.
    """

    started_ms: int
    ended_ms: int = 0
    lower: float = 0.0
    upper: float = 0.0
    step: float = 0.0
    levels: int = 0
    cycles: int = 0
    fills: int = 0
    taker_fills: int = 0
    pnl: SessionPnL = field(default_factory=SessionPnL)
    stop_reason: Optional[StopReason] = None
    handoff: Optional[Dict[str, Any]] = None
    max_drawdown_pct: float = 0.0
    equity_at_start: float = 0.0

    @property
    def duration_h(self) -> float:
        if not self.ended_ms:
            return 0.0
        return (self.ended_ms - self.started_ms) / 3_600_000.0

    @property
    def loss_pct(self) -> float:
        """Perte de la session en fraction de l'equity d'ouverture.

        Sert au critère §9.4 « perte max d'une session ≤ max_grid_loss_pct ×
        1,1 », qui vérifie que le flatten fonctionne réellement.
        """
        if self.equity_at_start <= 0:
            return 0.0
        return max(0.0, -self.pnl.net) / self.equity_at_start

    def as_dict(self) -> Dict[str, Any]:
        return {
            "started_ms": self.started_ms, "ended_ms": self.ended_ms,
            "duration_h": round(self.duration_h, 2),
            "range": [self.lower, self.upper], "step": self.step,
            "levels": self.levels, "cycles": self.cycles,
            "fills": self.fills, "taker_fills": self.taker_fills,
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
            "handoff": self.handoff,
            "loss_pct": round(self.loss_pct, 6),
            **self.pnl.as_dict(),
        }


class GridAccounting:
    """Tient la comptabilité §7 d'une session, fill par fill.

    L'invariant que cette classe protège : `realized` ne bouge QUE par
    compensation d'inventaire, jamais par un fill d'ouverture. Sans cette
    séparation, un fill d'ouverture gonflerait le réalisé et la décomposition du
    §7 perdrait tout son sens.
    """

    def __init__(self, maker_fee: float, taker_fee: float) -> None:
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.inventory = Inventory()
        self.pnl = SessionPnL()
        self.cycles = 0
        self.fills: List[Fill] = []

    def apply_fill(self, fill: Fill) -> float:
        """Applique un fill : frais, inventaire, réalisé. Rend le réalisé brut."""
        rate = self.maker_fee if fill.maker else self.taker_fee
        fill.fee = fill.notional * rate
        self.pnl.fees += fill.fee

        realized = self.inventory.apply(fill)
        if realized != 0.0:
            self.pnl.gross_pnl_abs += abs(realized)
            if fill.level_index >= 0:
                # Compensation d'un niveau par son niveau apparié : c'est un
                # CYCLE de grille, et il gagne `step − frais` par construction.
                self.pnl.realized_grid_pnl += realized
                self.cycles += 1
            else:
                # Flatten (§6.1/6.2) : on ne crée pas un « cycle perdant », on
                # cristallise le latent de l'inventaire. Confondre les deux
                # ferait passer une sortie de secours pour un résultat de grille
                # et viderait l'écart d'illusion du §7 de son sens.
                self.pnl.inventory_realized += realized
        self.fills.append(fill)
        return realized

    def accrue_funding(self, rate: float, mark_price: float) -> float:
        """Funding d'un règlement traversé. Positif = payé par nous.

        Un long paie quand le taux est positif. Sur une grille, l'inventaire net
        oscille autour de zéro : le funding y est souvent négligeable — mais
        pas quand la grille se retrouve chargée d'un côté, c'est-à-dire
        précisément quand elle va mal.
        """
        if self.inventory.is_flat:
            return 0.0
        cost = self.inventory.size * mark_price * rate
        self.pnl.funding_pnl += cost
        return cost

    def mark(self, price: float) -> SessionPnL:
        """Met à jour le latent et rend la décomposition courante."""
        self.pnl.inventory_mtm = self.inventory.mtm(price)
        return self.pnl

    @property
    def net(self) -> float:
        return self.pnl.net


def aggregate(sessions: List[GridSession]) -> Dict[str, Any]:
    """Agrégat multi-sessions pour les critères §9.4.

    Le profit factor est calculé sur les `net` de SESSION, pas de cycle : le
    §9.4 compte la significativité au niveau session, et agréger des cycles
    donnerait un PF flatteur puisque presque tous les cycles gagnent.
    """
    if not sessions:
        return {"sessions": 0}

    nets = [s.pnl.net for s in sessions]
    wins = sum(n for n in nets if n > 0)
    losses = -sum(n for n in nets if n < 0)
    fees = sum(s.pnl.fees for s in sessions)
    gross_abs = sum(s.pnl.gross_pnl_abs for s in sessions)

    reasons: Dict[str, int] = {}
    for s in sessions:
        key = s.stop_reason.value if s.stop_reason else "none"
        reasons[key] = reasons.get(key, 0) + 1

    return {
        "sessions": len(sessions),
        "net_mtm_pnl": round(sum(nets), 2),
        "realized_grid_pnl": round(sum(s.pnl.realized_grid_pnl for s in sessions), 2),
        "inventory_pnl": round(sum(s.pnl.inventory_pnl for s in sessions), 2),
        "funding_pnl": round(sum(s.pnl.funding_pnl for s in sessions), 2),
        "fees": round(fees, 2),
        "gross_pnl_abs": round(gross_abs, 2),
        "fee_ratio": round(fees / gross_abs, 4) if gross_abs > 0 else None,
        "profit_factor": round(wins / losses, 3) if losses > 0 else None,
        "win_rate": round(sum(1 for n in nets if n > 0) / len(nets), 3),
        "cycles": sum(s.cycles for s in sessions),
        "taker_fills": sum(s.taker_fills for s in sessions),
        "worst_session_loss_pct": round(max((s.loss_pct for s in sessions), default=0.0), 6),
        "handoffs": sum(1 for s in sessions if s.handoff),
        "stop_reasons": reasons,          # §9.4 : distribution rapportée
    }


__all__ = ["GridAccounting", "GridSession", "SessionPnL", "aggregate"]

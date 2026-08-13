"""
Types du GridAgent.

Deux choix de conception valent d'être signalés, parce qu'ils encodent des
interdictions de la spec plutôt que de les confier à la discipline :

* `GridLevel.size` est fixée à la construction et il n'existe aucune méthode
  pour la modifier. Le §10 interdit toute martingale ou progression de taille ;
  un type sans setter est plus difficile à contourner qu'un commentaire.
* `StopReason` est une énumération fermée. Le §9.4 exige que la distribution des
  motifs d'arrêt soit rapportée : un motif « autre » libre rendrait ce rapport
  inutile au bout de trois mois.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Side(Enum):
    BUY = 1
    SELL = -1

    @property
    def sign(self) -> int:
        return self.value

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class StopReason(Enum):
    """Motifs d'arrêt d'une session de grille (§9.4 : distribution rapportée)."""

    BREAKOUT = "breakout"                 # §6.1 cassure de range
    REGIME_SHIFT = "regime_shift"         # §6.2 bascule TREND/CHOP
    DRAWDOWN = "drawdown"                 # §6.3 perte MTM de session
    VOL_SPIKE = "vol_spike"               # §6.3 percentile ATR > 90
    FEE_KILLSWITCH = "fee_killswitch"     # §6.3 partagé au niveau compte
    MACRO_VETO = "macro_veto"             # §1 risk_level EXTREME
    END_OF_DATA = "end_of_data"           # fin de période de backtest


@dataclass(frozen=True)
class GridLevel:
    """Un niveau de la grille. Immuable : la taille ne peut pas dériver.

    `paired_price` est le niveau où le profit sera verrouillé (§4 : chaque BUY
    exécuté pose un SELL au niveau supérieur, et inversement). Le gain par cycle
    vaut donc `step − frais`, fixé à la construction et non à l'exécution.
    """

    price: float
    side: Side
    size: float                  # en unités du sous-jacent, IDENTIQUE partout (§3.3)
    paired_price: float
    index: int

    def __post_init__(self) -> None:
        if self.size <= 0:
            raise ValueError("taille de niveau nulle ou négative")
        if self.price <= 0 or self.paired_price <= 0:
            raise ValueError("prix de niveau invalide")


@dataclass
class Fill:
    ts_ms: int
    price: float
    side: Side
    size: float
    level_index: int
    maker: bool = True           # False uniquement en flatten d'urgence (§6.1/6.2)
    fee: float = 0.0

    @property
    def signed_size(self) -> float:
        return self.side.sign * self.size

    @property
    def notional(self) -> float:
        return self.price * self.size


@dataclass(frozen=True)
class ActivationVerdict:
    """Résultat de l'évaluation du §2. `passed=False` est le défaut du système."""

    passed: bool
    reason: str
    data: Dict[str, Any] = field(default_factory=dict)

    def as_log(self) -> Dict[str, Any]:
        return {"passed": self.passed, "reason": self.reason, **self.data}


@dataclass(frozen=True)
class HandoffPlan:
    """Transfert d'inventaire au TrailingStopAgent (§6.1 étape 2).

    Émis uniquement quand la cassure est ALIGNÉE avec le biais 1d. Une cassure à
    contre-biais est un candidat statistique au faux breakout : le §6.1 impose
    alors le flatten complet, et ce type n'est pas produit.

    Une fois le plan émis, `GridAgent` n'a plus aucun droit sur la position :
    elle vit sous les règles du moteur de tendance (trailing ATR, invalidation
    de biais). C'est pourquoi il n'existe aucun chemin de retour.
    """

    side: Side                   # sens de la position transférée
    size: float                  # après plafonnement à handoff_max_position_usd
    entry_price: float           # prix moyen de l'inventaire transféré
    stop_price: float            # borne cassée ∓ handoff_stop_k_atr × ATR_1h
    excess_size: float = 0.0     # excédent débouclé en maker, jamais transféré
    broken_bound: float = 0.0
    atr_1h: float = 0.0

    def as_log(self) -> Dict[str, Any]:
        return {
            "side": self.side.name, "size": self.size,
            "entry_price": self.entry_price, "stop_price": self.stop_price,
            "excess_size": self.excess_size, "broken_bound": self.broken_bound,
        }


@dataclass
class Inventory:
    """Inventaire net et prix moyen, alimentés par les fills.

    Le prix moyen suit une convention de compensation : un fill qui RÉDUIT
    l'inventaire réalise du PnL et ne change pas le prix moyen ; un fill qui
    l'augmente déplace la moyenne. C'est ce qui permet au §7 de séparer
    proprement `realized_grid_pnl` et `inventory_mtm` — les confondre est
    exactement l'erreur comptable que le §7 cherche à empêcher.
    """

    size: float = 0.0            # signé : > 0 long, < 0 short
    avg_price: float = 0.0
    realized: float = 0.0        # PnL brut réalisé par compensation (hors frais)

    @property
    def is_flat(self) -> bool:
        return abs(self.size) < 1e-12

    def apply(self, fill: Fill) -> float:
        """Applique un fill. Rend le PnL brut réalisé par ce fill."""
        qty = fill.signed_size
        realized = 0.0

        if self.is_flat or (self.size > 0) == (qty > 0):
            # Ouverture ou renforcement : la moyenne se déplace.
            total = self.size + qty
            if abs(total) > 1e-12:
                self.avg_price = (self.avg_price * self.size + fill.price * qty) / total
            self.size = total
        else:
            # Compensation : on réalise, à concurrence de l'inventaire existant.
            closing = min(abs(qty), abs(self.size))
            direction = 1.0 if self.size > 0 else -1.0
            realized = direction * (fill.price - self.avg_price) * closing
            self.realized += realized
            self.size += qty
            if self.is_flat:
                self.size, self.avg_price = 0.0, 0.0
            elif (self.size > 0) != (direction > 0):
                # Le fill a retourné l'inventaire : le reliquat s'ouvre au prix
                # du fill.
                self.avg_price = fill.price
        return realized

    def mtm(self, price: float) -> float:
        if self.is_flat:
            return 0.0
        return (price - self.avg_price) * self.size


@dataclass
class GridState:
    """État d'une session de grille, persistable (§12 : restart en cours de
    session recharge grille, inventaire, cooldowns)."""

    levels: List[GridLevel] = field(default_factory=list)
    filled: Dict[int, bool] = field(default_factory=dict)
    inventory: Inventory = field(default_factory=Inventory)
    lower: float = 0.0
    upper: float = 0.0
    center: float = 0.0
    step: float = 0.0
    atr_1h: float = 0.0
    started_ms: int = 0
    fees_paid: float = 0.0
    funding_paid: float = 0.0
    peak_equity: float = 0.0
    stopped: Optional[StopReason] = None

    def to_json(self) -> Dict[str, Any]:
        return {
            "levels": [{"price": lv.price, "side": lv.side.name, "size": lv.size,
                        "paired_price": lv.paired_price, "index": lv.index}
                       for lv in self.levels],
            "filled": {str(k): v for k, v in self.filled.items()},
            "inventory": {"size": self.inventory.size,
                          "avg_price": self.inventory.avg_price,
                          "realized": self.inventory.realized},
            "lower": self.lower, "upper": self.upper, "center": self.center,
            "step": self.step, "atr_1h": self.atr_1h, "started_ms": self.started_ms,
            "fees_paid": self.fees_paid, "funding_paid": self.funding_paid,
            "peak_equity": self.peak_equity,
            "stopped": self.stopped.value if self.stopped else None,
        }

    @classmethod
    def from_json(cls, raw: Dict[str, Any]) -> "GridState":
        state = cls(
            levels=[GridLevel(price=lv["price"], side=Side[lv["side"]], size=lv["size"],
                              paired_price=lv["paired_price"], index=lv["index"])
                    for lv in raw.get("levels", [])],
            filled={int(k): bool(v) for k, v in (raw.get("filled") or {}).items()},
            lower=float(raw.get("lower", 0.0)), upper=float(raw.get("upper", 0.0)),
            center=float(raw.get("center", 0.0)), step=float(raw.get("step", 0.0)),
            atr_1h=float(raw.get("atr_1h", 0.0)),
            started_ms=int(raw.get("started_ms", 0)),
            fees_paid=float(raw.get("fees_paid", 0.0)),
            funding_paid=float(raw.get("funding_paid", 0.0)),
            peak_equity=float(raw.get("peak_equity", 0.0)),
        )
        inv = raw.get("inventory") or {}
        state.inventory = Inventory(size=float(inv.get("size", 0.0)),
                                    avg_price=float(inv.get("avg_price", 0.0)),
                                    realized=float(inv.get("realized", 0.0)))
        stopped = raw.get("stopped")
        state.stopped = StopReason(stopped) if stopped else None
        return state


__all__ = ["ActivationVerdict", "Fill", "GridLevel", "GridState", "HandoffPlan",
           "Inventory", "Side", "StopReason"]

"""
Contrat de signal — SPEC §5.

Les dataclasses sont figées : un signal ou un verdict, une fois émis, décrit une
bougie clôturée et ne doit plus jamais changer. C'est la moitié de la garantie
anti-repaint (l'autre moitié est dans `indicators.closed()`).

Écart assumé par rapport à la lettre du §5 : `LayerVerdict` porte un champ
supplémentaire `data`. La spec impose passed/reason/computed_at (tous présents),
mais une couche doit aussi transmettre ce qu'elle a calculé — le biais courant,
le régime, l'ATR — sans quoi l'orchestrateur devrait recalculer les indicateurs
lui-même et la pureté du `evaluate()` ne servirait à rien.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Mapping, Tuple


class Bias(Enum):
    LONG_ONLY = 1
    SHORT_ONLY = -1
    FLAT = 0


class Regime(Enum):
    TREND = "trend"
    RANGE = "range"
    CHOP = "chop"


class Side(Enum):
    LONG = 1
    SHORT = -1

    @property
    def sign(self) -> int:
        return self.value

    @property
    def opposite(self) -> "Side":
        return Side.SHORT if self is Side.LONG else Side.LONG


class RiskLevel(Enum):
    """Niveau de risque macro on-chain (cf. MacroRegimeAgent, §2).

    UNKNOWN est le défaut quand aucune donnée on-chain n'est disponible : il ne
    déclenche PAS de veto mais ne donne pas non plus de feu vert — le biais 1d
    décide seul. Seul EXTREME force FLAT (§4.1).
    """

    UNKNOWN = "unknown"
    LOW = "low"
    NORMAL = "normal"
    ELEVATED = "elevated"
    EXTREME = "extreme"


@dataclass(frozen=True)
class LayerVerdict:
    passed: bool
    reason: str                       # obligatoire même si passed=True (traçabilité)
    computed_at: datetime             # timestamp de la bougie clôturée utilisée
    data: Mapping[str, Any] = field(default_factory=dict)

    def as_log(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "reason": self.reason,
            "computed_at": self.computed_at.isoformat(),
            **{k: _jsonable(v) for k, v in self.data.items()},
        }


@dataclass(frozen=True)
class ConfluenceSignal:
    side: Side
    entry_zone: Tuple[float, float]   # zone limite (min, max)
    stop_price: float                 # ATR-based, cf. §6
    atr_1h: float
    verdicts: Dict[str, LayerVerdict]  # clés: "1d", "1h", "15m"
    expires_at: datetime              # périmé après N bougies 15m (défaut 2)

    # Champs de traçabilité, hors contrat strict §5 mais indispensables au
    # backtest : sans le prix de référence, impossible de rejouer un fill.
    entry_ref: float = 0.0
    bar_ts: int = 0                   # ts (ms) de la bougie 15m de déclenchement

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def as_log(self) -> Dict[str, Any]:
        return {
            "side": self.side.name,
            "entry_zone": list(self.entry_zone),
            "entry_ref": self.entry_ref,
            "stop_price": self.stop_price,
            "atr_1h": self.atr_1h,
            "expires_at": self.expires_at.isoformat(),
            "bar_ts": self.bar_ts,
            "verdicts": {k: v.as_log() for k, v in self.verdicts.items()},
        }


def utc(ts_ms: int) -> datetime:
    """ts Hyperliquid (ms) → datetime UTC. Une seule conversion dans tout le
    module, pour qu'aucun timestamp naïf ne circule."""
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def veto(reason: str, at: datetime, **data: Any) -> LayerVerdict:
    return LayerVerdict(passed=False, reason=reason, computed_at=at, data=data)


def ok(reason: str, at: datetime, **data: Any) -> LayerVerdict:
    return LayerVerdict(passed=True, reason=reason, computed_at=at, data=data)


def _jsonable(v: Any) -> Any:
    if isinstance(v, Enum):
        return v.name if isinstance(v, (Bias, Side)) else v.value
    if isinstance(v, datetime):
        return v.isoformat()
    if hasattr(v, "to_json"):
        # Les états (BiasState…) savent se sérialiser. Sans ce cas, ils
        # retombaient sur le `default=str` de json.dumps et le log contenait
        # un repr Python — illisible pour l'outil qui relira ces lignes.
        return _jsonable(v.to_json())
    if isinstance(v, float):
        return round(v, 8)
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


__all__ = [
    "Bias", "ConfluenceSignal", "LayerVerdict", "Regime", "RiskLevel", "Side",
    "ms", "ok", "utc", "veto",
]

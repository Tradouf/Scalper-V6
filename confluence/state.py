"""
État persistant — SPEC §8 (« un restart ne doit pas réinitialiser les
garde-fous ») et §6.5.

Séparation stricte, c'est tout l'intérêt du fichier :

* les **structures** (`BiasState`, `GuardState`, `AgentState`) sont des
  dataclasses pures, sérialisables, sans I/O — les couches les manipulent
  librement et restent testables ;
* le **magasin** (`StateStore`) fait seul l'I/O disque, en écriture atomique.

Le mode d'échec qu'on évite ici est concret : un bot qui a pris ses 3 trades du
jour, redémarre, et en reprend 3 de plus parce que le compteur vivait en
mémoire. Sur une stratégie dont le diagnostic de départ est « les frais font
64 % des pertes », un garde-fou qui s'oublie au restart est pire qu'absent —
il donne l'illusion d'une limite.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from confluence.types import Bias

logger = logging.getLogger("sdm.confluence.state")

STATE_VERSION = 1
DEFAULT_DIR = Path(__file__).resolve().parent / "state"


# ── Structures pures ─────────────────────────────────────────────────────────

@dataclass
class BiasState:
    """Biais 1d courant et son hystérésis (§4.1).

    `pending`/`pending_count` matérialisent « 2 clôtures daily consécutives ».
    `last_bar_ts` est la clé d'idempotence : rejouer la même bougie daily ne
    doit pas faire avancer le compteur une seconde fois (§8).
    """

    current: Bias = Bias.FLAT
    pending: Optional[Bias] = None
    pending_count: int = 0
    last_bar_ts: int = 0

    def to_json(self) -> Dict[str, Any]:
        return {
            "current": self.current.name,
            "pending": self.pending.name if self.pending else None,
            "pending_count": self.pending_count,
            "last_bar_ts": self.last_bar_ts,
        }

    @classmethod
    def from_json(cls, raw: Dict[str, Any]) -> "BiasState":
        return cls(
            current=Bias[raw.get("current", "FLAT")],
            pending=Bias[raw["pending"]] if raw.get("pending") else None,
            pending_count=int(raw.get("pending_count", 0)),
            last_bar_ts=int(raw.get("last_bar_ts", 0)),
        )


@dataclass
class ClosedTrade:
    """Trade clôturé, tel que le kill-switch frais (§6.5) en a besoin.

    `gross_pnl` est le PnL AVANT frais : le ratio du §6.5 est
    `fees_paid / gross_pnl_abs`, donc mélanger les deux le rendrait faux (et
    flatteur, puisque les frais réduisent le dénominateur).
    """

    closed_ms: int
    gross_pnl: float
    fees: float
    funding: float = 0.0
    side: str = ""
    reason: str = ""

    @property
    def net_pnl(self) -> float:
        """Le funding compte dans le net mais PAS dans les frais.

        Il pèse pourtant lourd sur une position tenue plusieurs jours. Le
        confondre avec les frais de transaction fausserait le ratio du §6.5,
        dont tout l'objet est de mesurer le coût du CHURN — un funding élevé
        n'est pas un symptôme d'over-trading, c'est même l'inverse.
        """
        return self.gross_pnl - self.fees - self.funding

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: Dict[str, Any]) -> "ClosedTrade":
        return cls(
            closed_ms=int(raw["closed_ms"]),
            gross_pnl=float(raw.get("gross_pnl", 0.0)),
            fees=float(raw.get("fees", 0.0)),
            funding=float(raw.get("funding", 0.0)),
            side=str(raw.get("side", "")),
            reason=str(raw.get("reason", "")),
        )


@dataclass
class GuardState:
    """Compteurs anti-overtrading (§6.5), tous persistés."""

    day_key: str = ""                       # "YYYY-MM-DD" UTC du compteur courant
    trades_today: int = 0
    last_entry_ms: int = 0
    last_loss_ms: int = 0
    history: List[ClosedTrade] = field(default_factory=list)
    killswitch_alerted_ms: int = 0
    seen_entry_bars: List[int] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {
            "day_key": self.day_key,
            "trades_today": self.trades_today,
            "last_entry_ms": self.last_entry_ms,
            "last_loss_ms": self.last_loss_ms,
            "history": [t.to_json() for t in self.history],
            "killswitch_alerted_ms": self.killswitch_alerted_ms,
            "seen_entry_bars": list(self.seen_entry_bars),
        }

    @classmethod
    def from_json(cls, raw: Dict[str, Any]) -> "GuardState":
        return cls(
            day_key=str(raw.get("day_key", "")),
            trades_today=int(raw.get("trades_today", 0)),
            last_entry_ms=int(raw.get("last_entry_ms", 0)),
            last_loss_ms=int(raw.get("last_loss_ms", 0)),
            history=[ClosedTrade.from_json(t) for t in raw.get("history", [])],
            killswitch_alerted_ms=int(raw.get("killswitch_alerted_ms", 0)),
            seen_entry_bars=[int(b) for b in raw.get("seen_entry_bars", [])],
        )

    # -- opérations --

    def roll_day(self, now_ms: int) -> None:
        """Remet le compteur journalier à zéro au changement de jour UTC.

        Appelé avant toute lecture de `trades_today` : sans ça, un bot resté
        allumé 48 h lirait le compteur de l'avant-veille.
        """
        key = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
        if key != self.day_key:
            self.day_key = key
            self.trades_today = 0

    def prune(self, now_ms: int, keep_days: int) -> None:
        """Purge l'historique au-delà de la fenêtre du kill-switch.

        Sans purge le fichier d'état grossit indéfiniment — exactement le
        problème constaté sur `memory/shared_memory.json` (838 Ko et jamais
        élagué, cf. CLAUDE.md).
        """
        cutoff = now_ms - keep_days * 86_400_000
        self.history = [t for t in self.history if t.closed_ms >= cutoff]
        # Les barres déjà traitées ne servent qu'à l'idempotence intra-journée.
        bar_cutoff = now_ms - 7 * 86_400_000
        self.seen_entry_bars = sorted(b for b in self.seen_entry_bars if b >= bar_cutoff)


@dataclass
class AgentState:
    version: int = STATE_VERSION
    bias: BiasState = field(default_factory=BiasState)
    guards: GuardState = field(default_factory=GuardState)
    last_eval_bar_ts: int = 0               # dernière bougie 15m évaluée (idempotence §8)
    open_position: Optional[Dict[str, Any]] = None

    def to_json(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "bias": self.bias.to_json(),
            "guards": self.guards.to_json(),
            "last_eval_bar_ts": self.last_eval_bar_ts,
            "open_position": self.open_position,
        }

    @classmethod
    def from_json(cls, raw: Dict[str, Any]) -> "AgentState":
        return cls(
            version=int(raw.get("version", STATE_VERSION)),
            bias=BiasState.from_json(raw.get("bias", {})),
            guards=GuardState.from_json(raw.get("guards", {})),
            last_eval_bar_ts=int(raw.get("last_eval_bar_ts", 0)),
            open_position=raw.get("open_position"),
        )


# ── Magasin disque ───────────────────────────────────────────────────────────

class StateStore:
    """Persistance JSON atomique (§8).

    Écriture par fichier temporaire + `os.replace` : une coupure de courant en
    plein `write()` laisserait sinon un JSON tronqué, donc un état illisible au
    redémarrage — et un bot qui repart avec des garde-fous vierges.
    """

    def __init__(self, path: Optional[Path] = None, symbol: str = "BTC") -> None:
        base = Path(os.environ.get("CONFLUENCE_STATE_DIR") or DEFAULT_DIR)
        self.path = Path(path) if path else base / f"{symbol.lower()}_state.json"

    def load(self) -> AgentState:
        if not self.path.exists():
            return AgentState()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # On NE repart PAS sur un état vierge en silence : perdre les
            # compteurs est précisément ce que §8 interdit. On archive et on
            # crie, l'appelant décidera.
            backup = self.path.with_suffix(f".corrupt.{int(datetime.now(timezone.utc).timestamp())}")
            try:
                self.path.rename(backup)
            except OSError:
                pass
            logger.error("état illisible (%s) — archivé dans %s, garde-fous réinitialisés",
                         exc, backup)
            return AgentState()
        if int(raw.get("version", 0)) != STATE_VERSION:
            logger.warning("état en version %s, attendu %s — relecture best-effort",
                           raw.get("version"), STATE_VERSION)
        return AgentState.from_json(raw)

    def save(self, state: AgentState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state.to_json(), ensure_ascii=False, indent=2, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def day_key(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def hours_since(now_ms: int, then_ms: int) -> float:
    if then_ms <= 0:
        return float("inf")
    return (now_ms - then_ms) / 3_600_000.0


__all__ = [
    "AgentState", "BiasState", "ClosedTrade", "GuardState", "StateStore",
    "STATE_VERSION", "day_key", "hours_since",
]

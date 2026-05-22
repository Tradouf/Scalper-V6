"""
OrderRegistry — registre d'ownership des OIDs Hyperliquid.

Tout ordre placé par le bot (limit, trigger, TP/SL natif, recovery, etc.) doit
être enregistré ici avec sa source et son intent. Permet :
  - de distinguer un SL trail d'un TP grid d'un trigger orphelin
  - de reconcilier au boot avec frontend_open_orders pour détecter les OIDs
    inconnus (positions ou ordres survivants d'une session précédente)
  - de tracer le cancel+recreate (modify) sans confusion d'OID
  - d'éviter le fallback de résolution OID qui pourrissait le trail

Persistence : memory/order_registry.json (auto-saved après chaque mutation).
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("sdm.order_registry")

# Sources canoniques. Tout register() doit utiliser une de ces valeurs.
SOURCE_GRID_PENDING = "grid_pending"   # ordre limit grid en attente de fill
SOURCE_GRID_TP = "grid_tp"             # TP limit reduce_only après fill grid
SOURCE_SCALP_SL = "scalp_sl"           # SL natif HL posé par le scalp/trail
SOURCE_SCALP_TP = "scalp_tp"           # TP natif HL posé par le scalp
SOURCE_RECOVERY = "recovery"           # SL posé par _health_check / _recover_or_place_sl
SOURCE_UNKNOWN = "unknown"             # détecté côté HL mais sans origine claire

VALID_SOURCES = {
    SOURCE_GRID_PENDING,
    SOURCE_GRID_TP,
    SOURCE_SCALP_SL,
    SOURCE_SCALP_TP,
    SOURCE_RECOVERY,
    SOURCE_UNKNOWN,
}


@dataclass
class OrderRecord:
    oid: int
    source: str
    symbol: str
    intent: str          # "open" | "tp" | "sl" | "close"
    side: str            # "buy" | "sell"
    is_trigger: bool
    reduce_only: bool
    qty: float
    price: float
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    meta: Dict[str, Any] = field(default_factory=dict)


class OrderRegistry:
    def __init__(self, file_path: Path):
        self._path = Path(file_path)
        self._lock = threading.RLock()
        self._records: Dict[int, OrderRecord] = {}
        self._load()

    # ─── persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            for raw in data.get("records", []):
                rec = OrderRecord(**raw)
                self._records[rec.oid] = rec
            logger.info("OrderRegistry: %d records chargés depuis %s", len(self._records), self._path)
        except Exception as e:
            logger.warning("OrderRegistry: échec chargement %s: %r", self._path, e)

    def _save_locked(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            payload = {"records": [asdict(r) for r in self._records.values()]}
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
            tmp.replace(self._path)
        except Exception as e:
            logger.warning("OrderRegistry: échec sauvegarde: %r", e)

    # ─── API publique ─────────────────────────────────────────────────────────

    def register(
        self,
        oid: int,
        source: str,
        symbol: str,
        intent: str,
        side: str,
        is_trigger: bool,
        reduce_only: bool,
        qty: float,
        price: float,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        if source not in VALID_SOURCES:
            logger.warning("OrderRegistry.register: source invalide %r (oid=%s)", source, oid)
            source = SOURCE_UNKNOWN
        try:
            oid_int = int(oid)
        except Exception:
            logger.warning("OrderRegistry.register: oid invalide %r", oid)
            return
        with self._lock:
            self._records[oid_int] = OrderRecord(
                oid=oid_int,
                source=source,
                symbol=str(symbol).upper(),
                intent=str(intent),
                side=str(side).lower(),
                is_trigger=bool(is_trigger),
                reduce_only=bool(reduce_only),
                qty=float(qty),
                price=float(price),
                meta=dict(meta or {}),
            )
            self._save_locked()

    def lookup(self, oid: int) -> Optional[OrderRecord]:
        try:
            oid_int = int(oid)
        except Exception:
            return None
        with self._lock:
            rec = self._records.get(oid_int)
            return OrderRecord(**asdict(rec)) if rec else None

    def by_symbol(self, symbol: str) -> List[OrderRecord]:
        sym = str(symbol).upper()
        with self._lock:
            return [OrderRecord(**asdict(r)) for r in self._records.values() if r.symbol == sym]

    def by_source(self, source: str) -> List[OrderRecord]:
        with self._lock:
            return [OrderRecord(**asdict(r)) for r in self._records.values() if r.source == source]

    def unregister(self, oid: int) -> Optional[OrderRecord]:
        try:
            oid_int = int(oid)
        except Exception:
            return None
        with self._lock:
            rec = self._records.pop(oid_int, None)
            if rec is not None:
                self._save_locked()
            return rec

    def update_oid(self, old_oid: int, new_oid: int) -> bool:
        """Pour le cancel+recreate de HL modify. Préserve source/intent."""
        try:
            old_i = int(old_oid)
            new_i = int(new_oid)
        except Exception:
            return False
        if old_i == new_i:
            return True
        with self._lock:
            rec = self._records.pop(old_i, None)
            if rec is None:
                logger.debug("OrderRegistry.update_oid: old_oid %s inconnu", old_i)
                return False
            rec.oid = new_i
            rec.updated_at = time.time()
            self._records[new_i] = rec
            self._save_locked()
            return True

    def all(self) -> List[OrderRecord]:
        with self._lock:
            return [OrderRecord(**asdict(r)) for r in self._records.values()]

    def reconcile(self, live_oids: Set[int]) -> Tuple[Set[int], Set[int]]:
        """Compare le registre avec les OIDs vivants côté HL.

        Returns:
            (ghost, orphan)
                ghost  = OIDs dans le registre mais absents côté HL → à purger
                orphan = OIDs vivants côté HL mais absents du registre → source UNKNOWN à enquêter
        """
        with self._lock:
            registered = set(self._records.keys())
            ghost = registered - live_oids
            orphan = live_oids - registered
            return ghost, orphan

    def purge_ghosts(self, live_oids: Set[int]) -> int:
        """Supprime du registre les OIDs absents côté HL. Retourne le nombre purgé."""
        ghost, _ = self.reconcile(live_oids)
        if not ghost:
            return 0
        with self._lock:
            for oid in ghost:
                self._records.pop(oid, None)
            self._save_locked()
        logger.info("OrderRegistry: purgé %d ghosts (ordres absents HL)", len(ghost))
        return len(ghost)

    def absorb_orphans(self, live_orders: List[Dict[str, Any]]) -> int:
        """Pour chaque OID vivant côté HL non présent dans le registre, le tag UNKNOWN.

        live_orders : liste de dicts façon frontend_open_orders (chaque dict contient
            oid, coin, side, sz, triggerPx|limitPx, isTrigger, reduceOnly, tpsl, ...).
        """
        absorbed = 0
        with self._lock:
            for o in live_orders:
                try:
                    oid = int(o.get("oid"))
                except Exception:
                    continue
                if oid in self._records:
                    continue
                is_trigger = bool(o.get("isTrigger", False))
                tpsl = str(o.get("tpsl") or "").lower()
                if tpsl == "tp":
                    intent = "tp"
                elif tpsl == "sl":
                    intent = "sl"
                elif is_trigger:
                    intent = "trigger"
                else:
                    intent = "limit"
                side_raw = str(o.get("side", "")).upper()
                side = "buy" if side_raw in ("B", "BUY") else ("sell" if side_raw in ("A", "SELL") else "?")
                price = float(o.get("triggerPx") or o.get("limitPx") or 0)
                qty = float(o.get("sz") or 0)
                self._records[oid] = OrderRecord(
                    oid=oid,
                    source=SOURCE_UNKNOWN,
                    symbol=str(o.get("coin", "")).upper(),
                    intent=intent,
                    side=side,
                    is_trigger=is_trigger,
                    reduce_only=bool(o.get("reduceOnly", False)),
                    qty=qty,
                    price=price,
                    meta={"absorbed_at_boot": True},
                )
                absorbed += 1
            if absorbed:
                self._save_locked()
        if absorbed:
            logger.warning("OrderRegistry: absorbé %d orphelins (source=unknown)", absorbed)
        return absorbed

    def stats(self) -> Dict[str, int]:
        with self._lock:
            counts: Dict[str, int] = {}
            for r in self._records.values():
                counts[r.source] = counts.get(r.source, 0) + 1
            counts["_total"] = len(self._records)
            return counts


# ─── singleton ────────────────────────────────────────────────────────────────

_REGISTRY: Optional[OrderRegistry] = None
_REGISTRY_LOCK = threading.Lock()

DEFAULT_PATH = Path(__file__).parent / "order_registry.json"


def get_order_registry(path: Optional[Path] = None) -> OrderRegistry:
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = OrderRegistry(path or DEFAULT_PATH)
        return _REGISTRY

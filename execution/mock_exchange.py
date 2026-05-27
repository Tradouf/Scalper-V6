"""
MockExchange — implémentation minimale d'ExchangeClient pour les tests et le
backtester. Suit l'interface utilisée par GridEngine (place_order, cancel_order,
get_open_orders, get_mark_price + .info.meta()).

Comportement :
  - place_order : retourne un order_id séquentiel, accepté
  - cancel_order : retourne success=True si l'OID est connu
  - tient un état interne d'OIDs ouverts pour pouvoir simuler des fills
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from execution.types import CancelResult, OrderRequest, OrderResult


@dataclass
class MockOrder:
    oid: int
    coin: str
    side: str   # 'B' ou 'A' (HL convention)
    sz: float
    limit_px: Optional[float]
    trigger_px: Optional[float]
    is_trigger: bool
    reduce_only: bool


class MockMeta:
    """Mock du sous-attribut .info.meta() utilisé par GridEngine._get_tick_decimals."""

    def __init__(self, universe: list[dict]) -> None:
        self._universe = universe

    def meta(self) -> dict:
        return {"universe": self._universe}


class MockClient:
    """Mock du sous-attribut self._exchange._client.info utilisé par GridEngine."""

    def __init__(self, universe: list[dict]) -> None:
        self.info = MockMeta(universe)


class MockExchange:
    """Implémentation minimale d'un exchange pour tester GridEngine isolément.

    Note : GridEngine accède à self._exchange._client.info.meta() pour les tick
    decimals. On expose cette chaîne d'attributs via MockClient.
    """

    def __init__(self, universe: Optional[list[dict]] = None, mark_prices: Optional[Dict[str, float]] = None) -> None:
        if universe is None:
            universe = [
                {"name": "BTC", "szDecimals": 5},
                {"name": "ETH", "szDecimals": 4},
                {"name": "SOL", "szDecimals": 3},
                {"name": "BNB", "szDecimals": 3},
                {"name": "AAVE", "szDecimals": 3},
                {"name": "LINK", "szDecimals": 4},
                {"name": "SUI", "szDecimals": 5},
                {"name": "BCH", "szDecimals": 2},
                {"name": "DOGE", "szDecimals": 0},
            ]
        self._client = MockClient(universe)
        self._mark_prices: Dict[str, float] = mark_prices or {}
        self._open_orders: Dict[int, MockOrder] = {}
        self._next_oid = itertools.count(start=10_000_000)
        self.placed_orders: List[OrderRequest] = []
        self.cancelled_oids: List[int] = []

    # ─── API utilisée par GridEngine ──────────────────────────────────────────

    def place_order(self, req: OrderRequest) -> OrderResult:
        oid = next(self._next_oid)
        self.placed_orders.append(req)
        # Stocke pour pouvoir filtrer via get_open_orders ensuite
        side_hl = "B" if req.side == "buy" else "A"
        self._open_orders[oid] = MockOrder(
            oid=oid,
            coin=req.symbol,
            side=side_hl,
            sz=req.qty,
            limit_px=req.price if req.order_type == "limit" else None,
            trigger_px=None,
            is_trigger=False,
            reduce_only=req.reduce_only,
        )
        return OrderResult(
            order_id=str(oid),
            symbol=req.symbol,
            side=req.side,
            qty=req.qty,
            price=req.price,
            status="accepted",
        )

    def cancel_order(self, order_id: str) -> CancelResult:
        try:
            oid = int(order_id)
        except (ValueError, TypeError):
            return CancelResult(order_id=str(order_id), success=False)
        if oid in self._open_orders:
            del self._open_orders[oid]
            self.cancelled_oids.append(oid)
            return CancelResult(order_id=str(oid), success=True)
        return CancelResult(order_id=str(oid), success=False)

    def get_open_orders(self, coin: Optional[str] = None) -> list[dict]:
        out = []
        for o in self._open_orders.values():
            if coin is not None and o.coin != coin:
                continue
            out.append({
                "coin": o.coin,
                "oid": o.oid,
                "side": o.side,
                "sz": o.sz,
                "limit_px": o.limit_px,
                "limitPx": o.limit_px,
                "triggerPx": str(o.trigger_px) if o.trigger_px is not None else "0.0",
                "isTrigger": o.is_trigger,
                "reduceOnly": o.reduce_only,
                "tpsl": "",
                "orderType": "Limit",
            })
        return out

    def get_mark_price(self, coin: str) -> float:
        return self._mark_prices.get(coin, 0.0)

    # ─── Helpers pour tests : simuler un fill ────────────────────────────────

    def simulate_fill(self, oid: int) -> Optional[MockOrder]:
        """Retire un order du book (= simule son exécution complète)."""
        return self._open_orders.pop(oid, None)

    def open_oids(self) -> set:
        return set(self._open_orders.keys())

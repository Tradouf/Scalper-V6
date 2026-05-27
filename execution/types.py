"""
Types d'exécution V7 (port simplifié depuis V6 exchanges/base.py).

OrderRequest/OrderResult/CancelResult sont les structures échangées entre
l'Execution Engine et les implémentations concrètes (hyperliquid_adapter,
paper). Le Protocol ExchangeClient définit le contrat minimal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol


JsonDict = Dict[str, Any]


@dataclass
class OrderRequest:
    """Description d'un ordre à envoyer à un exchange."""

    symbol: str
    side: str               # "buy" ou "sell"
    qty: float              # taille positive en unités du sous-jacent
    order_type: str = "limit"  # "limit" ou "market"
    price: Optional[float] = None
    leverage: Optional[float] = None
    reduce_only: bool = False
    client_id: Optional[str] = None
    strategy_id: Optional[str] = None  # NEW V7 : pour attribution PnL


@dataclass
class OrderResult:
    order_id: str
    symbol: str
    side: str
    qty: float
    price: Optional[float]
    status: str             # "accepted" | "rejected" | "filled"
    raw: JsonDict = field(default_factory=dict)


@dataclass
class CancelResult:
    order_id: str
    success: bool
    raw: JsonDict = field(default_factory=dict)


class ExchangeClient(Protocol):
    """Contrat minimal pour un client d'échange (live ou paper)."""

    def place_order(self, req: OrderRequest) -> OrderResult: ...

    def cancel_order(self, order_id: str) -> CancelResult: ...

    def get_open_orders(self, coin: Optional[str] = None) -> list[dict]: ...

    def get_mark_price(self, coin: str) -> float: ...

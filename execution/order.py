"""
Order concret — un ordre prêt à soumettre. Implémente le Protocol Order.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class OrderImpl:
    """Order prêt à soumettre à l'exchange via ExchangeClient.place_order.

    Convention : `qty` est toujours positif. La direction est portée par `side`.
    `strategy_id` permet l'attribution PnL via le Fill retourné.
    """

    asset: str
    side: str               # 'buy' | 'sell'
    qty: float
    order_type: str = "market"     # 'market' | 'limit'
    price: Optional[float] = None  # None si market
    reduce_only: bool = False
    strategy_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError(f"Order qty doit être > 0 (signe via side), got {self.qty}")
        if self.side not in ("buy", "sell"):
            raise ValueError(f"Order side invalide : {self.side}")
        if self.order_type == "limit" and self.price is None:
            raise ValueError("Order limit requires price")

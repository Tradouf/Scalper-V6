"""
HyperliquidWriteAdapter — exchange write side pour V7 live.

Pour le cutover V6 → V7 (P8). Wrappe l'`HyperliquidExchangeClient` V6
existant (battle-tested, gère IOC/GTC, marketable limit, oid extraction)
et expose l'API V7 attendue par ExecutionEngine + grid_engine.

Surface ExchangeClient duck-typed exigée par V7 :
  - place_order(OrderRequest)        → OrderResult
  - cancel_order(order_id: str)      → CancelResult
  - get_open_orders(coin=None)       → list[dict]   (frontend_open_orders HL)
  - get_mark_price(coin: str)        → float
  - _client                          attribut (HyperliquidClient SDK)
                                     pour grid_engine ._client.info.meta()
                                     et ._client.get_user_state()

Les types V6 (exchanges.base.OrderRequest|OrderResult|CancelResult) et V7
(execution.types) ont la MÊME structure côté champs utilisés (le surplus
strategy_id de V7 n'est pas consulté par HL). Le passage est donc duck-typé,
sans conversion explicite — `place_order` accepte indifféremment les deux
dataclasses, le résultat est duck-compatible côté caller V7.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from exchanges.hyperliquid import HyperliquidExchangeClient
from execution.types import CancelResult, OrderRequest, OrderResult

logger = logging.getLogger("v7.hl_write")


class HyperliquidWriteAdapter:
    """Adapter live HL pour V7. Wrappe HyperliquidExchangeClient V6.

    Args:
        enable_trading : passe à False pour la phase de dry-run / vérif config.
    """

    def __init__(self, enable_trading: bool = True) -> None:
        self._inner = HyperliquidExchangeClient(enable_trading=enable_trading)
        # Expose le SDK client brut pour les besoins de grid_engine (meta, user_state).
        self._client = self._inner._client
        logger.info(
            "HyperliquidWriteAdapter init enable_trading=%s network=%s",
            enable_trading,
            getattr(self._client, "_network", "?"),
        )

    # ─── ExchangeClient Protocol ─────────────────────────────────────────────

    def place_order(self, req: OrderRequest) -> OrderResult:
        """Délègue à HyperliquidExchangeClient. Le résultat V6 est duck-compatible
        avec OrderResult V7 (mêmes attributs : order_id, status, price, ...).

        Critique pour Fix 7 V7 : HyperliquidExchangeClient renvoie déjà
        status='filled' + order_id='' quand un limit marketable est exécuté
        immédiatement par HL. grid_engine._place_limit voit alors PlaceResult
        status='filled' (cf. Fix 7 commit 0a10091).
        """
        return self._inner.place_order(req)  # type: ignore[return-value]

    def cancel_order(self, order_id: str) -> CancelResult:
        return self._inner.cancel_order(order_id)  # type: ignore[return-value]

    def get_open_orders(self, coin: Optional[str] = None) -> List[dict]:
        """Liste des ordres ouverts (frontend_open_orders HL). Format :
        [{coin, oid, side(B/A), sz, limitPx, triggerPx, isTrigger, reduceOnly, tpsl, orderType, ...}]
        """
        try:
            return self._client.get_open_orders(coin=coin) or []
        except Exception as e:
            logger.warning("get_open_orders error coin=%s: %r", coin, e)
            return []

    def get_mark_price(self, coin: str) -> float:
        """Mark price courant via /info type=ticker (déjà cached côté HyperliquidClient)."""
        try:
            ticker = self._client.get_ticker(coin)
            return float(ticker.get("price", 0) or 0)
        except Exception as e:
            logger.warning("get_mark_price %s error: %r", coin, e)
            return 0.0

    # ─── API étendue (parité ExecutionEngine si besoin) ───────────────────────

    def get_positions(self) -> list:
        """Positions HL courantes (typées V6 Position). Utile pour le boot reconciler."""
        return self._inner.get_positions()

    def get_balances(self) -> list:
        return self._inner.get_balances()

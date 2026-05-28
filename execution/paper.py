"""
PaperExchange — adaptateur paper trading.

Comportement :
  - Reçoit place_order() comme un vrai exchange
  - Simule un fill instantané au prix mark + slippage
  - Stocke un journal interne des ordres et fills
  - Ne touche jamais le vrai exchange (lecture seule pour les mark prices)

Le mark price est fourni par un callback (ex: lecture HL info en lecture seule)
ou par injection directe via update_mark_price() pour les tests.

PaperExchange satisfait le Protocol ExchangeClient pour pouvoir être passé
à ExecutionEngine.
"""
from __future__ import annotations

import datetime as dt
import itertools
import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from execution.types import CancelResult, OrderRequest, OrderResult

logger = logging.getLogger("v7.paper")


@dataclass
class PaperFillRecord:
    """Journal interne d'un fill paper."""

    oid: int
    asset: str
    side: str
    qty: float
    fill_price: float
    fee: float
    slippage: float
    strategy_id: Optional[str]
    ts: dt.datetime


class PaperExchange:
    """Exchange virtuel pour paper trading."""

    def __init__(
        self,
        get_mark_price: Optional[Callable[[str], float]] = None,
        maker_fee_bps: float = 1.5,
        taker_fee_bps: float = 4.5,
        slippage_bps: float = 2.0,
    ) -> None:
        self._get_mark_price = get_mark_price
        self._maker_fee_bps = maker_fee_bps
        self._taker_fee_bps = taker_fee_bps
        self._slippage_bps = slippage_bps
        # État
        self._mark_prices: Dict[str, float] = {}
        self._oid_counter = itertools.count(start=1_000_000)
        self._open_limit_orders: Dict[int, OrderRequest] = {}  # pour les limit non immédiatement filled
        self._fills_log: List[PaperFillRecord] = []
        # Positions paper : asset → notional signé courant
        self._paper_positions: Dict[str, float] = {}

    # ─── ExchangeClient Protocol ─────────────────────────────────────────────

    def place_order(self, req: OrderRequest) -> OrderResult:
        oid = next(self._oid_counter)
        mark = self._mark_price(req.symbol)
        if mark <= 0:
            return OrderResult(
                order_id=str(oid),
                symbol=req.symbol,
                side=req.side,
                qty=req.qty,
                price=None,
                status="rejected",
                raw={"reason": "no_mark_price"},
            )

        # Slippage
        slip_factor = self._slippage_bps / 10_000.0
        if req.order_type == "market":
            # Market : fill immédiat à mark × (1 ± slippage)
            fill_price = mark * (1 + slip_factor) if req.side == "buy" else mark * (1 - slip_factor)
            fee_bps = self._taker_fee_bps
            self._record_fill(oid, req, fill_price, fee_bps)
            return OrderResult(
                order_id=str(oid), symbol=req.symbol, side=req.side, qty=req.qty,
                price=fill_price, status="filled",
            )

        # Limit : si crossing (buy au-dessus du mark / sell en-dessous), fill immédiat
        # (Aloud ≠ post-only). Sinon, on stocke comme open.
        limit_px = float(req.price) if req.price else mark
        crossing = (req.side == "buy" and limit_px >= mark) or (req.side == "sell" and limit_px <= mark)
        if crossing:
            fill_price = limit_px  # on prend le prix de la limit
            fee_bps = self._maker_fee_bps
            self._record_fill(oid, req, fill_price, fee_bps)
            return OrderResult(
                order_id=str(oid), symbol=req.symbol, side=req.side, qty=req.qty,
                price=fill_price, status="filled",
            )
        # Stocke comme ordre ouvert (sera fillé via update_mark_price si croise)
        self._open_limit_orders[oid] = req
        return OrderResult(
            order_id=str(oid), symbol=req.symbol, side=req.side, qty=req.qty,
            price=limit_px, status="accepted",
        )

    def cancel_order(self, order_id: str) -> CancelResult:
        try:
            oid = int(order_id)
        except (ValueError, TypeError):
            return CancelResult(order_id=str(order_id), success=False)
        if oid in self._open_limit_orders:
            del self._open_limit_orders[oid]
            return CancelResult(order_id=str(oid), success=True)
        return CancelResult(order_id=str(oid), success=False)

    def get_open_orders(self, coin: Optional[str] = None) -> list[dict]:
        out = []
        for oid, req in self._open_limit_orders.items():
            if coin is not None and req.symbol != coin:
                continue
            side_hl = "B" if req.side == "buy" else "A"
            out.append({
                "coin": req.symbol, "oid": oid, "side": side_hl,
                "sz": req.qty, "limit_px": req.price, "limitPx": req.price,
                "triggerPx": "0.0", "isTrigger": False, "reduceOnly": req.reduce_only,
                "tpsl": "", "orderType": "Limit",
            })
        return out

    def get_mark_price(self, coin: str) -> float:
        return self._mark_price(coin)

    # ─── API paper-specific ───────────────────────────────────────────────────

    def update_mark_price(self, asset: str, price: float) -> None:
        """Met à jour le mark price (utilisé par le main loop ou tests).
        Side-effect : check si des limit orders ouverts crossent maintenant → fill.
        """
        if price <= 0:
            return
        self._mark_prices[asset] = float(price)
        # Check limit orders crossing
        to_fill: list[int] = []
        for oid, req in self._open_limit_orders.items():
            if req.symbol != asset:
                continue
            limit_px = float(req.price) if req.price else price
            if (req.side == "buy" and price <= limit_px) or (req.side == "sell" and price >= limit_px):
                to_fill.append(oid)
        for oid in to_fill:
            req = self._open_limit_orders.pop(oid)
            self._record_fill(oid, req, float(req.price), self._maker_fee_bps)

    def position_notional(self, asset: str) -> float:
        return self._paper_positions.get(asset, 0.0)

    def all_positions(self) -> Dict[str, float]:
        return dict(self._paper_positions)

    def fills(self) -> List[PaperFillRecord]:
        return list(self._fills_log)

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _mark_price(self, asset: str) -> float:
        cached = self._mark_prices.get(asset, 0.0)
        if cached > 0:
            return cached
        if self._get_mark_price is not None:
            try:
                p = float(self._get_mark_price(asset))
                if p > 0:
                    self._mark_prices[asset] = p
                    return p
            except Exception:
                pass
        return 0.0

    def _record_fill(self, oid: int, req: OrderRequest, fill_price: float, fee_bps: float) -> None:
        notional = req.qty * fill_price
        fee = notional * fee_bps / 10_000.0
        slippage = notional * self._slippage_bps / 10_000.0
        # Met à jour la position paper (signée)
        signed = notional * (1.0 if req.side == "buy" else -1.0)
        if req.reduce_only:
            # On clamp pour ne pas inverser la position via RO
            current = self._paper_positions.get(req.symbol, 0.0)
            if current > 0 and signed < 0:
                signed = max(signed, -current)  # ne descend pas sous 0
            elif current < 0 and signed > 0:
                signed = min(signed, -current)  # ne monte pas au-dessus de 0
            else:
                signed = 0.0  # RO sur position nulle = no-op
        new_n = self._paper_positions.get(req.symbol, 0.0) + signed
        if abs(new_n) < 1e-9:
            self._paper_positions.pop(req.symbol, None)
        else:
            self._paper_positions[req.symbol] = new_n
        self._fills_log.append(PaperFillRecord(
            oid=oid, asset=req.symbol, side=req.side, qty=req.qty,
            fill_price=fill_price, fee=fee, slippage=slippage,
            strategy_id=req.strategy_id, ts=dt.datetime.utcnow(),
        ))

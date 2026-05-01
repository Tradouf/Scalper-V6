"""
Client Hyperliquid conforme à l'interface ExchangeClient (base.py)
pour le projet SalleDesMarches.
"""

from __future__ import annotations

import json
import logging
import time
import requests
from pathlib import Path
from typing import Any, Dict, List, Optional

from exchanges.base import (
    Balance,
    CancelResult,
    ExchangeClient,
    JsonDict,
    OrderRequest,
    OrderResult,
    Position,
)

from hyperliquid_client import HyperliquidClient, HyperliquidClientError

logger = logging.getLogger("sdm.exchange.hyperliquid")

ROOT_DIR = Path(__file__).resolve().parent.parent
HL_CONFIG_PATH = ROOT_DIR / "hl_config.json"


def _load_network_from_config() -> str:
    if not HL_CONFIG_PATH.is_file():
        return "mainnet"
    try:
        with HL_CONFIG_PATH.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("network", "mainnet")
    except Exception:
        return "mainnet"


class HyperliquidExchangeClient(ExchangeClient):
    def __init__(self, enable_trading: bool = True) -> None:
        network = _load_network_from_config()
        use_testnet = network.lower() == "testnet"

        if enable_trading:
            self._client = HyperliquidClient(
                wallet_key=None,
                config_path=str(HL_CONFIG_PATH),
                use_testnet=use_testnet,
            )
        else:
            self._client = HyperliquidClient(
                wallet_key=None,
                config_path="__does_not_exist__.json",
                use_testnet=use_testnet,
            )

    def get_markets(self) -> List[JsonDict]:
        meta = self._client.get_meta()
        universe = meta.get("meta", {}).get("universe", [])
        ctxs = meta.get("asset_ctxs", [])
        markets: List[Dict[str, Any]] = []

        for i, u in enumerate(universe):
            coin = u.get("name")
            ctx = ctxs[i] if i < len(ctxs) else {}
            markets.append(
                {
                    "symbol": coin,
                    "base_asset": coin,
                    "quote_asset": "USDT",
                    "sz_decimals": u.get("szDecimals", 3),
                    "max_leverage": u.get("maxLeverage", 50),
                    "day_notional_volume": float(ctx.get("dayNtlVlm", 0)),
                    "open_interest": float(ctx.get("openInterest", 0)),
                }
            )
        return markets

    def get_orderbook(self, symbol: str, depth: int = 50) -> JsonDict:
        book = self._client.get_l2_snapshot(symbol)
        levels = book.get("levels", [[], []])
        bids = [
            [float(l.get("px", 0)), float(l.get("sz", 0))]
            for l in levels[0][:depth]
        ]
        asks = [
            [float(l.get("px", 0)), float(l.get("sz", 0))]
            for l in levels[1][:depth]
        ]
        return {"symbol": symbol, "bids": bids, "asks": asks, "time": book.get("time")}

    def get_trades(self, symbol: str, limit: int = 100) -> List[JsonDict]:
        return []

    def get_ohlcv(
        self,
        symbol: str,
        interval: str = "15m",
        days: int = 7,
    ) -> List[List[float]]:
        end_time = int(time.time() * 1000)
        start_time = end_time - days * 24 * 3600 * 1000

        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": symbol,
                "interval": interval,
                "startTime": start_time,
                "endTime": end_time,
            },
        }

        try:
            resp = requests.post(
                "https://api.hyperliquid.xyz/info",
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=10,
            )
            resp.raise_for_status()
            raw = resp.json()
            return [
                [
                    int(c["t"]),
                    float(c["o"]),
                    float(c["h"]),
                    float(c["l"]),
                    float(c["c"]),
                    float(c["v"]),
                ]
                for c in raw
            ]
        except Exception as e:
            logger.warning("[get_ohlcv] Erreur pour %s/%s: %r", symbol, interval, e)
            return []

    def get_positions(self) -> List[Position]:
        try:
            raw_positions = self._client.get_positions(coin=None)
        except HyperliquidClientError:
            return []

        return [
            Position(
                symbol=p.get("coin", ""),
                qty=float(p.get("szi", 0)),
                entry_price=float(p.get("entry_px", 0)),
                leverage=float(p.get("leverage", 1)),
                unrealized_pnl=float(p.get("unrealized_pnl", 0)),
                raw=p,
            )
            for p in raw_positions
        ]

    def get_balances(self) -> List[Balance]:
        try:
            account_value = self._client.get_account_value()
        except HyperliquidClientError:
            return []

        return [
            Balance(
                asset="USDT",
                free=float(account_value),
                locked=0.0,
                raw={"account_value_usdt": account_value},
            )
        ]

    def place_tpsl_native(
        self,
        symbol: str,
        side: str,
        qty: float,
        entry: float,
        tp: float | None,
        sl: float,
    ) -> dict:
        """
        Crée les TP/SL natifs pour une position existante.

        Si tp est None -> SL-only natif.
        Sinon -> TP+SL via primitive positionTpsl.
        """
        is_long = side.lower() == "buy"
        coin = symbol

        # Pas de TP -> SL-only natif
        if tp is None or tp <= 0:
            return self._client.place_sl_only(
                coin=coin,
                is_long=is_long,
                sz=float(qty),
                sl_price=float(sl),
            )

        # TP+SL natifs
        return self._client.place_position_tpsl(
            coin=coin,
            is_long=is_long,
            sz=float(qty),
            tp_price=float(tp),
            sl_price=float(sl),
        )

    def place_order(self, req: OrderRequest) -> OrderResult:
        is_buy = req.side.lower() == "buy"

        if hasattr(req, "leverage") and req.leverage and req.leverage > 0:
            try:
                self._client.update_leverage(
                    coin=req.symbol,
                    leverage=int(req.leverage),
                    is_cross=False,
                )
            except Exception as e:
                logger.warning("update_leverage %s x%s: %s", req.symbol, req.leverage, e)

        if req.order_type == "market":
            limit_px = float(req.price or 0)
            if limit_px <= 0:
                ticker = self._client.get_ticker(req.symbol)
                limit_px = float(ticker.get("price", 0))
            result = self._client.place_order(
                coin=req.symbol,
                is_buy=is_buy,
                sz=float(req.qty),
                limit_px=limit_px,
                order_type="market",
                reduce_only=req.reduce_only,
                tif="Ioc",
            )
        else:
            if req.price is None:
                raise ValueError("price requis pour un ordre limit")
            result = self._client.place_order(
                coin=req.symbol,
                is_buy=is_buy,
                sz=float(req.qty),
                limit_px=float(req.price),
                order_type="limit",
                reduce_only=req.reduce_only,
                tif="Gtc",
            )

        status = "accepted"
        oid: Optional[str] = None
        price = req.price

        if result.get("filled"):
            status = "filled"
            price = float(result.get("avg_px", price or 0))
        if "oid" in result and result.get("oid") is not None:
            oid = str(result["oid"])

        return OrderResult(
            order_id=oid or "",
            symbol=req.symbol,
            side=req.side,
            qty=float(req.qty),
            price=price,
            status=status,
            raw=result,
        )

    def cancel_order(self, order_id: str) -> CancelResult:
        try:
            open_orders = self._client.get_open_orders(coin=None)
        except HyperliquidClientError as e:
            return CancelResult(order_id=order_id, success=False, raw={"error": str(e)})

        oid_int: Optional[int] = None
        target_coin: Optional[str] = None
        try:
            oid_int = int(order_id)
        except ValueError:
            pass

        for o in open_orders:
            if oid_int is not None and o.get("oid") == oid_int:
                target_coin = o.get("coin")
                break

        if target_coin is None or oid_int is None:
            return CancelResult(
                order_id=order_id,
                success=False,
                raw={"error": "order_id introuvable"},
            )

        ok = self._client.cancel_order(target_coin, oid_int)
        return CancelResult(order_id=order_id, success=ok, raw={"coin": target_coin})

    def modify_stop_trigger_order(
        self,
        order_id: str,
        symbol: str,
        side_close: str,
        qty: float,
        trigger_price: float,
    ) -> bool:
        try:
            oid_int = int(order_id)
        except Exception:
            logger.warning("modify_stop_trigger_order: oid invalide %r", order_id)
            return False

        is_buy = str(side_close).lower() == "buy"

        try:
            ok = self._client.modify_order(
                coin=symbol,
                oid=oid_int,
                is_buy=is_buy,
                sz=float(qty),
                trigger_px=float(trigger_price),
                order_type="sl",
                reduce_only=True,
            )
            if ok:
                logger.info(
                    "modify_stop_trigger_order OK %s %s oid=%s qty=%.6f trigger=%.8f",
                    symbol,
                    side_close,
                    order_id,
                    float(qty),
                    float(trigger_price),
                )
            else:
                logger.warning(
                    "modify_stop_trigger_order NOK %s %s oid=%s qty=%.6f trigger=%.8f",
                    symbol,
                    side_close,
                    order_id,
                    float(qty),
                    float(trigger_price),
                )
            return bool(ok)
        except Exception as e:
            logger.warning(
                "modify_stop_trigger_order failed %s %s oid=%s trigger=%.8f: %r",
                symbol,
                side_close,
                order_id,
                float(trigger_price),
                e,
            )
            return False

    def get_candles(self, symbol: str, interval: str = "1h", limit: int = 50) -> list:
        try:
            return self._client.get_candles(symbol, interval=interval, limit=limit)
        except Exception:
            return []

    def place_position_tpsl(
        self,
        coin: str,
        is_long: bool,
        sz: float,
        tp_price: float | None,
        sl_price: float,
    ) -> dict:
        self._require_exchange()

        sz_decimals = self.get_sz_decimals(coin)
        sz = math.floor(float(sz) * 10**sz_decimals) / 10**sz_decimals
        if sz <= 0:
            raise HyperliquidClientError(f"Taille TPSL invalide: {sz}")

        sl_price = self.format_price(float(sl_price), sz_decimals)
        tp_price_fmt = None if tp_price is None else self.format_price(float(tp_price), sz_decimals)

        asset_index = self._get_asset_index(coin)
        exit_is_buy = not is_long

        orders = []
        order_kinds = []

        if tp_price_fmt is not None and tp_price_fmt > 0:
            orders.append(
                {
                    "a": asset_index,
                    "b": exit_is_buy,
                    "p": str(tp_price_fmt),
                    "s": str(sz),
                    "r": True,
                    "t": {
                        "trigger": {
                            "isMarket": True,
                            "triggerPx": str(tp_price_fmt),
                            "tpsl": "tp",
                        },
                    },
                }
            )
            order_kinds.append("tp")

        orders.append(
            {
                "a": asset_index,
                "b": exit_is_buy,
                "p": str(sl_price),
                "s": str(sz),
                "r": True,
                "t": {
                    "trigger": {
                        "isMarket": True,
                        "triggerPx": str(sl_price),
                        "tpsl": "sl",
                    },
                },
            }
        )
        order_kinds.append("sl")

        action = {
            "type": "order",
            "orders": orders,
            "grouping": "positionTpsl",
        }

        for method_name in ("post_action", "_post_action", "bulk_orders"):
            method = getattr(self.exchange, method_name, None)

            if method_name in ("post_action", "_post_action") and callable(method):
                try:
                    logger.info(
                        "Envoi TPSL natif positionTpsl %s sz=%.6f tp=%s sl=%.6f",
                        coin,
                        sz,
                        f"{tp_price_fmt:.6f}" if tp_price_fmt is not None else "None",
                        sl_price,
                    )
                    raw_result = method(action)
                    return self._parse_position_tpsl_response(
                        raw_result=raw_result,
                        order_kinds=order_kinds,
                        grouping="positionTpsl",
                    )
                except Exception as e:
                    logger.warning("Échec %s pour positionTpsl: %s", method_name, e)

            if method_name == "bulk_orders" and callable(method):
                try:
                    logger.info(
                        "Fallback bulk_orders TP/SL %s sz=%.6f tp=%s sl=%.6f",
                        coin,
                        sz,
                        f"{tp_price_fmt:.6f}" if tp_price_fmt is not None else "None",
                        sl_price,
                    )

                    fallback_orders = []
                    fallback_kinds = []

                    if tp_price_fmt is not None and tp_price_fmt > 0:
                        fallback_orders.append(
                            {
                                "coin": coin,
                                "is_buy": exit_is_buy,
                                "sz": sz,
                                "limit_px": tp_price_fmt,
                                "order_type": {
                                    "trigger": {
                                        "isMarket": True,
                                        "triggerPx": tp_price_fmt,
                                        "tpsl": "tp",
                                    }
                                },
                                "reduce_only": True,
                            }
                        )
                        fallback_kinds.append("tp")

                    fallback_orders.append(
                        {
                            "coin": coin,
                            "is_buy": exit_is_buy,
                            "sz": sz,
                            "limit_px": sl_price,
                            "order_type": {
                                "trigger": {
                                    "isMarket": True,
                                    "triggerPx": sl_price,
                                    "tpsl": "sl",
                                }
                            },
                            "reduce_only": True,
                        }
                    )
                    fallback_kinds.append("sl")

                    raw_result = method(fallback_orders)
                    wrapped = {"status": "ok", "response": raw_result}
                    return self._parse_position_tpsl_response(
                        raw_result=wrapped,
                        order_kinds=fallback_kinds,
                        grouping="fallback",
                    )
                except Exception as e:
                    logger.warning("Échec fallback bulk_orders TPSL: %s", e)

        raise HyperliquidClientError(
            "Aucune primitive SDK disponible pour envoyer les TP/SL natifs"
        )

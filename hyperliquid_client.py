"""
HYDRAQUEEN Grid Bot — Client Hyperliquid
Wrapper autour du SDK hyperliquid-python-sdk pour le grid bot.

Les méthodes publiques (Info) fonctionnent sans wallet.
Les méthodes privées (Exchange) nécessitent un wallet configuré.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time

from hyperliquid.utils.signing import sign_l1_action, get_timestamp_ms

HL_MAINNET_URL = "https://api.hyperliquid.xyz"
HL_TESTNET_URL = "https://api.hyperliquid-testnet.xyz"
HL_WALLET_CONFIG = "hl_config.json"
USE_TESTNET = False

logger = logging.getLogger("hydraqueen.hl_client")


class HyperliquidClientError(Exception):
    """Erreur spécifique au client Hyperliquid."""
    pass


class HyperliquidClient:
    """Client Hyperliquid wrappant le SDK pour le grid bot HYDRAQUEEN."""

    def __init__(
        self,
        wallet_key: str | None = None,
        config_path: str | None = None,
        use_testnet: bool | None = None,
    ):
        from hyperliquid.info import Info

        self._testnet = use_testnet if use_testnet is not None else USE_TESTNET
        self._api_url = HL_TESTNET_URL if self._testnet else HL_MAINNET_URL

        _empty_spot_meta = {"universe": [], "tokens": []}
        self.info = Info(self._api_url, skip_ws=True, spot_meta=_empty_spot_meta)

        self.exchange = None
        self.wallet = None
        self._wallet_address: str | None = None

        key = wallet_key
        account_addr = None
        if key is None:
            key, account_addr = self._load_wallet_config(config_path or HL_WALLET_CONFIG)

        if key:
            self._init_exchange(key, account_address=account_addr)

        self._meta_cache: dict | None = None
        self._meta_cache_time: float = 0.0
        self._META_CACHE_TTL = 300.0

        net_str = "TESTNET" if self._testnet else "MAINNET"
        auth_str = "authentifié" if self.exchange else "public uniquement"
        logger.info("HyperliquidClient initialisé — %s, %s", net_str, auth_str)

    def _load_wallet_config(self, config_path: str) -> tuple[str | None, str | None]:
        # Priorité 1 : variables d'environnement
        env_key = os.environ.get("HL_PRIVATE_KEY")
        env_account = os.environ.get("HL_ACCOUNT_ADDRESS")
        if env_key:
            logger.info("Clé wallet chargée depuis variable d'environnement HL_PRIVATE_KEY")
            if env_account:
                logger.info("Compte maître (env): %s...%s", env_account[:6], env_account[-4:])
            return env_key, env_account

        # Priorité 2 : fichier config
        if not os.path.isfile(config_path):
            logger.debug("Fichier config wallet introuvable: %s", config_path)
            return None, None

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)

            key = config.get("private_key") or config.get("wallet_key") or config.get("key")
            account = config.get("account_address") or config.get("vault_address")

            # Refuser les placeholders
            if key and key.startswith("SET_VIA_ENV"):
                logger.warning("hl_config.json contient un placeholder — configurez HL_PRIVATE_KEY en variable d'environnement")
                return None, None

            if key:
                logger.info("Clé wallet chargée depuis %s", config_path)
            if account:
                logger.info("Compte maître: %s...%s", account[:6], account[-4:])

            return key, account
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Erreur lecture config wallet %s: %s", config_path, e)
            return None, None

    def _init_exchange(self, private_key: str, account_address: str | None = None) -> None:
        try:
            import eth_account
            from hyperliquid.exchange import Exchange

            self.wallet = eth_account.Account.from_key(private_key)
            self._wallet_address = account_address or self.wallet.address
            _empty_spot = {"universe": [], "tokens": []}

            self.exchange = Exchange(
                self.wallet,
                self._api_url,
                account_address=account_address,
                spot_meta=_empty_spot,
            )

            logger.info(
                "Exchange client initialisé — wallet %s...%s",
                self._wallet_address[:6],
                self._wallet_address[-4:],
            )
        except Exception as e:
            logger.error("Échec initialisation Exchange: %s", e)
            self.exchange = None
            self.wallet = None
            self._wallet_address = None

    def _require_exchange(self) -> None:
        if self.exchange is None:
            raise HyperliquidClientError(
                "Exchange non initialisé — wallet requis pour cette opération"
            )

    @property
    def wallet_address(self) -> str | None:
        return self._wallet_address

    def test_connection(self) -> dict:
        try:
            mids = self.info.all_mids()
            return {
                "ok": True,
                "coins": len(mids),
                "has_wallet": self.exchange is not None,
            }
        except Exception as e:
            raise HyperliquidClientError(f"Échec test_connection: {e}") from e

    def get_all_mids(self) -> dict[str, float]:
        try:
            mids = self.info.all_mids()
            return {k: float(v) for k, v in mids.items()}
        except Exception as e:
            raise HyperliquidClientError(f"Erreur get_all_mids: {e}") from e

    def get_meta(self) -> dict:
        now = time.monotonic()
        if self._meta_cache and (now - self._meta_cache_time) < self._META_CACHE_TTL:
            return self._meta_cache

        try:
            result = self.info.meta_and_asset_ctxs()
            meta = result[0]
            ctxs = result[1]
            self._meta_cache = {"meta": meta, "asset_ctxs": ctxs}
            self._meta_cache_time = now
            return self._meta_cache
        except Exception as e:
            raise HyperliquidClientError(f"Erreur get_meta: {e}") from e

    def get_universe(self) -> list[dict]:
        meta = self.get_meta()
        return meta.get("meta", {}).get("universe", [])

    def get_asset_ctx(self, coin: str) -> dict | None:
        meta = self.get_meta()
        universe = meta.get("meta", {}).get("universe", [])
        ctxs = meta.get("asset_ctxs", [])

        for i, u in enumerate(universe):
            if u.get("name") == coin and i < len(ctxs):
                return ctxs[i]
        return None

    def get_sz_decimals(self, coin: str) -> int:
        universe = self.get_universe()
        for u in universe:
            if u.get("name") == coin:
                return int(u.get("szDecimals", 3))
        return 3

    def _get_asset_index(self, coin: str) -> int:
        universe = self.get_universe()
        for i, u in enumerate(universe):
            if str(u.get("name", "")).upper() == coin.upper():
                return i
        raise HyperliquidClientError(f"Asset introuvable dans universe: {coin}")

    @staticmethod
    def format_price(price: float, sz_decimals: int) -> float:
        if price <= 0:
            return 0.0
        max_decimals = 6 - sz_decimals
        formatted = float(f"{price:.5g}")
        formatted = round(formatted, max(0, max_decimals))
        return formatted

    def get_l2_snapshot(self, coin: str) -> dict:
        try:
            return self.info.l2_snapshot(coin)
        except Exception as e:
            raise HyperliquidClientError(f"Erreur get_l2_snapshot({coin}): {e}") from e

    def get_candles(
        self,
        coin: str,
        interval: str = "1h",
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 200,
    ) -> list[dict]:
        now_ms = int(time.time() * 1000)
        if end_ms is None:
            end_ms = now_ms

        if start_ms is None:
            interval_ms = {
                "1m": 60_000,
                "5m": 300_000,
                "15m": 900_000,
                "1h": 3_600_000,
                "4h": 14_400_000,
                "1d": 86_400_000,
            }.get(interval, 3_600_000)
            start_ms = end_ms - (limit * interval_ms)

        try:
            raw = self.info.candles_snapshot(coin, interval, start_ms, end_ms)
        except Exception as e:
            raise HyperliquidClientError(f"Erreur get_candles({coin}): {e}") from e

        candles = []
        for c in raw:
            candles.append(
                {
                    "open": float(c.get("o", 0)),
                    "high": float(c.get("h", 0)),
                    "low": float(c.get("l", 0)),
                    "close": float(c.get("c", 0)),
                    "vol": float(c.get("v", 0)),
                    "time": c.get("t", c.get("T", 0)),
                }
            )
        return candles

    def get_funding_rate(self, coin: str) -> float:
        ctx = self.get_asset_ctx(coin)
        if ctx:
            return float(ctx.get("funding", 0))
        return 0.0

    def get_ticker(self, coin: str) -> dict:
        ctx = self.get_asset_ctx(coin)
        if not ctx:
            raise HyperliquidClientError(f"Asset non trouvé: {coin}")

        mid_px = float(ctx.get("midPx", 0))
        mark_px = float(ctx.get("markPx", 0))
        oracle_px = float(ctx.get("oraclePx", 0))
        volume_24h = float(ctx.get("dayNtlVlm", 0))
        oi = float(ctx.get("openInterest", 0))
        funding = float(ctx.get("funding", 0))
        premium = float(ctx.get("premium", 0))
        prev_day_px = float(ctx.get("prevDayPx", 0))

        bid, ask = 0.0, 0.0
        try:
            book = self.get_l2_snapshot(coin)
            levels = book.get("levels", [[], []])
            if levels[0]:
                bid = float(levels[0][0].get("px", 0))
            if levels[1]:
                ask = float(levels[1][0].get("px", 0))
        except HyperliquidClientError:
            pass

        price = mid_px or mark_px or oracle_px
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else price
        spread_bps = (
            (ask - bid) / mid * 10000 if mid > 0 and bid > 0 and ask > 0 else 0
        )

        return {
            "coin": coin,
            "price": price,
            "mid": mid_px,
            "mark": mark_px,
            "oracle": oracle_px,
            "volume_24h": volume_24h,
            "open_interest": oi,
            "open_interest_usdt": oi * price if price > 0 else 0,
            "funding_rate": funding,
            "premium": premium,
            "prev_day_price": prev_day_px,
            "bid": bid,
            "ask": ask,
            "spread_bps": round(spread_bps, 2),
        }

    def get_user_state(self) -> dict:
        self._require_exchange()
        try:
            return self.info.user_state(self._wallet_address)
        except Exception as e:
            raise HyperliquidClientError(f"Erreur get_user_state: {e}") from e

    def get_positions(self, coin: str | None = None) -> list[dict]:
        self._require_exchange()
        try:
            state = self.info.user_state(self._wallet_address)
        except Exception as e:
            raise HyperliquidClientError(f"Erreur get_positions: {e}") from e

        positions = []
        for ap in state.get("assetPositions", []):
            pos = ap.get("position", {})
            pos_coin = pos.get("coin", "")
            if coin and str(pos_coin).upper() != str(coin).upper():
                continue

            szi = float(pos.get("szi", 0) or 0)
            if szi == 0:
                continue

            leverage_raw = pos.get("leverage", 1)
            leverage = float(
                leverage_raw.get("value", 1)
                if isinstance(leverage_raw, dict)
                else leverage_raw
            )

            positions.append(
                {
                    "coin": pos_coin,
                    "szi": szi,
                    "size": abs(szi),
                    "side": "long" if szi > 0 else "short",
                    "entry_px": float(pos.get("entryPx", 0) or 0),
                    "leverage": leverage,
                    "unrealized_pnl": float(pos.get("unrealizedPnl", 0) or 0),
                    "liquidation_px": float(pos.get("liquidationPx", 0) or 0),
                }
            )
        return positions

    def get_open_orders(self, coin: str | None = None) -> list[dict]:
        self._require_exchange()
        try:
            orders = self.info.open_orders(self._wallet_address)
        except Exception as e:
            raise HyperliquidClientError(f"Erreur get_open_orders: {e}") from e

        result = []
        for o in orders:
            o_coin = o.get("coin", "")
            if coin and str(o_coin).upper() != str(coin).upper():
                continue

            side_raw = o.get("side", "")
            side = "buy" if side_raw == "B" else "sell"

            order_type = o.get("orderType", o.get("order_type", {}))
            trigger_px_raw = (
                o.get("triggerPx")
                or o.get("trigger_px")
                or o.get("triggerPrice")
            )

            reduce_only_raw = (
                o.get("reduceOnly")
                if "reduceOnly" in o
                else o.get("reduce_only", o.get("reduceonly", False))
            )

            try:
                trigger_px = float(trigger_px_raw) if trigger_px_raw is not None else None
            except Exception:
                trigger_px = None

            result.append(
                {
                    "coin": o_coin,
                    "oid": o.get("oid"),
                    "side": side,
                    "sz": float(o.get("sz", 0) or 0),
                    "limit_px": float(o.get("limitPx", 0) or 0),
                    "timestamp": o.get("timestamp", 0),
                    "orderType": order_type,
                    "order_type": order_type,
                    "triggerPx": trigger_px,
                    "reduceOnly": bool(reduce_only_raw),
                    "reduce_only": bool(reduce_only_raw),
                    "raw": o,
                }
            )
        return result

    def get_user_fills(self, coin: str | None = None, limit: int = 100) -> list[dict]:
        self._require_exchange()
        try:
            fills = self.info.user_fills(self._wallet_address)
        except Exception as e:
            raise HyperliquidClientError(f"Erreur get_user_fills: {e}") from e

        result = []
        for f in fills:
            f_coin = f.get("coin", "")
            if coin and str(f_coin).upper() != str(coin).upper():
                continue

            side_raw = f.get("side", "")
            side = "buy" if side_raw == "B" else "sell"

            result.append(
                {
                    "coin": f_coin,
                    "side": side,
                    "sz": float(f.get("sz", 0) or 0),
                    "px": float(f.get("px", 0) or 0),
                    "closed_pnl": float(f.get("closedPnl", 0) or 0),
                    "fee": float(f.get("fee", 0) or 0),
                    "start_position": float(f.get("startPosition", 0) or 0),
                    "dir": f.get("dir", ""),
                    "time": f.get("time", 0),
                    "oid": f.get("oid"),
                    "hash": f.get("hash", ""),
                }
            )
        return result[:limit]

    def get_recent_closed_trade(
        self,
        coin: str,
        since_ms: int,
        max_wait_sec: float = 5.0,
    ) -> dict | None:
        """
        Attend brièvement qu'un fill de clôture apparaisse pour `coin`,
        puis retourne le plus récent fill avec closed_pnl != 0 ou réduction de position.
        """
        self._require_exchange()
        deadline = time.time() + max_wait_sec

        while time.time() < deadline:
            try:
                fills = self.get_user_fills(coin=coin, limit=20)
            except Exception as e:
                logger.warning("get_recent_closed_trade(%s) user_fills error: %s", coin, e)
                fills = []

            candidates = []
            for f in fills:
                if int(f.get("time", 0) or 0) < int(since_ms):
                    continue

                start_pos = abs(float(f.get("start_position", 0) or 0))
                fill_sz = abs(float(f.get("sz", 0) or 0))
                closed_pnl = float(f.get("closed_pnl", 0) or 0)

                is_closing = closed_pnl != 0.0 or (start_pos > 0 and fill_sz <= start_pos)
                if is_closing:
                    candidates.append(f)

            if candidates:
                candidates.sort(key=lambda x: int(x.get("time", 0) or 0), reverse=True)
                return candidates[0]

            time.sleep(0.4)

        return None

    def place_order(
        self,
        coin: str,
        is_buy: bool,
        sz: float,
        limit_px: float,
        order_type: str = "limit",
        reduce_only: bool = False,
        tif: str = "Alo",
    ) -> dict:
        self._require_exchange()

        sz_decimals = self.get_sz_decimals(coin)
        sz_before = float(sz)
        sz = math.floor(float(sz) * 10**sz_decimals) / 10**sz_decimals
        if sz <= 0:
            raise HyperliquidClientError(f"Taille invalide après arrondi: {sz}")

        limit_px = self.format_price(float(limit_px), sz_decimals)
        notional = sz * limit_px

        logger.debug(
            "place_order(%s): sz_in=%.6f -> sz=%.6f, px=%.4f, notional=%.2f, szDec=%d, tif=%s",
            coin,
            sz_before,
            sz,
            limit_px,
            notional,
            sz_decimals,
            tif,
        )

        if notional < 10.0 and not reduce_only:
            raise HyperliquidClientError(
                f"Notional {notional:.2f} < $10 minimum (sz={sz}, px={limit_px})"
            )

        try:
            if order_type == "market":
                result = self.exchange.market_open(coin, is_buy, sz)
            else:
                ot = {"limit": {"tif": tif}}
                result = self.exchange.order(
                    coin,
                    is_buy=is_buy,
                    sz=sz,
                    limit_px=limit_px,
                    order_type=ot,
                    reduce_only=reduce_only,
                )
        except Exception as e:
            raise HyperliquidClientError(f"Erreur place_order({coin}): {e}") from e

        return self._parse_order_response(result)

    def place_bulk_orders(self, orders: list[dict]) -> list[dict]:
        self._require_exchange()

        bulk = []
        skipped = 0

        for o in orders:
            coin = o["coin"]
            sz_decimals = self.get_sz_decimals(coin)
            sz = math.floor(float(o["sz"]) * 10**sz_decimals) / 10**sz_decimals
            if sz <= 0:
                skipped += 1
                continue

            limit_px = self.format_price(float(o["limit_px"]), sz_decimals)
            notional = sz * limit_px
            if notional < 10.0 and not o.get("reduce_only", False):
                logger.warning(
                    "Ordre %s %s ignoré: notional %.2f < $10 (sz=%.6f, px=%.1f)",
                    coin,
                    "BUY" if o["is_buy"] else "SELL",
                    notional,
                    sz,
                    limit_px,
                )
                skipped += 1
                continue

            tif = o.get("tif", "Alo")
            bulk.append(
                {
                    "coin": coin,
                    "is_buy": o["is_buy"],
                    "sz": sz,
                    "limit_px": limit_px,
                    "order_type": {"limit": {"tif": tif}},
                    "reduce_only": o.get("reduce_only", False),
                }
            )

        if skipped:
            logger.info("Bulk orders: %d ignorés (sz=0 ou notional<$10)", skipped)

        if not bulk:
            return []

        try:
            result = self.exchange.bulk_orders(bulk)
        except Exception as e:
            raise HyperliquidClientError(f"Erreur place_bulk_orders: {e}") from e

        return self._parse_bulk_response(result)

    def cancel_order(self, coin: str, oid: int) -> bool:
        self._require_exchange()
        try:
            self.exchange.cancel(coin, oid)
            return True
        except Exception as e:
            logger.warning("Erreur cancel_order(%s, %d): %s", coin, oid, e)
            return False

    def cancel(self, coin: str, oid: int) -> bool:
        return self.cancel_order(coin, oid)

    def _parse_modify_response(self, response, oid: int, coin: str) -> bool:
        """
        Parse la réponse de modify_order pour vérifier si l'opération a réellement réussi.
        
        Hyperliquid renvoie typiquement:
        - Succès: {"status": "ok", "response": {"type": "default"}} ou similaire
        - Échec: {"status": "err", "response": "Order not found / Cannot modify / ..."}
          ou: {"status": "ok", "response": {"data": {"statuses": [{"error": "..."}]}}}
        
        Args:
            response: La réponse brute de l'API
            oid: L'OID modifié (pour logging)
            coin: Le symbole (pour logging)
            
        Returns:
            True si la modification a réellement été appliquée, False sinon
        """
        # Cas 1: réponse None ou vide
        if response is None:
            logger.warning("modify_order: réponse None pour oid=%d coin=%s", oid, coin)
            return False
        
        # Cas 2: réponse non-dict (ex: True/False, string, etc.)
        if not isinstance(response, dict):
            # Si c'est un booléen direct, l'utiliser
            if isinstance(response, bool):
                return response
            # Sinon, tenter une interprétation libérale
            logger.debug("modify_order: réponse non-dict pour oid=%d: %r", oid, response)
            return True  # par défaut on considère succès si pas d'erreur explicite
        
        # Cas 3: status au top level
        status = str(response.get("status", "")).lower()
        if status == "err" or status == "error":
            logger.warning(
                "modify_order: status=err pour oid=%d coin=%s: %r",
                oid, coin, response.get("response", "no detail")
            )
            return False
        
        # Cas 4: vérifier les statuses imbriqués (format Hyperliquid)
        resp_inner = response.get("response", {})
        if isinstance(resp_inner, dict):
            data = resp_inner.get("data", {})
            if isinstance(data, dict):
                statuses = data.get("statuses", [])
                if isinstance(statuses, list) and statuses:
                    for s in statuses:
                        if isinstance(s, dict) and "error" in s:
                            logger.warning(
                                "modify_order: erreur dans statuses pour oid=%d coin=%s: %s",
                                oid, coin, s["error"]
                            )
                            return False
        
        # Cas 5: status=ok par défaut → succès
        if status == "ok":
            return True
        
        # Cas 6: pas de status explicite mais pas d'erreur → succès par défaut
        return True

    def modify_order(
        self,
        coin: str,
        oid: int,
        is_buy: bool,
        sz: float,
        trigger_px: float,
        *,
        order_type: str = "sl",
        reduce_only: bool = True,
    ) -> bool:
        self._require_exchange()

        try:
            sz_decimals = self.get_sz_decimals(coin)
            sz = math.floor(float(sz) * 10**sz_decimals) / 10**sz_decimals
            if sz <= 0:
                return False

            trigger_px = self.format_price(float(trigger_px), sz_decimals)

            trigger_order_type = {
                "trigger": {
                    "isMarket": True,
                    "triggerPx": float(trigger_px),
                    "tpsl": str(order_type),
                }
            }

            if hasattr(self.exchange, "modify_order"):
                response = self.exchange.modify_order(
                    oid=int(oid),
                    name=coin,
                    is_buy=bool(is_buy),
                    sz=float(sz),
                    limit_px=float(trigger_px),
                    order_type=trigger_order_type,
                    reduce_only=bool(reduce_only),
                )
                # Vérifier la réponse de l'API au lieu de retourner True aveuglément
                ok = self._parse_modify_response(response, oid, coin)
                if not ok:
                    logger.warning(
                        "modify_order rejeté par Hyperliquid: coin=%s oid=%d trigger=%s response=%r",
                        coin, oid, trigger_px, response
                    )
                return ok

            if hasattr(self.exchange, "modify"):
                order = {
                    "coin": coin,
                    "is_buy": bool(is_buy),
                    "sz": float(sz),
                    "limit_px": float(trigger_px),
                    "order_type": trigger_order_type,
                    "reduce_only": bool(reduce_only),
                }
                response = self.exchange.modify(int(oid), order)
                ok = self._parse_modify_response(response, oid, coin)
                if not ok:
                    logger.warning(
                        "modify rejeté par Hyperliquid: coin=%s oid=%d trigger=%s response=%r",
                        coin, oid, trigger_px, response
                    )
                return ok

            raise HyperliquidClientError(
                "Aucune méthode modify/modify_order disponible sur exchange"
            )

        except Exception as e:
            logger.warning(
                "Erreur modify_order(%s, %d, %s): %s",
                coin,
                oid,
                trigger_px,
                e,
            )
            return False
    
    def cancel_all_orders(self, coin: str | None = None) -> int:
        self._require_exchange()
        try:
            orders = self.get_open_orders(coin)
            if not orders:
                return 0

            cancels = [{"coin": o["coin"], "oid": o["oid"]} for o in orders]
            self.exchange.bulk_cancel(cancels)
            return len(cancels)
        except Exception as e:
            raise HyperliquidClientError(f"Erreur cancel_all_orders: {e}") from e

    def market_close(self, coin: str) -> dict:
        self._require_exchange()
        try:
            result = self.exchange.market_close(coin)
        except Exception as e:
            raise HyperliquidClientError(f"Erreur market_close({coin}): {e}") from e
        return self._parse_order_response(result)

    def update_leverage(self, coin: str, leverage: int, is_cross: bool = True) -> bool:
        self._require_exchange()
        try:
            self.exchange.update_leverage(leverage, coin, is_cross=is_cross)
            return True
        except Exception as e:
            logger.warning("Erreur update_leverage(%s, %d): %s", coin, leverage, e)
            return False

    def get_account_value(self) -> float:
        self._require_exchange()
        try:
            state = self.info.user_state(self._wallet_address)
            summary = state.get("marginSummary", {})
            return float(summary.get("accountValue", 0))
        except Exception as e:
            raise HyperliquidClientError(f"Erreur get_account_value: {e}") from e

    def place_sl_only(
        self,
        coin: str,
        is_long: bool,
        sz: float,
        sl_price: float,
    ) -> dict:
        return self.place_position_tpsl(
            coin=coin,
            is_long=is_long,
            sz=sz,
            tp_price=None,
            sl_price=sl_price,
        )

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

        # Utiliser directement bulk_orders (pas de fallback, pas de tentative _post_action)
        # car bulk_orders signe automatiquement via le SDK
        try:
            logger.info(
                "Envoi TPSL natif bulk_orders %s sz=%.6f tp=%s sl=%.6f",
                coin,
                sz,
                f"{tp_price_fmt:.6f}" if tp_price_fmt is not None else "None",
                sl_price,
            )

            bulk_orders = []
            if tp_price_fmt is not None and tp_price_fmt > 0:
                bulk_orders.append(
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

            bulk_orders.append(
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

            result = self.exchange.bulk_orders(bulk_orders, grouping="positionTpsl")
            wrapped = {"status": "ok", "response": result, "grouping": "positionTpsl"}
            parsed = self._parse_position_tpsl_response(
                wrapped,
                order_kinds,
                grouping="positionTpsl",
            )
            
            # FIX OID RESOLUTION: si l'API renvoie waitingForTrigger sans OID,
            # on récupère les OIDs via frontend_open_orders en matchant sur le prix
            if parsed.get("tp_oid") is None or parsed.get("sl_oid") is None:
                logger.info(
                    "OID manquant après place_position_tpsl %s (tp=%s sl=%s), résolution via fallback...",
                    coin, parsed.get("tp_oid"), parsed.get("sl_oid")
                )
                resolved = self._resolve_trigger_oids(
                    coin=coin,
                    is_long=is_long,
                    tp_price=tp_price_fmt,
                    sl_price=sl_price,
                    max_retries=12,
                    retry_delay=0.5,
                )
                # Merger les OIDs résolus avec ce qu'on a déjà
                if parsed.get("tp_oid") is None and resolved.get("tp_oid") is not None:
                    parsed["tp_oid"] = resolved["tp_oid"]
                    # Mettre aussi à jour la liste orders
                    for o in parsed.get("orders", []):
                        if o.get("kind") == "tp":
                            o["oid"] = resolved["tp_oid"]
                            o["status"] = "resting_resolved"
                if parsed.get("sl_oid") is None and resolved.get("sl_oid") is not None:
                    parsed["sl_oid"] = resolved["sl_oid"]
                    for o in parsed.get("orders", []):
                        if o.get("kind") == "sl":
                            o["oid"] = resolved["sl_oid"]
                            o["status"] = "resting_resolved"
                
                if parsed.get("tp_oid") is None or parsed.get("sl_oid") is None:
                    logger.warning(
                        "⚠ ATTENTION %s: TP/SL placé SANS OID récupéré ! Le trailing NE POURRA PAS modifier ces ordres. tp=%s sl=%s",
                        coin, parsed.get("tp_oid"), parsed.get("sl_oid")
                    )
            
            return parsed
        except Exception as e:
            logger.error("Échec placement TP/SL natif bulk_orders: %s", e)
            raise HyperliquidClientError(f"Impossible de placer les TP/SL: {e}") from e

    def _resolve_trigger_oids(
        self,
        coin: str,
        is_long: bool,
        tp_price: float | None,
        sl_price: float,
        max_retries: int = 12,
        retry_delay: float = 0.5,
    ) -> dict:
        """
        Résout les OIDs des ordres TP/SL fraîchement posés en interrogeant frontend_open_orders.
        
        Hyperliquid renvoie souvent 'waitingForTrigger' au lieu d'un OID immédiat.
        On interroge frontend_open_orders jusqu'à trouver les ordres (matching par prix).
        
        Args:
            coin: Le symbole
            is_long: True si position long (donc TP/SL sont des sell)
            tp_price: Prix attendu du TP (None si pas de TP)
            sl_price: Prix attendu du SL
            max_retries: Nombre max de tentatives
            retry_delay: Délai entre tentatives en secondes
            
        Returns:
            {"tp_oid": int|None, "sl_oid": int|None}
        """
        out = {"tp_oid": None, "sl_oid": None}
        expected_close_side = "sell" if is_long else "buy"
        
        # Variable pour debug : on logge ce qu'on voit aux deux dernières tentatives
        debug_attempts = {max_retries - 2, max_retries - 1}
        
        for attempt in range(max_retries):
            try:
                orders = self.get_open_orders(coin=coin) or []
            except Exception as e:
                logger.warning("_resolve_trigger_oids: get_open_orders échec %s: %r", coin, e)
                time.sleep(retry_delay)
                continue
            
            # DEBUG: logger ce qu'on voit lors des dernières tentatives
            if attempt in debug_attempts and (out["tp_oid"] is None or (out["sl_oid"] is None)):
                trigger_count = sum(
                    1 for o in orders 
                    if isinstance(o, dict) and (
                        bool(o.get("isTrigger", False)) 
                        or "trigger" in str(o.get("orderType", "")).lower()
                        or str(o.get("tpsl", "")).lower() in ("tp", "sl")
                    )
                )
                logger.info(
                    "DEBUG _resolve_trigger_oids %s tentative %d: %d ordres total, %d triggers visibles",
                    coin, attempt + 1, len(orders), trigger_count
                )
                # Logger jusqu'à 3 ordres trigger pour debug
                count = 0
                for o in orders:
                    if not isinstance(o, dict):
                        continue
                    is_trig = (
                        bool(o.get("isTrigger", False)) 
                        or "trigger" in str(o.get("orderType", "")).lower()
                        or str(o.get("tpsl", "")).lower() in ("tp", "sl")
                    )
                    if is_trig:
                        logger.info(
                            "  DEBUG order: coin=%s side=%s tpsl=%s isTrigger=%s orderType=%s triggerPx=%s limitPx=%s reduceOnly=%s oid=%s",
                            o.get("coin"), o.get("side"), o.get("tpsl"), 
                            o.get("isTrigger"), o.get("orderType"),
                            o.get("triggerPx"), o.get("limitPx"),
                            o.get("reduceOnly"), o.get("oid")
                        )
                        count += 1
                        if count >= 3:
                            break
            
            for o in orders:
                if not isinstance(o, dict):
                    continue
                
                # Matching coin tolérant : "DYDX" == "DYDX-USDC" si commence pareil
                order_coin = str(o.get("coin", "")).upper()
                target_coin = coin.upper()
                if order_coin != target_coin and not order_coin.startswith(target_coin + "-"):
                    continue
                
                # Vérifier que c'est un ordre trigger ou TP/SL
                # Plus tolérant : on accepte si tpsl est "tp" ou "sl" même sans isTrigger
                tpsl = str(o.get("tpsl", "")).lower()
                is_trigger = (
                    bool(o.get("isTrigger", False)) 
                    or "trigger" in str(o.get("orderType", "")).lower()
                    or "stop" in str(o.get("orderType", "")).lower()
                    or tpsl in ("tp", "sl")
                )
                if not is_trigger:
                    continue
                
                # Vérifier le côté de fermeture
                side = str(o.get("side", "")).lower()
                # Hyperliquid utilise plusieurs formats : "B"/"A", "buy"/"sell", "Buy"/"Sell"
                if side in ("b", "buy"):
                    side_normalized = "buy"
                elif side in ("a", "sell"):
                    side_normalized = "sell"
                else:
                    side_normalized = side
                
                if side_normalized != expected_close_side:
                    continue
                
                # Récupérer le prix de trigger (plusieurs noms possibles)
                try:
                    trigger_px = float(
                        o.get("triggerPx", 0) 
                        or o.get("trigger_px", 0)
                        or o.get("limitPx", 0)
                        or o.get("limit_px", 0)
                        or 0
                    )
                except (ValueError, TypeError):
                    continue
                
                if trigger_px <= 0:
                    continue
                
                oid = o.get("oid")
                if oid is None:
                    continue
                
                # Tolérance plus généreuse: 0.5% au lieu de 0.1% pour absorber les arrondis tick
                # Si pas de tpsl explicite, on devine via le prix
                tolerance_tp = (tp_price or 0) * 0.005
                tolerance_sl = sl_price * 0.005
                
                # Matching TP
                if out["tp_oid"] is None and tp_price is not None and tp_price > 0:
                    diff_tp = abs(trigger_px - tp_price)
                    # Si tpsl explicite "tp", on prend
                    # Si pas de tpsl, on devine via la proximité du prix
                    matches_tp = (tpsl == "tp" and diff_tp <= tolerance_tp) or \
                                 (tpsl == "" and diff_tp <= tolerance_tp and diff_tp < abs(trigger_px - sl_price))
                    if matches_tp:
                        out["tp_oid"] = int(oid)
                        logger.info("✓ OID TP résolu %s: oid=%s trigger=%.6f (cible=%.6f, diff=%.6f) tentative %d", 
                                    coin, oid, trigger_px, tp_price, diff_tp, attempt + 1)
                        continue
                
                # Matching SL
                if out["sl_oid"] is None:
                    diff_sl = abs(trigger_px - sl_price)
                    matches_sl = (tpsl == "sl" and diff_sl <= tolerance_sl) or \
                                 (tpsl == "" and diff_sl <= tolerance_sl and (tp_price is None or diff_sl < abs(trigger_px - tp_price)))
                    if matches_sl:
                        out["sl_oid"] = int(oid)
                        logger.info("✓ OID SL résolu %s: oid=%s trigger=%.6f (cible=%.6f, diff=%.6f) tentative %d", 
                                    coin, oid, trigger_px, sl_price, diff_sl, attempt + 1)
                        continue
            
            # Sortir si tout est résolu
            tp_done = (tp_price is None or tp_price <= 0) or out["tp_oid"] is not None
            sl_done = out["sl_oid"] is not None
            if tp_done and sl_done:
                break
            
            time.sleep(retry_delay)
        
        if out["tp_oid"] is None and tp_price is not None and tp_price > 0:
            logger.warning("⚠ Échec résolution OID TP %s après %d tentatives (cible=%.6f)", 
                           coin, max_retries, tp_price)
        if out["sl_oid"] is None:
            logger.warning("⚠ Échec résolution OID SL %s après %d tentatives (cible=%.6f)", 
                           coin, max_retries, sl_price)
        
        return out

    def _parse_position_tpsl_response(
        self,
        raw_result: dict,
        order_kinds: list,
        grouping: str = "positionTpsl",
    ) -> dict:
        if not isinstance(raw_result, dict):
            return {
                "status": "ok",
                "grouping": grouping,
                "tp_oid": None,
                "sl_oid": None,
                "orders": [],
                "raw": raw_result,
            }

        response = raw_result.get("response", raw_result)
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except Exception:
                response = {}

        if isinstance(response, dict) and isinstance(response.get("response"), str):
            try:
                response["response"] = json.loads(response["response"])
            except Exception:
                response["response"] = {}

        inner = response.get("response", response) if isinstance(response, dict) else {}
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except Exception:
                inner = {}

        data = inner.get("data", {}) if isinstance(inner, dict) else {}
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = {}

        statuses = data.get("statuses", []) if isinstance(data, dict) else []

        parsed_orders = []
        tp_oid = None
        sl_oid = None

        for idx, kind in enumerate(order_kinds):
            s = statuses[idx] if idx < len(statuses) else {}
            item = {"kind": kind, "status": "unknown", "oid": None}

            if "resting" in s:
                oid = s["resting"].get("oid")
                item = {"kind": kind, "status": "resting", "oid": oid}
            elif "filled" in s:
                item = {
                    "kind": kind,
                    "status": "filled",
                    "oid": None,
                    "total_sz": float(s["filled"].get("totalSz", 0) or 0),
                    "avg_px": float(s["filled"].get("avgPx", 0) or 0),
                }
            elif "error" in s:
                item = {"kind": kind, "status": "error", "error": s["error"], "oid": None}

            if kind == "tp":
                tp_oid = item.get("oid")
            elif kind == "sl":
                sl_oid = item.get("oid")

            parsed_orders.append(item)

        return {
            "status": "ok",
            "grouping": grouping,
            "tp_oid": tp_oid,
            "sl_oid": sl_oid,
            "orders": parsed_orders,
            "raw": raw_result,
        }

    def _parse_order_response(self, result: dict) -> dict:
        if result.get("status") != "ok":
            raise HyperliquidClientError(f"Ordre rejeté: {result}")

        response = result.get("response", {})
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except (json.JSONDecodeError, TypeError):
                response = {}

        data = response.get("data", {}) if isinstance(response, dict) else {}
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                data = {}

        statuses = data.get("statuses", [])
        if not statuses:
            return {"status": "ok", "oid": None, "filled": False}

        s = statuses[0]
        if "resting" in s:
            return {"status": "ok", "oid": s["resting"]["oid"], "filled": False}
        if "filled" in s:
            return {
                "status": "ok",
                "oid": None,
                "filled": True,
                "total_sz": float(s["filled"].get("totalSz", 0)),
                "avg_px": float(s["filled"].get("avgPx", 0)),
            }
        if "error" in s:
            raise HyperliquidClientError(f"Erreur ordre: {s['error']}")

        return {"status": "ok", "oid": None, "filled": False}

    def _parse_bulk_response(self, result: dict) -> list[dict]:
        if result.get("status") != "ok":
            raise HyperliquidClientError(f"Ordres bulk rejetés: {result}")

        response = result.get("response", {})
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except (json.JSONDecodeError, TypeError):
                raise HyperliquidClientError(f"Réponse bulk non-parsable: {response[:200]}")

        data = response.get("data", {}) if isinstance(response, dict) else {}
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                data = {}

        statuses = data.get("statuses", [])
        results = []
        errors_count = 0
        first_error = None

        for s in statuses:
            if "resting" in s:
                results.append({"status": "ok", "oid": s["resting"]["oid"], "filled": False})
            elif "filled" in s:
                results.append(
                    {
                        "status": "ok",
                        "oid": None,
                        "filled": True,
                        "total_sz": float(s["filled"].get("totalSz", 0)),
                        "avg_px": float(s["filled"].get("avgPx", 0)),
                    }
                )
            elif "error" in s:
                errors_count += 1
                if first_error is None:
                    first_error = s["error"]
                results.append({"status": "error", "error": s["error"]})
            else:
                results.append({"status": "unknown"})

        if errors_count:
            logger.warning(
                "Bulk response: %d/%d erreurs. Première: %s",
                errors_count,
                len(statuses),
                first_error,
            )

        return results

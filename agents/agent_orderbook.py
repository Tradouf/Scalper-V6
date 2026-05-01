"""
AgentOrderbook V5 — agent_orderbook.py (fichier complet)
AUCUN appel LLM — 100% calcul Python.
V5.1 : ajout compute_orderbook_features + merge_features dans SharedMemory.
"""
from __future__ import annotations
import logging
from typing import Dict, Optional
try:
    from agents.feature_engine import compute_orderbook_features
except Exception:
    def compute_orderbook_features(base): return {}

logger = logging.getLogger("sdm.orderbook")
MIN_SPREAD_PCT = 0.05
MIN_DEPTH_USDT = 3000


class AgentOrderbook:
    def __init__(self, exchange_client) -> None:
        self.client = exchange_client
        self.memory = None

    def set_memory(self, memory) -> None:
        self.memory = memory

    def analyze(self, symbol: str, depth: int = 10) -> Dict:
        try:
            ob = self._fetch_orderbook(symbol, depth)
            if not ob:
                return self._empty(symbol, "fetch error")
            bids = ob.get("bids", [])
            asks = ob.get("asks", [])
            if not bids or not asks:
                return self._empty(symbol, "empty orderbook")
            best_bid = float(bids[0][0])
            best_ask = float(asks[0][0])
            if best_bid <= 0 or best_ask <= 0:
                return self._empty(symbol, "prix invalides")
            spread_pct = (best_ask - best_bid) / best_bid * 100
            bid_depth = sum(float(b[0]) * float(b[1]) for b in bids[:depth])
            ask_depth = sum(float(a[0]) * float(a[1]) for a in asks[:depth])
            total_depth = bid_depth + ask_depth
            imbalance = (bid_depth - ask_depth) / total_depth if total_depth > 0 else 0.0
            is_liquid = (
                spread_pct <= MIN_SPREAD_PCT
                and bid_depth >= MIN_DEPTH_USDT
                and ask_depth >= MIN_DEPTH_USDT
            )
            tick = (best_ask - best_bid) / 2
            recommended_entry = best_bid + tick
            base = {
                "recommended_entry_price": round(recommended_entry, 8),
                "spread_pct": round(spread_pct, 4),
                "bid_ask_imbalance": round(imbalance, 4),
                "is_liquid_enough": is_liquid,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "bid_depth_usdt": round(bid_depth, 2),
                "ask_depth_usdt": round(ask_depth, 2),
                "bids": bids,
                "asks": asks,
            }
            ob_features = {}
            try:
                ob_features = compute_orderbook_features(base)
            except Exception as e:
                logger.warning("ORDERBOOK %s compute_orderbook_features error: %r", symbol, e)
            result = {**base, **ob_features}
            result.pop("bids", None)
            result.pop("asks", None)
            if ob_features:
                try:
                    if self.memory is not None and hasattr(self.memory, "merge_features"):
                        self.memory.merge_features(symbol, ob_features)
                except Exception as e:
                    logger.warning("ORDERBOOK %s merge_features error %r", symbol, e)
            logger.info(
                "ORDERBOOK FEATURES %s spread=%.5f imb1=%.3f imb5=%.3f pressure=%s liquid=%s",
                symbol,
                float(result.get("spread_pct", 0.0) or 0.0),
                float(result.get("ob_imbalance_l1", 0.0) or 0.0),
                float(result.get("ob_imbalance_l5", 0.0) or 0.0),
                result.get("ob_pressure_state", "?"),
                result.get("is_liquid_enough", False),
            )
            return result
        except Exception as e:
            logger.warning("AgentOrderbook %s: %s", symbol, e)
            return self._empty(symbol, str(e))

    def _fetch_orderbook(self, symbol: str, depth: int = 10) -> Optional[Dict]:
        for method_name in ("get_orderbook", "get_l2", "fetch_order_book", "get_l2_snapshot"):
            method = getattr(self.client, method_name, None)
            if method is not None:
                try:
                    return method(symbol, depth=depth)
                except Exception:
                    try:
                        return method(symbol)
                    except Exception:
                        continue
        return None

    @staticmethod
    def _empty(symbol: str, reason: str) -> Dict:
        logger.debug("OB %s vide: %s", symbol, reason)
        return {
            "recommended_entry_price": None,
            "spread_pct": 999.0,
            "bid_ask_imbalance": 0.0,
            "is_liquid_enough": False,
            "best_bid": None,
            "best_ask": None,
            "bid_depth_usdt": 0.0,
            "ask_depth_usdt": 0.0,
            "ob_imbalance_l1": 0.0,
            "ob_imbalance_l5": 0.0,
            "ob_pressure_state": "unknown",
        }

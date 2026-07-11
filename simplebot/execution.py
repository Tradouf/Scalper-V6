"""
Exécution maker-first des entrées SimpleBot (P1 brief 2026-07-11).

Constat MinuteLab : l'edge brut existe mais les frais taker (0.045 % + slippage)
le mangent. Un fill maker coûte 0.015 % sans slippage — sur un aller, l'écart
représente ~0.06 % du notional, soit une part significative de l'edge/trade.

Stratégie d'exécution :
  1. limit post-only (Alo) au MID du book — Alo garantit de ne jamais prendre
     la liquidité (l'ordre est rejeté s'il croiserait, jamais exécuté taker) ;
  2. si le mid croiserait (spread d'un tick), repli : limit Alo au meilleur
     bid/ask de notre côté ;
  3. poll du book d'ordres ouverts pendant EXEC_MAKER_TIMEOUT_SEC ;
  4. timeout → cancel + market pour le reliquat (fallback taker).

Un fill partiel maker + reliquat market est compté « mixed ».

Le module est sans état : le trader passe le client, la fonction rend
{"mode": "maker"|"taker"|"mixed", "avg_px": float, "total_sz": float}.
`sleep`/`monotonic` sont injectables pour des tests sans horloge.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from simplebot import config

logger = logging.getLogger("sdm.simplebot.exec")


def _best_bid_ask(client, coin: str) -> tuple:
    """(best_bid, best_ask) depuis le snapshot L2, (None, None) si illisible."""
    try:
        book = client.get_l2_snapshot(coin)
        levels = book.get("levels") or []
        best_bid = float(levels[0][0]["px"])
        best_ask = float(levels[1][0]["px"])
        if best_bid > 0 and best_ask > best_bid:
            return best_bid, best_ask
    except Exception as e:
        logger.debug("%s: snapshot L2 illisible (%r)", coin, e)
    return None, None


def _market_entry(client, coin: str, is_buy: bool, sz: float, ref_price: float) -> dict:
    result = client.place_order(
        coin=coin, is_buy=is_buy, sz=sz, limit_px=ref_price, order_type="market",
    )
    return {
        "mode": "taker",
        "avg_px": float(result.get("avg_px") or ref_price),
        "total_sz": float(result.get("total_sz") or sz),
    }


def _position_size(client, coin: str) -> float:
    """Taille absolue de la position courante (0 si flat/illisible)."""
    try:
        for p in client.get_positions(coin=coin):
            return abs(float(p.get("szi", 0)))
    except Exception:
        pass
    return 0.0


def smart_entry(
    client,
    coin: str,
    is_buy: bool,
    sz: float,
    ref_price: float,
    timeout_sec: Optional[float] = None,
    poll_sec: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    """Entrée maker-first avec fallback market. Suppose la position FLAT au
    départ (les entrées SimpleBot se font à plat ou après clôture du flip)."""
    timeout_sec = config.EXEC_MAKER_TIMEOUT_SEC if timeout_sec is None else timeout_sec
    poll_sec = config.EXEC_POLL_SEC if poll_sec is None else poll_sec

    best_bid, best_ask = _best_bid_ask(client, coin)
    if best_bid is None:
        px_try = [ref_price]
    else:
        mid = (best_bid + best_ask) / 2.0
        join = best_bid if is_buy else best_ask   # notre côté du book
        px_try = [mid] if mid == join else [mid, join]

    # 1) pose du limit Alo (post-only) — Alo rejette au lieu de croiser
    resting = None
    px_sent = None
    for px in px_try:
        try:
            res = client.place_order(
                coin=coin, is_buy=is_buy, sz=sz,
                limit_px=px, order_type="limit", tif="Alo",
            )
        except Exception as e:
            logger.info("%s: limit Alo @%.6g rejeté (%r) — essai suivant", coin, px, e)
            continue
        px_sent = px
        if res.get("filled"):   # défensif : Alo ne devrait jamais fill immédiat
            return {
                "mode": "maker",
                "avg_px": float(res.get("avg_px") or px),
                "total_sz": float(res.get("total_sz") or sz),
            }
        if res.get("oid") is not None:
            resting = int(res["oid"])
            break

    if resting is None:
        logger.info("%s: aucun limit Alo accepté → entrée market", coin)
        return _market_entry(client, coin, is_buy, sz, ref_price)

    # 2) poll jusqu'au fill ou timeout
    deadline = monotonic() + timeout_sec
    while monotonic() < deadline:
        sleep(poll_sec)
        try:
            open_orders = client.get_open_orders(coin)
        except Exception as e:
            logger.debug("%s: get_open_orders (%r) — retry", coin, e)
            continue
        if not any(int(o.get("oid", -1)) == resting for o in open_orders):
            # l'ordre a quitté le book → rempli (maker)
            filled = _position_size(client, coin) or sz
            entry_px = px_sent or ref_price
            logger.info("%s: entrée MAKER remplie @%.6g (sz=%.6f)", coin, entry_px, filled)
            return {"mode": "maker", "avg_px": entry_px, "total_sz": filled}

    # 3) timeout → cancel + market du reliquat
    try:
        client.cancel_order(coin, resting)
    except Exception as e:
        logger.warning("%s: cancel du limit %d échoué (%r)", coin, resting, e)

    filled = _position_size(client, coin)
    remaining = max(0.0, sz - filled)
    if remaining * ref_price < 10.0:   # reliquat sous le min HL → on garde le fill partiel
        if filled > 0:
            logger.info("%s: entrée MAKER partielle @%.6g (sz=%.6f, reliquat<min)",
                        coin, px_sent, filled)
            return {"mode": "maker", "avg_px": px_sent or ref_price, "total_sz": filled}
        logger.info("%s: limit non rempli en %.0fs → entrée market", coin, timeout_sec)
        return _market_entry(client, coin, is_buy, sz, ref_price)

    taker = _market_entry(client, coin, is_buy, remaining, ref_price)
    if filled > 0:
        total = filled + taker["total_sz"]
        avg = ((px_sent or ref_price) * filled + taker["avg_px"] * taker["total_sz"]) / total
        logger.info("%s: entrée MIXTE maker %.6f + taker %.6f", coin, filled, taker["total_sz"])
        return {"mode": "mixed", "avg_px": avg, "total_sz": total}
    logger.info("%s: limit non rempli en %.0fs → entrée market", coin, timeout_sec)
    return taker

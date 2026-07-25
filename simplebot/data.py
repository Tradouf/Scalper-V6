"""
Récupération OHLCV Hyperliquid (endpoint public /info, pas de wallet requis).
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

import requests

logger = logging.getLogger("sdm.simplebot.data")

HL_INFO_URL = "https://api.hyperliquid.xyz/info"


def fetch_ohlcv(
    symbol: str,
    interval: str,
    days: float,
    end_ms: Optional[int] = None,
    timeout: float = 10.0,
) -> List[dict]:
    """
    Retourne les bougies triées par ts croissant :
    [{"ts", "open", "high", "low", "close", "volume"}, ...]
    La dernière bougie peut être EN COURS (non clôturée) — à filtrer côté appelant.
    """
    now_ms = int(time.time() * 1000)
    if end_ms is None:
        end_ms = now_ms
    start_ms = end_ms - int(days * 24 * 3600 * 1000)

    # Cache disque partagé inter-processus (SimpleBot + SuperBot + outils sur
    # la même IP) — voir hl_ohlcv_cache. Best-effort, jamais bloquant.
    from hl_ohlcv_cache import cache_get, cache_put

    cached = cache_get(symbol, interval, start_ms, end_ms, now_ms)
    if cached is not None:
        return cached

    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
        },
    }

    # Import paresseux : config appelle data au chargement (mode ALL) → un import
    # top-level serait circulaire.
    from simplebot import config

    attempts = max(1, config.FETCH_MAX_RETRIES)
    raw = None
    from hl_rate_limit import throttle_before_hl_request

    for attempt in range(1, attempts + 1):
        try:
            throttle_before_hl_request()
            resp = requests.post(
                HL_INFO_URL,
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            raw = resp.json()
            break
        except requests.exceptions.RequestException as e:
            # 429 (throttle) et 5xx sont transitoires → backoff exponentiel.
            # Les erreurs réseau (timeout, connexion coupée) le sont aussi.
            status = getattr(getattr(e, "response", None), "status_code", None)
            retryable = status is None or status == 429 or 500 <= status < 600
            if retryable and attempt < attempts:
                delay = config.FETCH_BACKOFF_SEC * (2 ** (attempt - 1))
                logger.warning(
                    "fetch_ohlcv %s/%s: %s — retry %d/%d dans %.1fs",
                    symbol, interval, status or type(e).__name__,
                    attempt, attempts, delay,
                )
                time.sleep(delay)
                continue
            logger.warning("fetch_ohlcv %s/%s: %r", symbol, interval, e)
            return []
        except Exception as e:  # JSON illisible, réponse inattendue…
            logger.warning("fetch_ohlcv %s/%s: %r", symbol, interval, e)
            return []
    if raw is None:
        return []

    candles = [
        {
            "ts": int(c["t"]),
            "open": float(c["o"]),
            "high": float(c["h"]),
            "low": float(c["l"]),
            "close": float(c["c"]),
            "volume": float(c["v"]),
        }
        for c in raw
    ]
    candles.sort(key=lambda c: c["ts"])
    if end_ms >= now_ms - 1000:           # seules les fenêtres « jusqu'à maintenant »
        cache_put(symbol, interval, candles, now_ms)
    return candles


def fetch_perp_universe(
    top_n: Optional[int] = None,
    include_delisted: bool = False,
    timeout: float = 10.0,
) -> List[str]:
    """
    Retourne les noms de perps de l'univers Hyperliquid, TRIÉS par volume
    notionnel 24h décroissant. Endpoint public /info (metaAndAssetCtxs),
    aucun wallet requis.

    - exclut les actifs délistés (isDelisted) — intradables ;
    - si top_n est fourni, ne garde que les top_n plus liquides : filtre
      anti-micro-cap (les books trop fins ont un slippage réel ingérable).

    Lève une exception en cas d'échec réseau : l'appelant gère le fallback.
    """
    from hl_rate_limit import throttle_before_hl_request

    throttle_before_hl_request()
    resp = requests.post(
        HL_INFO_URL,
        headers={"Content-Type": "application/json"},
        json={"type": "metaAndAssetCtxs"},
        timeout=timeout,
    )
    resp.raise_for_status()
    meta, ctxs = resp.json()
    universe = meta.get("universe", [])

    ranked = []
    for u, c in zip(universe, ctxs):
        name = u.get("name")
        if not name or (u.get("isDelisted") and not include_delisted):
            continue
        vol = float(c.get("dayNtlVlm", 0) or 0)   # volume notionnel 24h ($)
        ranked.append((name, vol))
    ranked.sort(key=lambda x: x[1], reverse=True)

    names = [name for name, _ in ranked]
    if top_n is not None and top_n > 0:
        names = names[:top_n]
    return names


def fetch_funding_rates(timeout: float = 10.0) -> dict:
    """{coin: taux de funding HORAIRE courant} via metaAndAssetCtxs (public).
    Positif = les longs paient. Lève en cas d'échec réseau (l'appelant gère)."""
    from hl_rate_limit import throttle_before_hl_request

    throttle_before_hl_request()
    resp = requests.post(
        HL_INFO_URL,
        headers={"Content-Type": "application/json"},
        json={"type": "metaAndAssetCtxs"},
        timeout=timeout,
    )
    resp.raise_for_status()
    meta, ctxs = resp.json()
    out = {}
    for u, c in zip(meta.get("universe", []), ctxs):
        name = u.get("name")
        if not name:
            continue
        try:
            out[name] = float(c.get("funding", 0) or 0)
        except (TypeError, ValueError):
            continue
    return out


def fetch_ledger_updates(address: str, start_ms: int, timeout: float = 10.0) -> List[dict]:
    """Mouvements non-funding du compte (dépôts, retraits, transferts) depuis
    start_ms — endpoint public userNonFundingLedgerUpdates, adresse seule.
    Sert au kill-switch pour distinguer un RETRAIT d'une perte de trading."""
    from hl_rate_limit import throttle_before_hl_request

    throttle_before_hl_request()
    resp = requests.post(
        HL_INFO_URL,
        headers={"Content-Type": "application/json"},
        json={"type": "userNonFundingLedgerUpdates", "user": address,
              "startTime": int(start_ms)},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json() or []


def net_transfer_flow(updates: List[dict], address: str) -> float:
    """Flux net (USDC) des mouvements EXTERNES du compte : dépôts/entrées > 0,
    retraits/sorties < 0. Les transferts internes spot<->perp (accountClassTransfer)
    sont ignorés : ils ne changent pas la valeur totale du compte."""
    addr = (address or "").lower()
    net = 0.0
    for u in updates:
        d = u.get("delta") or {}
        typ = d.get("type", "")
        amount = 0.0
        for key in ("usdc", "amount", "sz"):
            try:
                amount = abs(float(d.get(key)))
                break
            except (TypeError, ValueError):
                continue
        if amount <= 0:
            continue
        if typ == "deposit":
            net += amount
        elif typ == "withdraw":
            net -= amount
        elif typ in ("send", "spotTransfer", "subAccountTransfer", "internalTransfer"):
            dest = str(d.get("destination", "")).lower()
            src = str(d.get("user", "")).lower()
            if dest == addr and src != addr:
                net += amount
            elif src == addr and dest != addr:
                net -= amount
    return net


def closed_candles(candles: List[dict], interval_ms: int, now_ms: Optional[int] = None) -> List[dict]:
    """Ne garde que les bougies dont la fenêtre est terminée."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return [c for c in candles if c["ts"] + interval_ms <= now_ms]

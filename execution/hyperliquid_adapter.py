"""
HyperliquidReadAdapter — lecture seule de l'état HL.

Pour le paper trading (P7) et le live (P8). Ne fait jamais d'ordres.
Utilisé pour :
  - alimenter PaperExchange en mark prices live
  - lire les positions courantes (pour réconcilier au boot)
  - récupérer les candles 1h pour les stratégies

Implémentation : appels HL info API directement via requests (pas besoin du
SDK complet pour la partie read-only).
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Dict, List, Optional

import requests

from core.types import Candle

logger = logging.getLogger("v7.hl_adapter")

HL_API = "https://api.hyperliquid.xyz/info"


class HyperliquidReadAdapter:
    """Lecture seule HL via API info."""

    # Fix 6 (port V6 30/05) : back-off / throttle quand l'API HL est instable.
    # Évite la tempête de retries + WARNINGs (~10800/36h observés en V6 lors
    # d'épisodes RemoteDisconnected). En V7, les points d'appel sont moins
    # nombreux (1 tick / 30s) mais une succession d'erreurs allMids + candles
    # peut quand même spammer les logs. On applique le même principe.
    _MIDS_FAIL_COOLDOWN_SEC = 5.0    # ne pas re-tenter avant T+cooldown après un échec
    _WARN_THROTTLE_SEC = 30.0        # 1 WARNING / fenêtre, agrégation du compteur

    def __init__(self, account_address: Optional[str] = None, timeout: float = 5.0) -> None:
        self._addr = account_address
        self._timeout = timeout
        # Cache courte durée pour les mids
        self._mids_cache: Dict[str, float] = {}
        self._mids_ts: float = 0.0
        self._mids_ttl = 2.0  # 2s
        # Back-off / circuit-breaker (Fix 6 port V6)
        self._mids_failed_until: float = 0.0
        self._warn_last_ts: float = 0.0
        self._warn_suppressed: int = 0

    # ─── Prix ─────────────────────────────────────────────────────────────────

    def get_mark_price(self, coin: str) -> float:
        self._refresh_mids_if_stale()
        return self._mids_cache.get(coin.upper(), 0.0)

    def get_all_mids(self) -> Dict[str, float]:
        self._refresh_mids_if_stale()
        return dict(self._mids_cache)

    def _log_throttled_warning(self, msg: str, *args) -> None:
        """Émet un WARNING au max 1 fois par _WARN_THROTTLE_SEC. Les occurrences
        avalées sont comptées et reportées dans le prochain message."""
        now = time.time()
        if now - self._warn_last_ts < self._WARN_THROTTLE_SEC:
            self._warn_suppressed += 1
            return
        suffix = ""
        if self._warn_suppressed > 0:
            suffix = " (+%d avalées en %.0fs)" % (
                self._warn_suppressed, self._WARN_THROTTLE_SEC,
            )
        logger.warning(msg + suffix, *args)
        self._warn_last_ts = now
        self._warn_suppressed = 0

    def _refresh_mids_if_stale(self) -> None:
        now = time.time()
        if now - self._mids_ts < self._mids_ttl:
            return
        # Back-off : si le dernier refresh a échoué récemment, ne pas re-tenter.
        # Le tick suivant retentera (le cache reste utilisable côté lecteur).
        if now < self._mids_failed_until:
            return
        try:
            r = requests.post(HL_API, json={"type": "allMids"}, timeout=self._timeout)
            r.raise_for_status()
            data = r.json()
            new_cache: Dict[str, float] = {}
            for coin, px in data.items():
                try:
                    new_cache[str(coin).upper()] = float(px)
                except Exception:
                    continue
            self._mids_cache = new_cache
            self._mids_ts = now
            self._mids_failed_until = 0.0
        except Exception as e:
            self._mids_failed_until = now + self._MIDS_FAIL_COOLDOWN_SEC
            self._log_throttled_warning("HL allMids refresh error: %r", e)

    # ─── Positions ───────────────────────────────────────────────────────────

    def get_positions(self) -> Dict[str, float]:
        """Retourne {asset → notional signé} du compte HL.

        Notional = qty × mark_price_current. Signé selon szi.
        """
        if not self._addr:
            return {}
        try:
            r = requests.post(
                HL_API,
                json={"type": "clearinghouseState", "user": self._addr},
                timeout=self._timeout,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            self._log_throttled_warning("HL clearinghouseState error: %r", e)
            return {}

        out: Dict[str, float] = {}
        mids = self.get_all_mids()
        for ap in data.get("assetPositions", []):
            pos = ap.get("position", ap)
            coin = str(pos.get("coin", "")).upper()
            szi = float(pos.get("szi", 0) or 0)
            if not coin or szi == 0:
                continue
            mark = mids.get(coin, 0.0)
            if mark <= 0:
                # Fallback : utilise entryPx
                mark = float(pos.get("entryPx", 0) or 0)
            out[coin] = szi * mark  # signed notional
        return out

    def get_positions_detailed(self) -> Dict[str, dict]:
        """Retourne {asset → {szi, entry_px, leverage, mark_px, roe}} pour le
        risk monitoring (EmergencyExitManager). ROE calculé sur prix :
          ROE = (mark - entry)/entry × leverage × sign(szi)
        Avec sign(szi) : -1 si short (les pertes augmentent quand mark monte),
        +1 si long.
        """
        if not self._addr:
            return {}
        try:
            r = requests.post(
                HL_API,
                json={"type": "clearinghouseState", "user": self._addr},
                timeout=self._timeout,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            self._log_throttled_warning("HL clearinghouseState detailed error: %r", e)
            return {}

        out: Dict[str, dict] = {}
        mids = self.get_all_mids()
        for ap in data.get("assetPositions", []):
            pos = ap.get("position", ap)
            coin = str(pos.get("coin", "")).upper()
            szi = float(pos.get("szi", 0) or 0)
            if not coin or szi == 0:
                continue
            entry_px = float(pos.get("entryPx", 0) or 0)
            mark_px = mids.get(coin, entry_px)
            lev_raw = pos.get("leverage", {})
            leverage = (
                float(lev_raw.get("value", 1)) if isinstance(lev_raw, dict)
                else float(lev_raw or 1)
            )
            roe = 0.0
            if entry_px > 0 and mark_px > 0:
                price_change_pct = (mark_px - entry_px) / entry_px
                # szi>0=long: +price → +ROE ; szi<0=short: +price → -ROE
                sign = 1.0 if szi > 0 else -1.0
                roe = price_change_pct * leverage * sign
            out[coin] = {
                "szi": szi,
                "entry_px": entry_px,
                "mark_px": mark_px,
                "leverage": leverage,
                "roe": roe,
                "side": "BUY" if szi > 0 else "SELL",
            }
        return out

    def get_equity(self) -> float:
        """Equity totale (spot USDC, en compte unifié)."""
        if not self._addr:
            return 0.0
        try:
            r = requests.post(
                HL_API,
                json={"type": "spotClearinghouseState", "user": self._addr},
                timeout=self._timeout,
            )
            r.raise_for_status()
            data = r.json()
            for b in data.get("balances", []) or []:
                if str(b.get("coin", "")).upper() == "USDC":
                    return float(b.get("total", 0) or 0)
        except Exception as e:
            self._log_throttled_warning("HL spot equity error: %r", e)
        return 0.0

    # ─── Candles ──────────────────────────────────────────────────────────────

    def get_candles(self, coin: str, interval: str = "1h", limit: int = 200) -> List[Candle]:
        """Récupère les dernières N candles via candleSnapshot.

        HL accepte (startTime, endTime). On calcule la fenêtre depuis maintenant.
        """
        end_ms = int(time.time() * 1000)
        # interval → ms
        interval_ms = {
            "1m": 60_000, "5m": 5 * 60_000, "15m": 15 * 60_000,
            "30m": 30 * 60_000, "1h": 3600_000, "4h": 4 * 3600_000,
            "1d": 24 * 3600_000,
        }.get(interval, 3600_000)
        start_ms = end_ms - limit * interval_ms
        try:
            r = requests.post(
                HL_API,
                json={
                    "type": "candleSnapshot",
                    "req": {
                        "coin": coin.upper(),
                        "interval": interval,
                        "startTime": start_ms,
                        "endTime": end_ms,
                    },
                },
                timeout=self._timeout,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            self._log_throttled_warning(
                "HL candles %s %s error: %r", coin, interval, e,
            )
            return []

        if not isinstance(data, list):
            return []

        candles: List[Candle] = []
        for row in data:
            try:
                ts_ms = int(row.get("t", 0))
                candles.append(Candle(
                    ts_open=dt.datetime.utcfromtimestamp(ts_ms / 1000.0),
                    open=float(row.get("o", 0)),
                    high=float(row.get("h", 0)),
                    low=float(row.get("l", 0)),
                    close=float(row.get("c", 0)),
                    volume=float(row.get("v", 0) or 0),
                ))
            except Exception:
                continue
        candles.sort(key=lambda c: c.ts_open)
        return candles

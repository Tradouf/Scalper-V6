"""
SimpleLiveTrader — exécution live de la stratégie sur Hyperliquid,
avec un SECOND wallet, totalement indépendant du bot V6.

Sécurités :
- DRY-RUN par défaut (SIMPLEBOT_DRY_RUN=0 requis pour trader réellement) ;
- refuse de démarrer si le wallet HL2 est le même que celui de la V6
  (HL_ACCOUNT_ADDRESS / HL_PRIVATE_KEY) ;
- n'agit qu'une seule fois par bougie clôturée et par symbole
  (état persisté → pas de double ordre après restart) ;
- TP/SL NATIFS posés sur l'exchange dès l'entrée : un crash du bot ne
  laisse jamais une position sans protection (contrairement au trail
  logiciel de la V6) ;
- ne touche qu'aux symboles marqués `active` par l'optimiseur.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from simplebot import config
from simplebot.data import closed_candles, fetch_ohlcv
from simplebot.strategy import StrategyParams, latest_signal

logger = logging.getLogger("sdm.simplebot.live")


# ── Second wallet ────────────────────────────────────────────────────────────

def make_second_wallet_client():
    """
    Client Hyperliquid authentifié sur le wallet SimpleBot (HL2_*).
    Lève RuntimeError si la clé manque ou si le wallet est celui de la V6.
    """
    from hyperliquid_client import HyperliquidClient

    key = os.environ.get(config.ENV_PRIVATE_KEY)
    if not key:
        raise RuntimeError(
            f"{config.ENV_PRIVATE_KEY} manquant — SimpleBot exige un wallet séparé de la V6"
        )
    addr = os.environ.get(config.ENV_ACCOUNT_ADDRESS) or None

    client = HyperliquidClient(wallet_key=key)
    if client.exchange is None:
        raise RuntimeError("Échec d'initialisation du wallet SimpleBot (clé invalide ?)")
    # Wallet API (agent) signant pour un compte maître distinct
    if addr and addr.lower() != (client.wallet_address or "").lower():
        client._init_exchange(key, account_address=addr)

    _assert_not_main_wallet(key, client.wallet_address or "")
    logger.info("Wallet SimpleBot: %s...%s", client.wallet_address[:6], client.wallet_address[-4:])
    return client


def _assert_not_main_wallet(key: str, address: str) -> None:
    main_addr = os.environ.get("HL_ACCOUNT_ADDRESS", "")
    if main_addr and main_addr.lower() == address.lower():
        raise RuntimeError(
            "Le wallet HL2 est identique au wallet du bot V6 — refus de démarrer. "
            "Créez un wallet dédié pour SimpleBot."
        )
    main_key = os.environ.get("HL_PRIVATE_KEY", "")
    if main_key and main_key.lower() == key.lower():
        raise RuntimeError(
            "HL2_PRIVATE_KEY est identique à HL_PRIVATE_KEY — refus de démarrer."
        )


# ── Paramètres publiés par l'optimiseur (rechargés à chaud) ──────────────────

class ParamStore:
    def __init__(self, path: Path = None):
        self.path = path or config.BEST_PARAMS_FILE
        self._mtime = 0.0
        self._state: dict = {}

    def maybe_reload(self) -> bool:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        if mtime == self._mtime:
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._state = json.load(f)
            self._mtime = mtime
            logger.info("Paramètres rechargés (updated_at=%s)", self._state.get("updated_at"))
            return True
        except Exception as e:
            logger.warning("Lecture %s échouée: %r", self.path, e)
            return False

    def active_params(self, symbol: str) -> Optional[StrategyParams]:
        entry = self._state.get("symbols", {}).get(symbol)
        if not entry or not entry.get("active"):
            return None
        try:
            return StrategyParams.from_dict(entry["params"])
        except Exception:
            return None

    @property
    def symbols(self) -> list:
        return list(self._state.get("symbols", {}).keys())


# ── Trader ───────────────────────────────────────────────────────────────────

class SimpleLiveTrader:

    def __init__(self, client=None, store: ParamStore = None, dry_run: bool = None,
                 fetch=None):
        self.dry_run = config.DRY_RUN if dry_run is None else dry_run
        self.client = client
        self.store = store or ParamStore()
        self._fetch = fetch or fetch_ohlcv
        self._live_state = self._load_live_state()

    # état persistant : dernière bougie traitée par symbole
    def _load_live_state(self) -> dict:
        try:
            with open(config.LIVE_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"last_ts": {}}

    def _save_live_state(self) -> None:
        try:
            config.LIVE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = config.LIVE_STATE_FILE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._live_state, f, indent=2)
            os.replace(tmp, config.LIVE_STATE_FILE)
        except Exception as e:
            logger.warning("Sauvegarde live_state échouée: %r", e)

    # ── Boucle principale ────────────────────────────────────────────────────

    def run_forever(self) -> None:
        mode = "DRY-RUN (aucun ordre envoyé)" if self.dry_run else "LIVE ⚠️ ordres réels"
        logger.info("SimpleLiveTrader démarré — %s — intervalle %s", mode, config.INTERVAL)
        while True:
            try:
                self.tick()
            except Exception as e:
                logger.error("Tick en erreur: %r", e, exc_info=True)
            time.sleep(config.LOOP_SEC)

    def tick(self) -> None:
        self.store.maybe_reload()
        for symbol in self.store.symbols:
            params = self.store.active_params(symbol)
            if params is None:
                continue
            try:
                self._process_symbol(symbol, params)
            except Exception as e:
                logger.error("Traitement %s en erreur: %r", symbol, e, exc_info=True)

    def _process_symbol(self, symbol: str, params: StrategyParams) -> None:
        # ~4× le warmup en bougies, converti en jours
        bars_needed = params.warmup_bars * 4
        days = max(1.0, bars_needed * config.INTERVAL_MS / 86_400_000)
        candles = closed_candles(
            self._fetch(symbol, config.INTERVAL, days), config.INTERVAL_MS
        )
        if len(candles) < params.warmup_bars + 1:
            logger.warning("%s: données insuffisantes (%d bougies)", symbol, len(candles))
            return

        last_ts = candles[-1]["ts"]
        if self._live_state["last_ts"].get(symbol) == last_ts:
            return  # bougie déjà traitée

        sig = latest_signal(candles, params)
        # bougie marquée traitée quoi qu'il arrive (une décision par bougie)
        self._live_state["last_ts"][symbol] = last_ts
        self._save_live_state()

        if sig["signal"] == 0:
            return

        direction = sig["signal"]
        current = self._current_position(symbol)

        if current is not None and current * direction > 0:
            logger.info("%s: signal %+d mais position déjà dans le sens — rien à faire",
                        symbol, direction)
            return

        if current is not None and current * direction < 0:
            logger.info("%s: signal %+d opposé à la position → flip", symbol, direction)
            self._close_position(symbol)

        if current is None and self._open_positions_count() >= config.MAX_OPEN_POSITIONS:
            logger.info("%s: signal %+d ignoré — MAX_OPEN_POSITIONS atteint", symbol, direction)
            return

        self._open_position(symbol, direction, sig["close"], sig["atr"])

    # ── Lecture des positions ────────────────────────────────────────────────

    def _current_position(self, symbol: str) -> Optional[float]:
        """szi signé de la position ouverte, None si flat (ou en dry-run)."""
        if self.dry_run or self.client is None:
            return None
        for p in self.client.get_positions(coin=symbol):
            szi = float(p.get("szi", 0))
            if abs(szi) > 0:
                return szi
        return None

    def _open_positions_count(self) -> int:
        if self.dry_run or self.client is None:
            return 0
        return len([p for p in self.client.get_positions() if abs(float(p.get("szi", 0))) > 0])

    # ── Exécution ────────────────────────────────────────────────────────────

    def _close_position(self, symbol: str) -> None:
        if self.dry_run:
            logger.info("[DRY-RUN] %s: cancel ordres + market_close", symbol)
            return
        try:
            self.client.cancel_all_orders(symbol)   # purge TP/SL natifs orphelins
        except Exception as e:
            logger.warning("%s: cancel_all_orders: %r", symbol, e)
        self.client.market_close(symbol)
        logger.info("%s: position clôturée (market)", symbol)

    def _open_position(self, symbol: str, direction: int, ref_price: float, atr_val: float) -> None:
        side = "LONG" if direction == 1 else "SHORT"
        params = self.store.active_params(symbol)
        if atr_val <= 0 or ref_price <= 0 or params is None:
            logger.warning("%s: ATR/prix invalide, entrée annulée", symbol)
            return

        sl_price = ref_price - direction * params.sl_atr * atr_val
        tp_price = ref_price + direction * params.tp_atr * atr_val

        if self.dry_run:
            logger.info(
                "[DRY-RUN] %s: OPEN %s @~%.6g | TP=%.6g SL=%.6g (ATR=%.6g, params=%s)",
                symbol, side, ref_price, tp_price, sl_price, atr_val, params.to_dict(),
            )
            return

        account_value = self.client.get_account_value()
        margin = account_value * config.MARGIN_PCT
        notional = max(config.MIN_NOTIONAL_USD, margin * config.LEVERAGE)
        qty = notional / ref_price

        self.client.update_leverage(symbol, config.LEVERAGE, is_cross=False)
        result = self.client.place_order(
            coin=symbol,
            is_buy=(direction == 1),
            sz=qty,
            limit_px=ref_price,
            order_type="market",
        )
        fill_px = float(result.get("avg_px") or ref_price)
        fill_sz = float(result.get("total_sz") or qty)
        logger.info("%s: OPEN %s sz=%.6f @ %.6g (notional≈$%.2f, lev=%dx)",
                    symbol, side, fill_sz, fill_px, fill_sz * fill_px, config.LEVERAGE)

        # TP/SL natifs recalés sur le prix de fill réel
        sl_price = fill_px - direction * params.sl_atr * atr_val
        tp_price = fill_px + direction * params.tp_atr * atr_val
        try:
            self.client.place_position_tpsl(
                coin=symbol,
                is_long=(direction == 1),
                sz=fill_sz,
                tp_price=tp_price,
                sl_price=sl_price,
            )
            logger.info("%s: TP/SL natifs posés TP=%.6g SL=%.6g", symbol, tp_price, sl_price)
        except Exception as e:
            # sans protection → on referme immédiatement
            logger.error("%s: pose TP/SL échouée (%r) → fermeture de sécurité", symbol, e)
            try:
                self.client.market_close(symbol)
            except Exception as e2:
                logger.critical("%s: FERMETURE DE SÉCURITÉ ÉCHOUÉE: %r — POSITION SANS SL !",
                                symbol, e2)

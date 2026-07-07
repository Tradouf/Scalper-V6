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
- RÉCONCILIATION au démarrage : toute position trouvée sans SL natif
  (crash entre l'ouverture et la pose du TP/SL) est re-protégée, ou
  fermée si impossible ;
- KILL-SWITCH : si l'account value perd KILL_LOSS_PCT vs son pic sur
  KILL_WINDOW_SEC, tout est fermé et le trading est mis en pause ;
- en DRY-RUN, les positions sont simulées (papier) : entrées, TP/SL et
  flips sont rejoués sur les bougies suivantes et le PnL cumulé est logué
  → le dry-run donne un vrai verdict chiffré avant de passer live ;
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
from simplebot.strategy import StrategyParams, atr, latest_signal

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

    # état persistant : dernière bougie traitée, positions papier, kill-switch
    def _load_live_state(self) -> dict:
        try:
            with open(config.LIVE_STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = {}
        state.setdefault("last_ts", {})
        state.setdefault("paper", {"positions": {}, "trades": []})
        state.setdefault("equity_history", [])
        state.setdefault("paused_until", 0)
        return state

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
        self.reconcile_positions()
        while True:
            try:
                self.tick()
            except Exception as e:
                logger.error("Tick en erreur: %r", e, exc_info=True)
            time.sleep(config.LOOP_SEC)

    def tick(self) -> None:
        self.store.maybe_reload()
        if self._kill_switch_engaged():
            return
        for symbol in self.store.symbols:
            params = self.store.active_params(symbol)
            if params is None:
                continue
            try:
                self._process_symbol(symbol, params)
            except Exception as e:
                logger.error("Traitement %s en erreur: %r", symbol, e, exc_info=True)

    # ── Sécurités live ───────────────────────────────────────────────────────

    def reconcile_positions(self) -> None:
        """
        Au démarrage live : toute position ouverte doit avoir un SL natif.
        Un crash entre place_order et place_position_tpsl peut laisser une
        position nue — on la re-protège, ou on la ferme si c'est impossible.
        """
        if self.dry_run or self.client is None:
            return
        self.store.maybe_reload()
        try:
            positions = self.client.get_positions()
        except Exception as e:
            logger.error("Réconciliation: lecture des positions impossible: %r", e)
            return
        for p in positions:
            coin = p["coin"]
            szi = float(p.get("szi", 0))
            if abs(szi) <= 0:
                continue
            try:
                orders = self.client.get_open_orders(coin)
            except Exception as e:
                logger.error("Réconciliation %s: lecture des ordres impossible: %r", coin, e)
                continue
            has_sl = any(
                o.get("isTrigger") and o.get("tpsl") == "sl" and o.get("reduceOnly")
                for o in orders
            )
            if has_sl:
                logger.info("Réconciliation %s: SL natif présent — OK", coin)
                continue
            logger.warning("Réconciliation %s: position ouverte SANS SL natif → réparation", coin)
            self._protect_or_close(coin, szi, float(p.get("entry_px", 0) or 0))

    def _protect_or_close(self, coin: str, szi: float, entry_px: float) -> None:
        """Repose un TP/SL natif sur une position nue ; la ferme sinon."""
        direction = 1 if szi > 0 else -1
        params = self.store.active_params(coin)
        atr_val = 0.0
        ref = entry_px
        if params is not None:
            bars_needed = params.warmup_bars * 4
            days = max(1.0, bars_needed * config.INTERVAL_MS / 86_400_000)
            candles = closed_candles(
                self._fetch(coin, config.INTERVAL, days), config.INTERVAL_MS
            )
            if len(candles) >= params.warmup_bars + 1:
                atr_val = atr(candles, params.atr_len)[-1]
                if ref <= 0:
                    ref = candles[-1]["close"]

        if params is None or atr_val <= 0 or ref <= 0:
            logger.error("%s: SL non recalculable (params/ATR indisponibles) → fermeture", coin)
            self._safety_close(coin)
            return

        sl_price = ref - direction * params.sl_atr * atr_val
        tp_price = ref + direction * params.tp_atr * atr_val
        try:
            self.client.cancel_all_orders(coin)  # purge un éventuel TP orphelin
            self.client.place_position_tpsl(
                coin=coin,
                is_long=(direction == 1),
                sz=abs(szi),
                tp_price=tp_price,
                sl_price=sl_price,
            )
            logger.info("%s: position re-protégée TP=%.6g SL=%.6g", coin, tp_price, sl_price)
        except Exception as e:
            logger.error("%s: re-protection échouée (%r) → fermeture", coin, e)
            self._safety_close(coin)

    def _safety_close(self, coin: str) -> None:
        try:
            self.client.cancel_all_orders(coin)
        except Exception as e:
            logger.warning("%s: cancel_all_orders: %r", coin, e)
        try:
            self.client.market_close(coin)
            logger.info("%s: position fermée (sécurité)", coin)
        except Exception as e:
            logger.critical("%s: FERMETURE DE SÉCURITÉ ÉCHOUÉE: %r — POSITION SANS SL !", coin, e)

    def _account_value(self) -> float:
        """Valeur de compte pour le kill-switch ET le sizing : collatéral perp
        + solde spot USDC (sur HL les deux sont séparés ; un solde logé en spot
        ferait lire ~0 au perp et déclencherait un faux kill-switch)."""
        perp = self.client.get_account_value()  # peut lever → fail-safe kill-switch
        if not config.COUNT_SPOT_IN_EQUITY:
            return perp
        # Une lecture spot en échec ne doit JAMAIS devenir « spot=0 » : sur ce
        # setup le capital vit en spot, et perp seul = résidu fantôme → l'incident
        # du 2026-07-04 08:16 (429 sur spot → 19.96 lu → faux kill-switch qui a
        # tout fermé). On PROPAGE l'erreur : le fail-safe de _kill_switch_engaged
        # gèle les entrées après N échecs au lieu de décider sur un chiffre faux.
        spot = self.client.get_spot_usdc()
        total = perp + spot
        # Garde-fou : HL renvoie parfois un accountValue perp résiduel/fantôme (dust
        # non adossé à du capital réel, cohérence différée) qui gonfle faussement la
        # somme perp+spot — au point d'avoir déclenché de faux kill-switch au reflux
        # du pic fantôme. On plafonne la somme par la valeur canonique du compte
        # (portfolio), qui nette perp+spot côté HL. Clamp à sens unique (baisse) :
        # sûr pour le kill-switch ET le sizing.
        if config.EQUITY_CANON_TOL > 0:
            # Comme pour le spot : un échec de lecture PROPAGE. Retomber sur la
            # somme brute laisserait entrer un pic fantôme (+5 à +15 % observés)
            # dans equity_history, d'où faux kill au retour du clamp.
            canon = self.client.get_portfolio_value()
            if canon > 0 and total > canon * (1 + config.EQUITY_CANON_TOL):
                logger.info(
                    "Equity: somme perp+spot %.2f > canon %.2f (+%.1f%%) — clamp sur "
                    "la valeur canonique HL (résidu perp fantôme)",
                    total, canon, (total / canon - 1) * 100,
                )
                return canon
        return total

    def _ensure_perp_margin(self, required_margin: float) -> None:
        """Pour trader des perps, la marge doit être dans le perp. Si le collatéral
        perp est insuffisant, on vire le manque (× tampon) depuis le spot."""
        if not config.AUTO_FUND_PERP or required_margin <= 0:
            return
        try:
            perp = self.client.get_account_value()
        except Exception as e:
            logger.warning("Top-up perp: lecture perp échouée (%r)", e)
            return
        if perp >= required_margin:
            return
        try:
            spot = self.client.get_spot_usdc()
        except Exception as e:
            logger.warning("Top-up perp: lecture spot échouée (%r)", e)
            return
        amount = min(required_margin * config.PERP_FUND_BUFFER - perp, spot)
        if amount <= 0:
            logger.warning("Top-up perp impossible: perp=%.2f spot=%.2f besoin marge=%.2f",
                           perp, spot, required_margin)
            return
        logger.info("Top-up perp: transfert %.2f USDC spot→perp (perp=%.2f, besoin marge=%.2f)",
                    amount, perp, required_margin)
        self.client.transfer_spot_to_perp(amount)

    def _kill_switch_engaged(self) -> bool:
        """
        True si le trading est en pause. Suit l'account value en fenêtre
        glissante ; en cas de perte > KILL_LOSS_PCT vs le pic, ferme tout
        et met le trading en pause KILL_PAUSE_SEC.
        """
        if self.dry_run or self.client is None:
            return False
        now = time.time()
        paused_until = float(self._live_state.get("paused_until", 0))
        if now < paused_until:
            if now - getattr(self, "_last_pause_log", 0) > 600:
                self._last_pause_log = now
                logger.warning("Kill-switch actif — reprise dans %.0f min",
                               (paused_until - now) / 60)
            return True

        try:
            account_value = self._account_value()
        except Exception as e:
            # Fail-safe : après N échecs consécutifs on gèle les entrées plutôt
            # que d'ignorer le check (les positions gardent leur TP/SL natif).
            self._acct_read_failures = getattr(self, "_acct_read_failures", 0) + 1
            if self._acct_read_failures >= config.KILL_MAX_READ_FAILURES:
                logger.critical(
                    "Kill-switch: account value illisible %d cycles consécutifs (%r) "
                    "— GEL des entrées jusqu'au rétablissement",
                    self._acct_read_failures, e,
                )
                return True
            logger.warning(
                "Kill-switch: account value illisible (%r) — échec %d/%d avant gel",
                e, self._acct_read_failures, config.KILL_MAX_READ_FAILURES,
            )
            return False
        self._acct_read_failures = 0
        if account_value <= 0:
            return False

        hist = self._live_state["equity_history"]
        if not hist or now - hist[-1][0] >= 300:  # un point max toutes les 5 min
            hist.append([now, account_value])
        cutoff = now - config.KILL_WINDOW_SEC
        hist[:] = [pt for pt in hist if pt[0] >= cutoff]
        peak = max(v for _, v in hist)

        if peak > 0 and account_value <= peak * (1 - config.KILL_LOSS_PCT):
            logger.critical(
                "KILL-SWITCH: account value %.2f ≤ pic %.2f × (1-%.1f%%) — "
                "fermeture de toutes les positions, pause %dh",
                account_value, peak, config.KILL_LOSS_PCT * 100,
                config.KILL_PAUSE_SEC // 3600,
            )
            self._emergency_flatten()
            self._live_state["paused_until"] = now + config.KILL_PAUSE_SEC
            self._save_live_state()
            return True

        self._save_live_state()
        return False

    def _emergency_flatten(self) -> None:
        try:
            positions = self.client.get_positions()
        except Exception as e:
            logger.critical("Kill-switch: lecture des positions impossible: %r", e)
            positions = []
        for p in positions:
            self._safety_close(p["coin"])
        try:
            self.client.cancel_all_orders()
        except Exception as e:
            logger.warning("Kill-switch: cancel_all_orders global: %r", e)

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

        # dry-run : rejouer les TP/SL papier sur les bougies apparues depuis
        if self.dry_run:
            self._paper_check_exits(symbol, candles)

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
            self._close_position(symbol, ref_price=sig["close"], ts=sig["ts"])

        if current is None and self._open_positions_count() >= config.MAX_OPEN_POSITIONS:
            logger.info("%s: signal %+d ignoré — MAX_OPEN_POSITIONS atteint", symbol, direction)
            return

        self._open_position(symbol, direction, sig["close"], sig["atr"], ts=sig["ts"])

    # ── Lecture des positions ────────────────────────────────────────────────

    def _current_position(self, symbol: str) -> Optional[float]:
        """szi signé de la position ouverte (±1 papier en dry-run), None si flat."""
        if self.dry_run or self.client is None:
            pos = self._live_state["paper"]["positions"].get(symbol)
            return float(pos["dir"]) if pos else None
        for p in self.client.get_positions(coin=symbol):
            szi = float(p.get("szi", 0))
            if abs(szi) > 0:
                return szi
        return None

    def _open_positions_count(self) -> int:
        if self.dry_run or self.client is None:
            return len(self._live_state["paper"]["positions"])
        return len([p for p in self.client.get_positions() if abs(float(p.get("szi", 0))) > 0])

    # ── Positions papier (dry-run) ───────────────────────────────────────────

    def _paper_check_exits(self, symbol: str, candles: list) -> None:
        """Rejoue TP/SL (SL prioritaire, comme le backtester) sur les bougies
        clôturées depuis le dernier check de la position papier."""
        pos = self._live_state["paper"]["positions"].get(symbol)
        if not pos:
            return
        for c in candles:
            if c["ts"] <= pos["checked_ts"]:
                continue
            if pos["dir"] == 1:
                if c["low"] <= pos["sl"]:
                    self._paper_close(symbol, pos["sl"], "SL", c["ts"])
                    return
                if c["high"] >= pos["tp"]:
                    self._paper_close(symbol, pos["tp"], "TP", c["ts"])
                    return
            else:
                if c["high"] >= pos["sl"]:
                    self._paper_close(symbol, pos["sl"], "SL", c["ts"])
                    return
                if c["low"] <= pos["tp"]:
                    self._paper_close(symbol, pos["tp"], "TP", c["ts"])
                    return
            pos["checked_ts"] = c["ts"]

    def _paper_close(self, symbol: str, exit_px: float, reason: str, ts) -> None:
        paper = self._live_state["paper"]
        pos = paper["positions"].pop(symbol, None)
        if not pos:
            return
        cost = 2.0 * (config.FEE_PCT + config.SLIPPAGE_PCT)
        pnl = pos["dir"] * (exit_px - pos["entry"]) / pos["entry"] - cost
        paper["trades"].append({
            "symbol": symbol,
            "dir": pos["dir"],
            "entry": pos["entry"],
            "exit": exit_px,
            "pnl_pct": pnl,
            "reason": reason,
            "entry_ts": pos["entry_ts"],
            "exit_ts": ts,
        })
        trades = paper["trades"]
        total = sum(t["pnl_pct"] for t in trades)
        wins = len([t for t in trades if t["pnl_pct"] > 0])
        logger.info(
            "[PAPER] %s: EXIT %s @ %.6g (%s) pnl=%+.3f%% | cumul: %d trades, "
            "%+.3f%%, winrate %.0f%%",
            symbol, "LONG" if pos["dir"] == 1 else "SHORT", exit_px, reason,
            pnl * 100, len(trades), total * 100, 100.0 * wins / len(trades),
        )

    # ── Exécution ────────────────────────────────────────────────────────────

    def _close_position(self, symbol: str, ref_price: float = None, ts=None) -> None:
        if self.dry_run:
            if ref_price is not None:
                self._paper_close(symbol, ref_price, "FLIP", ts)
            return
        try:
            self.client.cancel_all_orders(symbol)   # purge TP/SL natifs orphelins
        except Exception as e:
            logger.warning("%s: cancel_all_orders: %r", symbol, e)
        self.client.market_close(symbol)
        logger.info("%s: position clôturée (market)", symbol)

    def _open_position(self, symbol: str, direction: int, ref_price: float,
                       atr_val: float, ts=None) -> None:
        side = "LONG" if direction == 1 else "SHORT"
        params = self.store.active_params(symbol)
        if atr_val <= 0 or ref_price <= 0 or params is None:
            logger.warning("%s: ATR/prix invalide, entrée annulée", symbol)
            return

        sl_price = ref_price - direction * params.sl_atr * atr_val
        tp_price = ref_price + direction * params.tp_atr * atr_val

        if self.dry_run:
            self._live_state["paper"]["positions"][symbol] = {
                "dir": direction,
                "entry": ref_price,
                "sl": sl_price,
                "tp": tp_price,
                "entry_ts": ts,
                "checked_ts": ts,
            }
            logger.info(
                "[PAPER] %s: OPEN %s @~%.6g | TP=%.6g SL=%.6g (ATR=%.6g, params=%s)",
                symbol, side, ref_price, tp_price, sl_price, atr_val, params.to_dict(),
            )
            return

        account_value = self._account_value()
        margin = account_value * config.MARGIN_PCT
        notional = max(config.MIN_NOTIONAL_USD, margin * config.LEVERAGE)
        qty = notional / ref_price

        # La marge doit être dans le perp : vire du spot si nécessaire.
        self._ensure_perp_margin(notional / config.LEVERAGE)
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

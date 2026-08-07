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
from simplebot.data import (closed_candles, fetch_ledger_updates, fetch_ohlcv,
                            net_transfer_flow)
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
        self.maybe_reload()

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

    @property
    def updated_at(self) -> str:
        return str(self._state.get("updated_at") or "")

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

    def tradeable_symbols(self) -> list:
        """Symboles actifs après filtre qualité, par quality_score DÉCROISSANT
        (tri alphabétique en secondaire, stable) : quand MAX_OPEN_POSITIONS
        sature, ce sont les mieux notés qui prennent les slots — pas les
        premiers de l'alphabet."""
        scores = self.quality_scores()
        return sorted(
            (sym for sym in self.symbols if self.active_params(sym) is not None),
            key=lambda s: (-scores.get(s, 0.0), s),
        )

    def quality_scores(self) -> dict:
        """{symbole actif: quality_score} — score composite du filtre qualité."""
        from simplebot.symbol_filter import quality_score
        out = {}
        for sym, entry in self._state.get("symbols", {}).items():
            if entry.get("active"):
                out[sym] = quality_score(entry)
        return out


# ── Trader ───────────────────────────────────────────────────────────────────

class SimpleLiveTrader:

    def __init__(self, client=None, store: ParamStore = None, dry_run: bool = None,
                 fetch=None, ledger_fetch=None):
        self.dry_run = config.DRY_RUN if dry_run is None else dry_run
        self.client = client
        self.store = store or ParamStore()
        self._fetch = fetch or fetch_ohlcv
        self._ledger_fetch = ledger_fetch or fetch_ledger_updates
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
        state.setdefault("last_flip_ts", {})
        state.setdefault("last_close_ts", {})   # cooldown re-entry après TP/SL/EXCHANGE
        state.setdefault("live_tracked", {})
        state.setdefault("closed_trades", [])
        state.setdefault("live_disabled", {})  # {sym: {reason, pf, n, disabled_at}}
        state.setdefault("exec_stats", {"maker": 0, "taker": 0, "mixed": 0, "skip": 0})
        # Equity paper $ (A/B dry-run) — indépendante du wallet live.
        if "paper_equity" not in state:
            state["paper_equity"] = float(getattr(config, "PAPER_START_EQUITY", 200.0) or 200.0)
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
        logger.info(
            "SimpleLiveTrader démarré — %s — intervalle %s — state=%s — paper_equity=%.2f",
            mode, config.INTERVAL, config.STATE_DIR,
            float(self._live_state.get("paper_equity") or 0),
        )
        self._live_state["dry_run"] = self.dry_run
        if self.dry_run and not self._live_state.get("equity_history"):
            self._live_state["equity_history"] = [
                [time.time(), float(self._live_state.get("paper_equity") or config.PAPER_START_EQUITY)],
            ]
        self._save_live_state()
        self.reconcile_positions()
        while True:
            try:
                self.tick()
            except Exception as e:
                logger.error("Tick en erreur: %r", e, exc_info=True)
            time.sleep(config.LOOP_SEC)

    def tick(self) -> None:
        reloaded = self.store.maybe_reload()
        if reloaded:
            # Nouveau run optimiseur → on redonne sa chance aux symboles gated.
            if self._live_state.get("live_disabled"):
                logger.info(
                    "Reload params : clear live_disabled (%s)",
                    list(self._live_state["live_disabled"].keys()),
                )
                self._live_state["live_disabled"] = {}
                self._save_live_state()
        if not self.dry_run and self.client is not None:
            now = time.time()
            if now - getattr(self, "_last_pos_sync", 0) >= config.POSITION_SYNC_SEC:
                self._last_pos_sync = now
                self._sync_exchange_closes()
        if self._kill_switch_engaged():
            return
        disabled = self._live_state.get("live_disabled") or {}
        for symbol in self.store.tradeable_symbols():
            if symbol in disabled:
                continue
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
                direction = 1 if szi > 0 else -1
                entry_px = float(p.get("entry_px", 0) or 0)
                self._live_state.setdefault("live_tracked", {})[coin] = {
                    "dir": direction,
                    "entry": entry_px,
                    "sl": None,
                    "tp": None,
                    "entry_ts": int(time.time() * 1000),
                }
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

    def _kill_account_value(self) -> float:
        """Valeur canonique HL (portfolio) — seule source du kill-switch.
        Évite les faux positifs quand perp seul ou spot seul est lu à tort
        (incident 11/07 : 99.92 lu alors que portfolio ≈ 200)."""
        canon = self.client.get_portfolio_value()
        self._equity_raw = {"perp": None, "spot": None, "canon": canon, "clamped": False}
        return canon

    def _account_value(self) -> float:
        """Valeur de compte pour le sizing : collatéral perp
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
        # Composantes brutes exposées pour le log equity_raw du kill-check.
        self._equity_raw = {"perp": perp, "spot": spot, "canon": None, "clamped": False}
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
            self._equity_raw["canon"] = canon
            if canon > 0 and total > canon * (1 + config.EQUITY_CANON_TOL):
                self._equity_raw["clamped"] = True
                # Throttlé : quand le résidu fantôme persiste (observé +14% sur
                # des heures), ce log partait à CHAQUE tick 30s. Le détail vit
                # déjà dans equity_raw ; ici on ne signale qu'aux 600s.
                now = time.time()
                if now - getattr(self, "_last_clamp_log", 0) >= config.EQUITY_LOG_EVERY_SEC:
                    self._last_clamp_log = now
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

    def _external_outflow_since(self, since_ts_sec: float) -> float:
        """USDC net SORTI du compte (retraits/sends externes) depuis since_ts_sec.
        0.0 si aucun mouvement ou adresse inconnue. Peut lever (réseau)."""
        addr = getattr(self.client, "wallet_address", "") or ""
        if not addr:
            return 0.0
        updates = self._ledger_fetch(addr, int(since_ts_sec * 1000))
        return -min(0.0, net_transfer_flow(updates, addr))

    def _kill_switch_engaged(self) -> bool:
        """
        True si le trading est en pause. Suit l'account value en fenêtre
        glissante ; en cas de perte > KILL_LOSS_PCT vs le pic (confirmée
        KILL_CONFIRMATIONS fois et non expliquée par un retrait), ferme tout
        et met le trading en pause KILL_PAUSE_SEC.
        """
        if self.dry_run or self.client is None:
            return False
        now = time.time()
        paused_until = float(self._live_state.get("paused_until", 0))
        paused = now < paused_until

        try:
            account_value = self._kill_account_value()
        except Exception as e:
            # Fail-safe : après N échecs consécutifs on gèle les entrées plutôt
            # que d'ignorer le check (les positions gardent leur TP/SL natif).
            self._acct_read_failures = getattr(self, "_acct_read_failures", 0) + 1
            if paused:
                return True
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

        if account_value > 0:
            # Log détaillé des composantes (throttlé — pas à chaque tick 30s).
            if now - getattr(self, "_last_equity_log", 0) >= config.EQUITY_LOG_EVERY_SEC:
                self._last_equity_log = now
                raw = getattr(self, "_equity_raw", {}) or {}
                logger.info(
                    "equity_raw perp=%s spot=%s canon=%s clamped=%s → retenu=%.2f",
                    f"{raw['perp']:.2f}" if raw.get("perp") is not None else "n/a",
                    f"{raw['spot']:.2f}" if raw.get("spot") is not None else "n/a",
                    f"{raw['canon']:.2f}" if raw.get("canon") is not None else "n/a",
                    raw.get("clamped", False), account_value,
                )
            # L'historique continue de vivre PENDANT la pause : sans ça le
            # dashboard restait figé sur le point du kill (constat 12-07).
            hist = self._live_state["equity_history"]
            if not hist or now - hist[-1][0] >= 300:  # un point max toutes les 5 min
                hist.append([now, account_value])
                cutoff = now - config.KILL_WINDOW_SEC
                hist[:] = [pt for pt in hist if pt[0] >= cutoff]
                self._save_live_state()

        if paused:
            if now - getattr(self, "_last_pause_log", 0) > 600:
                self._last_pause_log = now
                logger.warning("Kill-switch actif — reprise dans %.0f min",
                               (paused_until - now) / 60)
            return True
        if account_value <= 0:
            # Valeur nulle = wallet vide ou lectures dégradées : on ne kill pas,
            # mais on ne reste plus SILENCIEUX (le silence a masqué un wallet
            # réellement vidé le 12-07 pendant une heure de diagnostic).
            if now - getattr(self, "_last_zero_log", 0) > 600:
                self._last_zero_log = now
                raw = getattr(self, "_equity_raw", {}) or {}
                logger.warning(
                    "Account value nulle (perp=%.2f spot=%s canon=%s) — wallet vide "
                    "ou API dégradée : kill-check suspendu, aucune entrée possible",
                    raw.get("perp", 0.0),
                    f"{raw['spot']:.2f}" if raw.get("spot") is not None else "n/a",
                    f"{raw['canon']:.2f}" if raw.get("canon") is not None else "n/a",
                )
            return False

        hist = self._live_state["equity_history"]
        peak = max(v for _, v in hist) if hist else account_value

        if peak > 0 and account_value <= peak * (1 - config.KILL_LOSS_PCT):
            # Un RETRAIT n'est pas une perte : si la chute vs pic est expliquée
            # par un flux sortant au ledger (incident du 11-07 : send de $100
            # → kill + flatten à tort), on REBASE l'historique sur la nouvelle
            # valeur au lieu de fermer. Les TP/SL natifs restent en place.
            peak_ts = max(hist, key=lambda p: p[1])[0] if hist else now
            try:
                outflow = self._external_outflow_since(peak_ts - 60)
            except Exception as e:
                logger.warning("Kill-switch: ledger illisible (%r) — rebase impossible, "
                               "on poursuit le protocole normal", e)
                outflow = 0.0
            drop = peak - account_value
            if outflow > 0 and outflow >= drop * 0.8:
                logger.warning(
                    "Kill-switch: chute %.2f vs pic expliquée par un RETRAIT de "
                    "%.2f USDC (ledger) — rebase de l'equity sur %.2f, PAS de fermeture",
                    drop, outflow, account_value,
                )
                self._live_state["equity_history"] = [[now, account_value]]
                self._kill_breach_count = 0
                self._save_live_state()
                return False
            # Hystérésis : une lecture isolée sous le seuil (429 résiduel, valeur
            # aberrante passée entre les mailles) ne suffit plus — il faut
            # KILL_CONFIRMATIONS lectures CONSÉCUTIVES avant de tout fermer.
            self._kill_breach_count = getattr(self, "_kill_breach_count", 0) + 1
            if self._kill_breach_count < config.KILL_CONFIRMATIONS:
                logger.warning(
                    "Kill-switch: account value %.2f ≤ pic %.2f × (1-%.1f%%) — "
                    "confirmation %d/%d avant fermeture (hystérésis)",
                    account_value, peak, config.KILL_LOSS_PCT * 100,
                    self._kill_breach_count, config.KILL_CONFIRMATIONS,
                )
                self._save_live_state()
                return False
            self._kill_breach_count = 0
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

        self._kill_breach_count = 0
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
        cooldown_ms = config.FLIP_COOLDOWN_BARS * config.INTERVAL_MS

        if current is not None and current * direction > 0:
            logger.info("%s: signal %+d mais position déjà dans le sens — rien à faire",
                        symbol, direction)
            return

        if current is not None and current * direction < 0:
            # Anti-churn flip : ignore signaux opposés pendant FLIP_COOLDOWN_BARS.
            last_flip = float(self._live_state["last_flip_ts"].get(symbol, 0) or 0)
            if sig["ts"] is not None and last_flip and sig["ts"] - last_flip < cooldown_ms:
                logger.info(
                    "%s: signal %+d opposé ignoré — cooldown post-flip (%d/%d bougies)",
                    symbol, direction,
                    int((sig["ts"] - last_flip) / config.INTERVAL_MS),
                    config.FLIP_COOLDOWN_BARS,
                )
                return
            logger.info("%s: signal %+d opposé à la position → flip", symbol, direction)
            self._close_position(symbol, ref_price=sig["close"], ts=sig["ts"], reason="FLIP")
            if sig["ts"] is not None:
                self._live_state["last_flip_ts"][symbol] = sig["ts"]
                self._save_live_state()
            # Flip : ouverture immédiate dans le nouveau sens (pas de cooldown re-entry).
        else:
            # Flat : cooldown re-entry après TP/SL/EXCHANGE (Phase 1 anti re-chop).
            last_close = float(
                (self._live_state.get("last_close_ts") or {}).get(symbol, 0) or 0
            )
            if (sig["ts"] is not None and last_close
                    and sig["ts"] - last_close < cooldown_ms):
                logger.info(
                    "%s: signal %+d ignoré — cooldown post-close (%d/%d bougies)",
                    symbol, direction,
                    int((sig["ts"] - last_close) / config.INTERVAL_MS),
                    config.FLIP_COOLDOWN_BARS,
                )
                return
            if self._open_positions_count() >= config.MAX_OPEN_POSITIONS:
                logger.info("%s: signal %+d ignoré — MAX_OPEN_POSITIONS atteint",
                            symbol, direction)
                return

        self._open_position(symbol, direction, sig["close"], sig["atr"], ts=sig["ts"])

    # ── Lecture des positions ────────────────────────────────────────────────

    def _sync_exchange_closes(self) -> None:
        """Détecte les positions fermées côté exchange (TP/SL natif) non vues par le bot."""
        tracked = self._live_state.setdefault("live_tracked", {})
        if not tracked:
            return
        try:
            open_coins = {
                p["coin"]
                for p in self.client.get_positions()
                if abs(float(p.get("szi", 0))) > 0
            }
        except Exception as e:
            logger.warning("Sync positions: lecture HL impossible (%r)", e)
            return
        for symbol in list(tracked.keys()):
            if symbol in open_coins:
                continue
            pos = tracked.pop(symbol, None)
            if not pos:
                continue
            exit_px, pnl_usd, fee = self._resolve_exit_from_fills(symbol, pos)
            reason = self._classify_exit_reason(pos, exit_px)
            self._record_closed_trade(
                symbol, pos, exit_px=exit_px, reason=reason,
                pnl_usd=pnl_usd, fee=fee, exec_mode="exchange",
                set_reentry_cooldown=True,
            )

    def _resolve_exit_from_fills(self, symbol: str, pos: dict) -> tuple:
        """(exit_px, pnl_usd|None, fee) depuis user_fills HL, sinon mid."""
        entry_ts = int(pos.get("entry_ts") or 0)
        # entry_ts live est souvent en ms bougie ; fills en ms epoch
        since_ms = entry_ts if entry_ts > 1_000_000_000_000 else int(entry_ts * 1000)
        # marge de sécurité : si ts bougie 15m, accepte fills un peu avant
        since_ms = max(0, since_ms - 60_000)
        exit_px = None
        pnl_usd = None
        fee = 0.0
        try:
            fills = self.client.get_user_fills(coin=symbol, limit=40)
        except Exception as e:
            logger.debug("%s: get_user_fills pour close (%r)", symbol, e)
            fills = []
        closing = []
        for f in fills or []:
            t = int(f.get("time") or 0)
            if since_ms and t and t < since_ms:
                continue
            c_pnl = float(f.get("closed_pnl") or 0)
            start_pos = abs(float(f.get("start_position") or 0))
            fill_sz = abs(float(f.get("sz") or 0))
            if c_pnl != 0.0 or (start_pos > 0 and fill_sz > 0 and fill_sz <= start_pos + 1e-12):
                closing.append(f)
        if closing:
            closing.sort(key=lambda x: int(x.get("time") or 0), reverse=True)
            # agrège les fills de clôture récents (partial closes)
            latest_t = int(closing[0].get("time") or 0)
            window = [f for f in closing if abs(int(f.get("time") or 0) - latest_t) < 10_000]
            qty = sum(abs(float(f.get("sz") or 0)) for f in window)
            if qty > 0:
                exit_px = sum(
                    float(f.get("px") or 0) * abs(float(f.get("sz") or 0)) for f in window
                ) / qty
            else:
                exit_px = float(closing[0].get("px") or 0) or None
            pnl_usd = sum(float(f.get("closed_pnl") or 0) for f in window)
            fee = sum(abs(float(f.get("fee") or 0)) for f in window)
            if pnl_usd is not None:
                pnl_usd = float(pnl_usd) - fee
        if exit_px is None or exit_px <= 0:
            try:
                mids = self.client.get_all_mids()
                exit_px = float(mids.get(symbol) or 0) or None
            except Exception:
                exit_px = None
        return exit_px, pnl_usd, fee

    @staticmethod
    def _classify_exit_reason(pos: dict, exit_px: Optional[float]) -> str:
        """Infère TP/SL/EXCHANGE selon proximité du prix de sortie aux niveaux."""
        if exit_px is None or exit_px <= 0:
            return "EXCHANGE"
        sl = pos.get("sl")
        tp = pos.get("tp")
        entry = float(pos.get("entry") or 0)
        candidates = []
        if sl is not None:
            try:
                candidates.append(("SL", abs(float(exit_px) - float(sl))))
            except (TypeError, ValueError):
                pass
        if tp is not None:
            try:
                candidates.append(("TP", abs(float(exit_px) - float(tp))))
            except (TypeError, ValueError):
                pass
        if not candidates:
            return "EXCHANGE"
        reason, dist = min(candidates, key=lambda x: x[1])
        # Tolérance : 0.35 % du prix d'entrée (couvre mark vs trigger HL).
        tol = max(abs(entry) * 0.0035, abs(exit_px) * 0.0035, 1e-12)
        if dist <= tol:
            return reason
        # Si clairement du bon côté du trade et plus près d'un niveau, accepte
        # une tolérance élargie (1 %).
        if dist <= tol * 3:
            return reason
        return "EXCHANGE"

    def _record_closed_trade(
        self,
        symbol: str,
        pos: dict,
        exit_px: Optional[float],
        reason: str,
        pnl_usd: Optional[float] = None,
        fee: float = 0.0,
        exec_mode: Optional[str] = None,
        set_reentry_cooldown: bool = False,
        ts=None,
    ) -> dict:
        """Persiste un close live avec PnL, log, gate live, cooldown re-entry."""
        direction = int(pos.get("dir") or 0)
        entry = float(pos.get("entry") or 0)
        notional = float(pos.get("notional") or 0)
        sz = float(pos.get("sz") or 0)
        if notional <= 0 and entry > 0 and sz > 0:
            notional = entry * sz

        pnl_pct = None
        if pnl_usd is None and exit_px and entry > 0 and direction:
            # estimation prix si fills indisponibles
            raw = direction * (float(exit_px) - entry) / entry
            # frais aller-retour approx (close taker + entry déjà payée à l'open)
            cost = config.FEE_PCT + config.SLIPPAGE_PCT
            if exec_mode == "maker":
                cost = config.FEE_MAKER_PCT
            elif exec_mode in ("taker", "exchange", "FLIP", None):
                cost = config.FEE_PCT + config.SLIPPAGE_PCT
            pnl_pct = raw - cost
            if notional > 0:
                pnl_usd = pnl_pct * notional
            else:
                pnl_usd = None
        elif pnl_usd is not None and notional > 0:
            pnl_pct = float(pnl_usd) / notional
        elif pnl_usd is not None and entry > 0 and exit_px and direction:
            pnl_pct = direction * (float(exit_px) - entry) / entry

        trade = {
            "symbol": symbol,
            "dir": direction,
            "entry": entry,
            "exit": exit_px,
            "sl": pos.get("sl"),
            "tp": pos.get("tp"),
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_pct,
            "fee": fee,
            "notional": notional or None,
            "reason": reason,
            "exec": exec_mode or pos.get("exec"),
            "entry_ts": pos.get("entry_ts"),
            "closed_at": time.time(),
            "exit_ts": ts,
        }
        self._live_state.setdefault("closed_trades", []).append(trade)

        # Cooldown re-entry (TP/SL/EXCHANGE) — pas pour FLIP (ré-entrée immédiate).
        if set_reentry_cooldown:
            close_ts = ts if ts is not None else int(time.time() * 1000)
            self._live_state.setdefault("last_close_ts", {})[symbol] = close_ts

        side = "LONG" if direction == 1 else "SHORT"
        pnl_s = f"{pnl_usd:+.4f}$" if pnl_usd is not None else "n/a"
        pct_s = f"{pnl_pct * 100:+.3f}%" if pnl_pct is not None else "n/a"
        exit_s = f"{exit_px:.6g}" if exit_px else "?"
        logger.info(
            "%s: CLOSE %s @ %s (%s) pnl=%s (%s) entry=%.6g SL=%s TP=%s",
            symbol, side, exit_s, reason, pnl_s, pct_s, entry,
            pos.get("sl"), pos.get("tp"),
        )
        self._maybe_live_disable(symbol)
        self._save_live_state()
        return trade

    def _live_symbol_stats(self, symbol: str) -> dict:
        trades = [
            t for t in self._live_state.get("closed_trades") or []
            if t.get("symbol") == symbol and t.get("pnl_usd") is not None
        ]
        if not trades:
            return {"n": 0, "pf": 0.0, "sum": 0.0, "wr": 0.0}
        wins = [float(t["pnl_usd"]) for t in trades if float(t["pnl_usd"]) > 0]
        losses = [float(t["pnl_usd"]) for t in trades if float(t["pnl_usd"]) <= 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        pf = (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
        return {
            "n": len(trades),
            "pf": pf,
            "sum": sum(float(t["pnl_usd"]) for t in trades),
            "wr": len(wins) / len(trades),
        }

    def _maybe_live_disable(self, symbol: str) -> None:
        """Désactive un symbole jusqu'au prochain reload si PF live pourri."""
        min_n = int(getattr(config, "LIVE_GATE_MIN_TRADES", 8) or 8)
        min_pf = float(getattr(config, "LIVE_GATE_MIN_PF", 0.9) or 0.9)
        stats = self._live_symbol_stats(symbol)
        if stats["n"] < min_n:
            return
        if stats["pf"] >= min_pf:
            return
        disabled = self._live_state.setdefault("live_disabled", {})
        if symbol in disabled:
            return
        disabled[symbol] = {
            "reason": f"live_pf<{min_pf:.2f}",
            "pf": round(stats["pf"], 4),
            "n": stats["n"],
            "sum_pnl_usd": round(stats["sum"], 4),
            "disabled_at": time.time(),
        }
        logger.warning(
            "%s: LIVE GATE — désactivé (n=%d PF=%.3f sum=%+.2f$ < seuil PF %.2f) "
            "jusqu'au prochain reload optimiseur",
            symbol, stats["n"], stats["pf"], stats["sum"], min_pf,
        )

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
        # $ equity paper : notional = equity × margin_pct × lev (sizing dry-run).
        eq = float(self._live_state.get("paper_equity") or config.PAPER_START_EQUITY or 200.0)
        notional = max(config.MIN_NOTIONAL_USD, eq * config.MARGIN_PCT * config.LEVERAGE)
        pnl_usd = pnl * notional
        eq_new = eq + pnl_usd
        self._live_state["paper_equity"] = eq_new
        self._live_state.setdefault("equity_history", []).append([time.time(), eq_new])
        paper["trades"].append({
            "symbol": symbol,
            "dir": pos["dir"],
            "entry": pos["entry"],
            "exit": exit_px,
            "pnl_pct": pnl,
            "pnl_usd": pnl_usd,
            "notional": notional,
            "reason": reason,
            "entry_ts": pos["entry_ts"],
            "exit_ts": ts,
        })
        # Phase 1 : cooldown re-entry aussi en paper (sauf FLIP).
        if reason != "FLIP" and ts is not None:
            self._live_state.setdefault("last_close_ts", {})[symbol] = ts
        trades = paper["trades"]
        total = sum(t["pnl_pct"] for t in trades)
        wins = len([t for t in trades if t["pnl_pct"] > 0])
        logger.info(
            "[PAPER] %s: EXIT %s @ %.6g (%s) pnl=%+.3f%% (%+.2f$) | equity=%.2f | "
            "cumul: %d trades, %+.3f%%, winrate %.0f%%",
            symbol, "LONG" if pos["dir"] == 1 else "SHORT", exit_px, reason,
            pnl * 100, pnl_usd, eq_new, len(trades), total * 100,
            100.0 * wins / len(trades),
        )

    # ── Exécution ────────────────────────────────────────────────────────────

    def _close_position(self, symbol: str, ref_price: float = None, ts=None,
                        reason: str = "FLIP") -> None:
        if self.dry_run:
            if ref_price is not None:
                self._paper_close(symbol, ref_price, reason, ts)
            return
        tracked = self._live_state.setdefault("live_tracked", {})
        pos = tracked.get(symbol) or {
            "dir": 0, "entry": ref_price or 0, "sl": None, "tp": None,
        }
        # direction depuis la position HL si tracking incomplet
        if not pos.get("dir"):
            cur = self._current_position(symbol)
            if cur is not None:
                pos = dict(pos)
                pos["dir"] = 1 if cur > 0 else -1
        try:
            self.client.cancel_all_orders(symbol)   # purge TP/SL natifs orphelins
        except Exception as e:
            logger.warning("%s: cancel_all_orders: %r", symbol, e)
        close_started_ms = int(time.time() * 1000) - 5_000
        try:
            self.client.market_close(symbol)
        except Exception as e:
            logger.error("%s: market_close échoué: %r", symbol, e)
            raise
        tracked.pop(symbol, None)
        # Récupère le fill de clôture pour PnL réel
        exit_px = ref_price
        pnl_usd = None
        fee = 0.0
        try:
            fill = None
            if hasattr(self.client, "get_recent_closed_trade"):
                fill = self.client.get_recent_closed_trade(
                    symbol, since_ms=close_started_ms, max_wait_sec=2.0,
                )
            if fill:
                exit_px = float(fill.get("px") or exit_px or 0) or exit_px
                fee = abs(float(fill.get("fee") or 0))
                c_pnl = float(fill.get("closed_pnl") or 0)
                pnl_usd = c_pnl - fee
            else:
                exit_px2, pnl2, fee2 = self._resolve_exit_from_fills(
                    symbol, {**pos, "entry_ts": close_started_ms},
                )
                exit_px = exit_px2 or exit_px
                pnl_usd = pnl2
                fee = fee2
        except Exception as e:
            logger.debug("%s: résolution fill close (%r)", symbol, e)
        self._record_closed_trade(
            symbol, pos, exit_px=exit_px, reason=reason,
            pnl_usd=pnl_usd, fee=fee, exec_mode="taker",
            set_reentry_cooldown=(reason != "FLIP"),
            ts=ts if ts is not None else close_started_ms,
        )
        logger.info("%s: position clôturée (market, reason=%s)", symbol, reason)

    def _margin_pct_for(self, symbol: str) -> float:
        """Sizing dynamique (P1) : marge interpolée entre MARGIN_PCT (pire
        symbole actif) et MARGIN_PCT_MAX (meilleur) selon le quality_score
        normalisé. Fixe si désactivé, symbole inconnu ou < 2 actifs."""
        if not config.SIZING_DYNAMIC:
            return config.MARGIN_PCT
        scores = self.store.quality_scores()
        if symbol not in scores or len(scores) < 2:
            return config.MARGIN_PCT
        lo, hi = min(scores.values()), max(scores.values())
        norm = 1.0 if hi <= lo else (scores[symbol] - lo) / (hi - lo)
        pct = config.MARGIN_PCT + norm * (config.MARGIN_PCT_MAX - config.MARGIN_PCT)
        return min(config.MARGIN_PCT_MAX, max(config.MARGIN_PCT, pct))

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
        margin_pct = self._margin_pct_for(symbol)
        margin = account_value * margin_pct
        notional = max(config.MIN_NOTIONAL_USD, margin * config.LEVERAGE)
        qty = notional / ref_price

        # La marge doit être dans le perp : vire du spot si nécessaire.
        self._ensure_perp_margin(notional / config.LEVERAGE)
        self.client.update_leverage(symbol, config.LEVERAGE, is_cross=False)
        if config.EXEC_MAKER_FIRST:
            # Maker-first : limit Alo ; skip si non fill (Phase 1, pas de market
            # sauf SIMPLEBOT_EXEC_MARKET_FALLBACK=1).
            from simplebot.execution import smart_entry
            result = smart_entry(self.client, symbol, direction == 1, qty, ref_price)
            exec_mode = result["mode"]
        else:
            result = self.client.place_order(
                coin=symbol,
                is_buy=(direction == 1),
                sz=qty,
                limit_px=ref_price,
                order_type="market",
            )
            exec_mode = "taker"
        stats = self._live_state.setdefault("exec_stats", {})
        stats[exec_mode] = int(stats.get(exec_mode, 0)) + 1

        if exec_mode == "skip" or float(result.get("total_sz") or 0) <= 0:
            logger.info(
                "%s: OPEN %s annulé — maker non rempli (skip, pas de market)",
                symbol, side,
            )
            self._save_live_state()
            return

        fill_px = float(result.get("avg_px") or ref_price)
        fill_sz = float(result.get("total_sz") or qty)
        logger.info("%s: OPEN %s sz=%.6f @ %.6g (notional≈$%.2f, lev=%dx, marge=%.1f%%, exec=%s)",
                    symbol, side, fill_sz, fill_px, fill_sz * fill_px, config.LEVERAGE,
                    margin_pct * 100, exec_mode)

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
            self._live_state.setdefault("live_tracked", {})[symbol] = {
                "dir": direction,
                "entry": fill_px,
                "sl": sl_price,
                "tp": tp_price,
                "sz": fill_sz,
                "notional": fill_sz * fill_px,
                "exec": exec_mode,
                "entry_ts": ts or int(time.time() * 1000),
            }
            self._save_live_state()
        except Exception as e:
            # sans protection → on referme immédiatement
            logger.error("%s: pose TP/SL échouée (%r) → fermeture de sécurité", symbol, e)
            try:
                self.client.market_close(symbol)
            except Exception as e2:
                logger.critical("%s: FERMETURE DE SÉCURITÉ ÉCHOUÉE: %r — POSITION SANS SL !",
                                symbol, e2)

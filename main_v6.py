"""
SalleDesMarches V6 — main_v6.py

V6.1.0
- AgentMomentum (bull) : prompt court, retourne signal+confidence directionnel
- AgentRisk (bear) : prompt court, retourne risk_score numérique (0-1)
- Consensus V6.1 : accord momentum×tech → moyenne ; risque réduit conf linéairement
- Equity spot HL : lecture spotClearinghouseState pour vraie balance USDC
- Smart limit : garde staleness (cancel si mid > 0.3% du prix posé)
- SymbolSelector : filtre dans SCALP_WATCHLIST uniquement

V6.0.0
- boucle Hyperliquid Sync indépendante à HL_SYNC_SEC
- cache local thread-safe normalisé (positions, ordres ouverts, prix, equity)
- _get_open_positions() et _get_open_orders_cached() lisent le cache
- trailing en lecture cache uniquement, write only sur vrai changement de stop
- recalcul TP/SL post-fill sur fill_price réel
- update_regime(trend, volatility) compatible SharedMemory
- AgentBull/AgentBear/AgentLearner/AgentScalper alignés
- TP/SL via HyperliquidExchangeClient.place_tpsl_native
- Trailing software PROFIT-ONLY
  · aucun SL logiciel
  · TP armé : issu du signal scalper (pnl_tp × levier en ROE)
  · Trailing : sortie si recul de TRAIL_DROP_PCT depuis le pic ROE
  · Fallback : TP_ARM_PCT constant si scalper ne fournit pas la valeur
- Logging fichier : logs/sdm.log (rotation 10 Mo, 5 archives)
"""

from __future__ import annotations

import atexit
import fcntl
import logging
import logging.handlers
import math
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from memory.shared_memory import SharedMemory
from exchanges.base import OrderRequest
from exchanges.hyperliquid import HyperliquidExchangeClient
from agents.agent_orchestrator import AgentOrchestrator
from agents.agent_technical import AgentTechnical
from agents.agent_momentum import AgentMomentum
from agents.agent_risk_entry import AgentRiskEntry
from agents.agent_news_v2 import AgentNewsV2
from agents.agent_whales import AgentWhales
from agents.agent_orderbook import AgentOrderbook
from agents.agent_scalper import AgentScalper
from agents.agent_learner import AgentLearner
from agents.agent_symbol_selector import AgentSymbolSelector
from agents.agent_trader import AgentTrader
from agents.feature_engine import FeatureEngine
from agents.regime_engine import RegimeEngine
from agents.grid_manager import GridManager


def _setup_logging() -> None:
    os.makedirs("logs", exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)

    file_handler = logging.handlers.RotatingFileHandler(
        "logs/sdm.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        root.addHandler(console)
        root.addHandler(file_handler)
    else:
        has_file = any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers)
        if not has_file:
            root.addHandler(file_handler)


_setup_logging()
logger = logging.getLogger("sdm.v6.main")


def _load_settings():
    try:
        from config import settings as s
        return s
    except Exception as e:
        raise RuntimeError(f"Impossible d'importer config/settings.py: {e}")


SETTINGS = _load_settings()

LOCALAI_BASE_URL = getattr(SETTINGS, "LOCALAI_BASE_URL", "http://localhost:8080/v1")
MODELS = getattr(SETTINGS, "MODELS", getattr(SETTINGS, "LOCALAI_MODELS", {}))
SCAN_INTERVAL_SEC = int(getattr(SETTINGS, "SCAN_INTERVAL_SEC",
                         getattr(SETTINGS, "CYCLE_SEC",
                         getattr(SETTINGS, "CYCLE_SECONDS", 30))))
MAX_OPEN_POSITIONS = int(getattr(SETTINGS, "MAX_OPEN_POSITIONS", 5))
MIN_CONFIDENCE = float(getattr(SETTINGS, "MIN_CONFIDENCE", 0.55))
SIMULATION_MODE = bool(getattr(SETTINGS, "SIMULATION_MODE", False))
RESET_SIM_POSITIONS = bool(getattr(SETTINGS, "RESET_SIM_POSITIONS", False))
DEBUG_SKIPS = bool(getattr(SETTINGS, "DEBUG_SKIPS", True))
COOLDOWN_SEC = int(getattr(SETTINGS, "COOLDOWN_SEC", 900))
EXIT_COOLDOWN_SEC = int(getattr(SETTINGS, "EXIT_COOLDOWN_SEC", 300))
FLIP_COOLDOWN_SEC = int(getattr(SETTINGS, "FLIP_COOLDOWN_SEC", 300))
NEWS_REFRESH_SEC = int(getattr(SETTINGS, "NEWS_REFRESH_SEC", 1800))
WHALES_REFRESH_SEC = int(getattr(SETTINGS, "WHALES_REFRESH_SEC", 1800))
MIN_VOLRATIO = float(getattr(SETTINGS, "MIN_VOLRATIO", 0.005))
RISK_PER_TRADE_PCT = float(getattr(SETTINGS, "RISK_PER_TRADE_PCT", 0.01))
MAX_LEVERAGE = float(getattr(SETTINGS, "MAX_LEVERAGE", 3.0))
MAX_NOTIONAL_PCT = float(getattr(SETTINGS, "MAX_NOTIONAL_PCT", 0.10))
SIZING_CONF_FLOOR = float(getattr(SETTINGS, "SIZING_CONF_FLOOR", 0.40))
FLIP_MIN_CONFIDENCE = float(getattr(SETTINGS, "FLIP_MIN_CONFIDENCE", 0.80))

# Sécurité capital — hard rules (cf. config/settings.py pour les valeurs)
SCALP_SL_PNL_PCT = float(getattr(SETTINGS, "SCALP_SL_PNL_PCT", 0.015))
EMERGENCY_LOSS_ROE_MULT = float(getattr(SETTINGS, "EMERGENCY_LOSS_ROE_MULT", 2.0))
SL_FALLBACK_BUFFER_PCT = float(getattr(SETTINGS, "SL_FALLBACK_BUFFER_PCT", 0.005))
FLIP_EMERGENCY_LOSS_PCT = float(getattr(SETTINGS, "FLIP_EMERGENCY_LOSS_PCT", 0.015))

BLOCKED_HOURS_UTC = set(getattr(SETTINGS, "BLOCKED_HOURS_UTC", set()))
SYMBOLS_PER_CYCLE = int(getattr(SETTINGS, "SYMBOLS_PER_CYCLE", 2))
BULL_BEAR_REFRESH_SEC = int(getattr(SETTINGS, "BULL_BEAR_REFRESH_SEC", 300))
HL_SYNC_SEC = float(getattr(SETTINGS, "HL_SYNC_SEC", 2.0))
HL_CACHE_MAX_AGE_SEC = float(getattr(SETTINGS, "HL_CACHE_MAX_AGE_SEC", max(10.0, HL_SYNC_SEC * 5.0)))

# FIX #4: Période de grâce pour les trail guards fraîchement créés.
# Empêche un sync HL en retard de supprimer un guard avant que la position
# n'apparaisse dans le cache (la position est sur Hyperliquid, mais le cache local
# n'est mis à jour que toutes les HL_SYNC_SEC secondes).
GUARD_GRACE_PERIOD_SEC = float(getattr(SETTINGS, "GUARD_GRACE_PERIOD_SEC", max(6.0, HL_SYNC_SEC * 3.0)))

TRAIL_CHECK_SEC = int(getattr(SETTINGS, "TRAIL_CHECK_SEC", 60))
TP_ARM_PCT = float(getattr(SETTINGS, "TP_ARM_PCT", 0.0060))
TRAIL_DROP_PCT = float(getattr(SETTINGS, "TRAIL_DROP_PCT", 0.0025))  # laissé temporairement pour compat

# Nouveau trailing natif à effet cliquet
TRAIL_STEP_ROE = float(getattr(SETTINGS, "TRAIL_STEP_ROE", 0.0020))
TRAIL_BREAKEVEN_ROE = float(getattr(SETTINGS, "TRAIL_BREAKEVEN_ROE", 0.0000))

# Clamp de sécurité : 0 <= BREAKEVEN < TP_ARM - STEP
TRAIL_BREAKEVEN_ROE = max(0.0, TRAIL_BREAKEVEN_ROE)
_TRAIL_BE_MAX = max(0.0, TP_ARM_PCT - TRAIL_STEP_ROE - 1e-9)

if TRAIL_BREAKEVEN_ROE > _TRAIL_BE_MAX:
    logger.warning(
        "TRAIL_BREAKEVEN_ROE clampé de %.4f%% à %.4f%% (contrainte: < TP_ARM_PCT - TRAIL_STEP_ROE)",
        TRAIL_BREAKEVEN_ROE * 100.0,
        _TRAIL_BE_MAX * 100.0,
    )
    TRAIL_BREAKEVEN_ROE = _TRAIL_BE_MAX

    
FREEZE_LOOKBACK_TRADES = int(getattr(SETTINGS, "FREEZE_LOOKBACK_TRADES", 5))
FREEZE_MIN_TRADES = int(getattr(SETTINGS, "FREEZE_MIN_TRADES", 3))
FREEZE_CONSEC_LOSSES = int(getattr(SETTINGS, "FREEZE_CONSEC_LOSSES", 2))
FREEZE_BAD_COUNT = int(getattr(SETTINGS, "FREEZE_BAD_COUNT", 3))
FREEZE_WINRATE_MAX = float(getattr(SETTINGS, "FREEZE_WINRATE_MAX", 0.34))
FREEZE_PNL_SUM_MAX = float(getattr(SETTINGS, "FREEZE_PNL_SUM_MAX", -0.0040))
FREEZE_SEC_SHORT = int(getattr(SETTINGS, "FREEZE_SEC_SHORT", 3600))
FREEZE_SEC_LONG = int(getattr(SETTINGS, "FREEZE_SEC_LONG", 4 * 3600))

DEFENSIVE_CUT_ENABLED = bool(getattr(SETTINGS, "DEFENSIVE_CUT_ENABLED", True))
DEFENSIVE_REGIME_CUT_ON_TREND_CHANGE = bool(getattr(SETTINGS, "DEFENSIVE_REGIME_CUT_ON_TREND_CHANGE", True))
DEFENSIVE_REGIME_CUT_ON_RISK_HIGH = bool(getattr(SETTINGS, "DEFENSIVE_REGIME_CUT_ON_RISK_HIGH", True))
DEFENSIVE_CUT_MIN_AGE_SEC = int(getattr(SETTINGS, "DEFENSIVE_CUT_MIN_AGE_SEC", 45))
DEFENSIVE_CUT_FLAT_PNL_MAX = float(getattr(SETTINGS, "DEFENSIVE_CUT_FLAT_PNL_MAX", 0.0015))

GRID_ENABLED = bool(getattr(SETTINGS, "GRID_ENABLED", False))
SCALP_ENABLED = bool(getattr(SETTINGS, "SCALP_ENABLED", True))
GRID_MAX_SYMBOLS = int(getattr(SETTINGS, "GRID_MAX_SYMBOLS", 2))
GRID_FORCE_SYMBOLS = list(getattr(SETTINGS, "GRID_FORCE_SYMBOLS", []))


def _consensus(bull: Dict, bear: Dict, technical: Dict) -> Dict:
    """
    V6.1 — consensus momentum × risque.

    Direction : momentum (AgentBull) + technique doivent s'aligner.
    Confiance  : moyenne pondérée réduite par le risk_score (AgentBear).
    """
    # LLM saturé / down ? On retourne un side distinct pour mesurer la perte
    # d'opportunité réelle (cf. code_proposals.md 2026-05-05 [INFO] LLM timeouts).
    # Sans ça, tech=0.00 issu d'un timeout est traité comme un signal neutre légitime.
    llm_down_sources = [
        name for name, src in (("bull", bull), ("bear", bear), ("tech", technical))
        if str(src.get("llm_status", "") or "").lower() == "down"
    ]
    if llm_down_sources:
        return {"side": "llm_down", "confidence": 0.0,
                "reason": f"LLM down on: {','.join(llm_down_sources)}"}

    mom_signal = str(bull.get("signal", "wait") or "wait").lower()
    mom_conf   = float(bull.get("confidence", 0) or 0)
    risk_score = float(bear.get("risk_score", 0.5) or 0.5)
    risk_lvl   = str(bear.get("risk_level", "medium") or "medium").lower()
    tech_conf  = float(technical.get("confidence", 0) or 0)
    tech_sig   = str(technical.get("signal", "wait") or "wait").lower()

    # Direction et confiance de base
    if mom_signal in ("buy", "sell") and mom_signal == tech_sig:
        # Les deux s'accordent → confiance moyenne (bonus d'accord)
        side = mom_signal
        base_conf = (mom_conf + tech_conf) / 2.0
    elif tech_sig in ("buy", "sell"):
        # Tech seul → poids réduit (momentum silencieux ou neutre)
        side = tech_sig
        base_conf = tech_conf * 0.80
    else:
        return {"side": "wait", "confidence": 0.0,
                "reason": f"bull={mom_conf:.2f} tech={tech_conf:.2f} bear_risk={risk_lvl}"}

    # Réduction par le risque : risk_score=0 → pas de pénalité, 1 → -50%
    final_conf = base_conf * (1.0 - risk_score * 0.5)

    return {
        "side":       side,
        "confidence": max(0.0, min(1.0, final_conf)),
        "reason":     f"bull={mom_conf:.2f} tech={tech_conf:.2f} bear_risk={risk_lvl}",
    }


class SalleDesMarchesV6:
    def __init__(self, symbols: List[str], simulation: bool = False) -> None:
        self.symbols = symbols
        self.simulation = simulation
        self._symbol_idx = 0

        self.memory = SharedMemory()
        self.exchange = HyperliquidExchangeClient(enable_trading=not simulation)

        self.orchestrator = AgentOrchestrator(self.memory)
        self.technical = AgentTechnical(self.memory, self.exchange)
        self.bull = AgentMomentum(self.memory)
        self.bear = AgentRiskEntry(self.memory)
        self.news = AgentNewsV2(self.memory, SETTINGS)
        self.whales = AgentWhales(self.memory)
        self.orderbook = AgentOrderbook(self.exchange)
        self.scalper = AgentScalper(self.memory)
        self.learner = AgentLearner(self.memory)
        self.symbol_selector = AgentSymbolSelector(self.memory)
        self.trader = AgentTrader(self.memory, self.exchange)
        self.featureengine = FeatureEngine(self.memory, self.exchange)
        self.regimeengine = RegimeEngine(self.memory)

        self.last_entry_ts: Dict[str, float] = {}
        self.last_news_ts = 0.0
        self.last_whales_ts = 0.0
        self.last_learn_ts = 0.0
        self.last_trail_ts = 0.0

        self._trail_guards: Dict[str, Dict] = {}
        self._trail_lock = threading.Lock()  # Protège l'accès concurrent aux trail_guards
        # Empêche un emergency exit de fire plusieurs fois sur le même symbole
        # pendant que la fermeture est en vol. Stocke {symbol: timestamp_of_attempt}.
        # Auto-libéré quand la position disparaît du HL cache, OU après 30s (TTL).
        self._emergency_closing: Dict[str, float] = {}
        self._emergency_lock = threading.Lock()
        self.grid_manager = GridManager(self.exchange)
        self._tick_decimals: Dict[str, int] = {}
        self._prev_open_positions: Dict[str, Dict] = {}
        self._exit_cooldowns: Dict[str, float] = {}
        self._flip_cooldowns: Dict[str, float] = {}
        self._freeze_until: Dict[str, float] = {}
        self._entry_regime_ctx: Dict[str, Dict] = {}

        # ── Cache Hyperliquid thread-safe ──────────────────────────
        self._hl_cache_lock = threading.Lock()
        self._hl_cache: Dict = {
            "positions": {},
            "prices": {},
            "account_value_usdt": 0.0,
            "open_orders": [],
        }
        self._hl_cache_ts: float = 0.0

        # Lancer le thread de sync HL en mode live
        if not self.simulation:
            self._hl_sync_thread = threading.Thread(
                target=self._hl_sync_loop,
                daemon=True,
                name="hl-sync",
            )
            self._hl_sync_thread.start()
            # Attendre le premier remplissage du cache
            for _ in range(10):
                if self._hl_cache_ts > 0:
                    break
                time.sleep(0.5)
            logger.info("HL cache initialisé (ts=%.1f)", self._hl_cache_ts)

            # Thread dédié pour le trailing — vérification fréquente, indépendante des cycles LLM
            self._trail_thread = threading.Thread(
                target=self._trail_loop,
                daemon=True,
                name="trail-monitor",
            )
            self._trail_thread.start()
            logger.info("Trail monitor thread démarré (intervalle=%ds)", TRAIL_CHECK_SEC)
            self._recover_trail_guards()

            if GRID_ENABLED:
                self._grid_thread = threading.Thread(
                    target=self._grid_loop,
                    daemon=True,
                    name="grid-monitor",
                )
                self._grid_thread.start()
                logger.info("Grid monitor thread démarré")

            # Health check thread : scan périodique 60s pour détecter les
            # positions orphelines (sans SL ni trail guard) et leur poser un SL.
            # Filet de rattrapage indépendant de la boucle principale et du grid.
            self._health_check_thread = threading.Thread(
                target=self._health_check_loop,
                daemon=True,
                name="health-check",
            )
            self._health_check_thread.start()
            logger.info("Health check thread démarré (intervalle=60s)")

        if self.simulation and RESET_SIM_POSITIONS:
            self._reset_sim_positions()

        try:
            meta = self.exchange._client.get_meta()
            universe = meta.get("meta", {}).get("universe", []) or meta.get("universe", [])
            active = self.symbol_selector.refresh_from_meta(universe)
            if active:
                self.symbols = active
            logger.info("SymbolSelector init: symboles actifs=%s", self.symbols)
        except Exception as e:
            logger.warning("SymbolSelector init error: %r", e)

    def _reset_sim_positions(self) -> None:
        try:
            current = self.memory.get_positions() or {}
            for sym in list(current.keys()):
                self.memory.update_position(sym, None)
            logger.info("[SIM] Positions reset (%d supprimées)", len(current))
        except Exception as e:
            logger.warning("[SIM] reset positions error: %r", e)

    # ── Cache Hyperliquid : sync loop + méthodes utilitaires ─────

    def _normalize_symbol(self, symbol: str) -> str:
        """Normalise un symbole vers le format Hyperliquid (majuscule, sans suffixe)."""
        s = str(symbol or "").upper().strip()
        for suffix in ("-PERP", "-USD", "/USD", "/USDT", "-USDT"):
            if s.endswith(suffix):
                s = s[: -len(suffix)]
        return s

    def _hl_sync_loop(self) -> None:
        """Thread démon : synchronise le cache HL toutes les HL_SYNC_SEC secondes."""
        logger.info("HL sync thread démarré (intervalle=%.1fs)", HL_SYNC_SEC)
        while True:
            try:
                self._hl_sync_once()
            except Exception as e:
                logger.warning("HL sync error: %r", e)
            time.sleep(HL_SYNC_SEC)

    def _trail_loop(self) -> None:
        """Thread démon : monitore le trailing toutes les TRAIL_CHECK_SEC secondes,
        indépendamment des cycles LLM (sinon les cycles longs masquent les pics ROE).
        """
        # Petit délai au démarrage pour laisser le cache se remplir
        time.sleep(max(2.0, HL_SYNC_SEC * 1.5))
        while True:
            try:
                # Toujours appeler _monitor_trailing : il contient le filet
                # emergency exit pour les positions grid/orphan (hors trail_guards).
                # Si on skippait quand _trail_guards est vide, les positions
                # ouvertes par le grid n'auraient AUCUNE protection.
                self._monitor_trailing()
            except Exception as e:
                logger.warning("trail loop error: %r", e)
            time.sleep(TRAIL_CHECK_SEC)

    def _health_check_loop(self) -> None:
        """Thread démon : scan périodique de toutes les positions pour détecter
        les orphelins (sans SL natif ni trail guard) et leur poser un SL.

        Fréquence basse (60s) : c'est un filet de rattrapage, pas un mécanisme
        temps réel. L'emergency exit + trail loop couvrent le temps réel.

        Cas couverts :
        - Position grid résiduelle après désactivation normale (cf. SOL +4.93% nu 2026-05-06)
        - Trail guard avec sl_oid=None resté orphelin depuis le boot
        - Position ouverte par mécanisme externe (manuel sur UI HL)
        """
        time.sleep(max(20.0, HL_SYNC_SEC * 10))  # délai initial pour boot stable
        while True:
            try:
                self._health_check_positions()
            except Exception as e:
                logger.warning("health check loop error: %r", e)
            time.sleep(60)

    def _health_check_positions(self) -> None:
        """Détecte et corrige les positions orphelines (sans protection)."""
        if self.simulation:
            return
        open_pos = self._get_open_positions()
        if not open_pos:
            return

        scalp_sl_pnl_pct = float(getattr(SETTINGS, "SCALP_SL_PNL_PCT", 0.015))

        for symbol, pos in open_pos.items():
            try:
                side = str(pos.get("side", "")).lower()
                entry = float(pos.get("entry", 0) or 0)
                qty = abs(float(pos.get("qty", 0) or 0))
                leverage = float(pos.get("leverage", MAX_LEVERAGE) or MAX_LEVERAGE)

                if side not in {"buy", "sell"} or entry <= 0 or qty <= 0:
                    continue

                # Skip dust (HL refuse les ordres < $10)
                price = self._get_current_price(symbol, entry)
                if price <= 0 or qty * price < 10.0:
                    continue

                guard = self._trail_guards.get(symbol)
                # Si guard existe ET a un sl_oid valide → considéré protégé
                if guard and guard.get("sl_oid"):
                    continue

                # Orphelin détecté : tente le scan large + place_tpsl_native fallback
                logger.warning(
                    "[HEALTH_CHECK] %s position orpheline (guard=%s sl_oid=%s) → tentative protection",
                    symbol, "yes" if guard else "no",
                    guard.get("sl_oid") if guard else "n/a",
                )
                sl_oid = self._recover_or_place_sl(
                    symbol=symbol, side=side, entry=entry,
                    qty=qty, leverage=leverage,
                    scalp_sl_pnl_pct=scalp_sl_pnl_pct,
                )
                if sl_oid:
                    if guard:
                        # Met à jour le guard existant
                        guard["sl_oid"] = sl_oid
                        logger.info("[HEALTH_CHECK] %s sl_oid mis à jour sur guard existant: %s", symbol, sl_oid)
                    else:
                        # Crée un nouveau guard pour activer la trail logic
                        self._register_trail_guard(
                            symbol=symbol, side=side, entry=entry,
                            leverage=leverage, scalper_prices=None,
                            sl_oid=sl_oid, tp_oid=None,
                        )
                        logger.warning(
                            "[HEALTH_CHECK] %s guard créé + SL placé oid=%s — position désormais protégée",
                            symbol, sl_oid,
                        )
                else:
                    logger.error(
                        "[HEALTH_CHECK] %s ÉCHEC protection — position toujours orpheline (emergency exit reste actif)",
                        symbol,
                    )
            except Exception as e:
                logger.warning("[HEALTH_CHECK] %s erreur: %r", symbol, e)

    def _grid_loop(self) -> None:
        """Thread démon : gère les grilles actives (range markets)."""
        time.sleep(max(3.0, HL_SYNC_SEC * 2))
        while True:
            try:
                if self.grid_manager.active_symbols():
                    self._grid_tick()
            except Exception as e:
                logger.warning("grid loop error: %r", e)
            time.sleep(TRAIL_CHECK_SEC)

    def _grid_tick(self) -> None:
        """Un tick du grid manager : vérifie chaque grille active via le cache HL."""
        with self._hl_cache_lock:
            open_orders = list(self._hl_cache.get("open_orders", []))
            prices = dict(self._hl_cache.get("prices", {}))

        open_oids: set = set()
        for o in open_orders:
            try:
                open_oids.add(int(o.get("oid", 0)))
            except (ValueError, TypeError):
                pass

        for symbol in self.grid_manager.active_symbols():
            price = float(prices.get(symbol, 0) or 0)
            if price > 0:
                self.grid_manager.on_tick(symbol, open_oids, price)

    def _hl_sync_once(self) -> None:
        """Lit positions, prix, equity et ordres ouverts depuis l'API HL."""
        positions: Dict[str, Dict] = {}
        prices: Dict[str, float] = {}
        account_value: float = 0.0
        open_orders: list = []
        positions_ok = False
        prices_ok = False
        orders_ok = False

        try:
            user_state = self.exchange._client.get_user_state()
            margin_summary = user_state.get("marginSummary", {})
            account_value = float(margin_summary.get("accountValue", 0.0) or 0.0)
            # Le compte perp n'a que la marge allouée aux positions ouvertes.
            # La vraie balance est dans le compte spot (USDC). On l'additionne.
            try:
                spot_state = self.exchange._client.info.spot_user_state(self.exchange._client._wallet_address)
                for bal in spot_state.get("balances", []):
                    if str(bal.get("coin", "")).upper() in ("USDC", "USDT"):
                        account_value += float(bal.get("total", 0.0) or 0.0)
            except Exception as _spot_err:
                logger.debug("HL spot balance non disponible: %r", _spot_err)
            logger.debug("HL equity (perp+spot)=%.2f", account_value)

            for pos_data in user_state.get("assetPositions", []):
                pos = pos_data.get("position", pos_data)
                coin = str(pos.get("coin", "")).upper()
                if not coin:
                    continue
                szi = float(pos.get("szi", 0) or 0)
                if szi == 0:
                    continue

                entry_px = float(pos.get("entryPx", 0) or 0)
                leverage_val = float(pos.get("leverage", {}).get("value", MAX_LEVERAGE) if isinstance(pos.get("leverage"), dict) else pos.get("leverage", MAX_LEVERAGE) or MAX_LEVERAGE)

                positions[coin] = {
                    "side": "buy" if szi > 0 else "sell",
                    "qty": abs(szi),
                    "entry": entry_px,
                    "leverage": leverage_val,
                    "unrealized_pnl": float(pos.get("unrealizedPnl", 0) or 0),
                }
            positions_ok = True
        except Exception as e:
            logger.warning("HL sync positions error: %r", e)

        try:
            all_mids = self.exchange._client.get_all_mids()
            for coin, px in all_mids.items():
                prices[str(coin).upper()] = float(px)
            prices_ok = True
        except Exception as e:
            logger.warning("HL sync prices error: %r", e)

        try:
            open_orders = self.exchange._client.get_open_orders(coin=None) or []
            orders_ok = True
        except Exception as e:
            logger.warning("HL sync orders error: %r", e)

        with self._hl_cache_lock:
            # Ne pas écraser le cache avec des données vides si l'appel API a échoué.
            # Un fetch raté ne doit pas supprimer les positions/ordres connus.
            if positions_ok:
                self._hl_cache["positions"] = positions
                self._hl_cache["account_value_usdt"] = account_value
            if prices_ok:
                self._hl_cache["prices"] = prices
            if orders_ok:
                self._hl_cache["open_orders"] = open_orders
            # On met à jour le timestamp seulement si au moins les positions ont été lues,
            # sinon _assert_hl_cache_fresh continuera à forcer des retries.
            if positions_ok:
                self._hl_cache_ts = time.time()

    def _assert_hl_cache_fresh(self) -> None:
        """Vérifie que le cache n'est pas périmé. Si oui, force un sync."""
        age = time.time() - self._hl_cache_ts
        if age > HL_CACHE_MAX_AGE_SEC:
            logger.warning("HL cache périmé (%.1fs > %.1fs), sync forcé", age, HL_CACHE_MAX_AGE_SEC)
            try:
                self._hl_sync_once()
            except Exception as e:
                logger.error("HL sync forcé échoué: %r", e)

    def _get_stop_orders_cached(self, symbol: str) -> Dict[str, Dict]:
        """Retourne les ordres stop/trigger ouverts pour un symbole depuis le cache."""
        sym = self._normalize_symbol(symbol)
        result: Dict[str, Dict] = {}

        with self._hl_cache_lock:
            orders = list(self._hl_cache.get("open_orders", []))

        for order in orders:
            coin = str(order.get("coin", "")).upper()
            if coin != sym:
                continue
            oid = order.get("oid")
            order_type_str = str(order.get("orderType", "")).lower()
            # Inclure les ordres trigger identifiés par isTrigger (frontend_open_orders),
            # par le contenu de orderType, ou par reduceOnly
            is_trigger = (
                bool(order.get("isTrigger", False))
                or any(kw in order_type_str for kw in ("stop", "trigger", "take profit", " tp", " sl"))
                or str(order.get("tpsl", "")).lower() in ("sl", "tp")
            )
            is_reduce = bool(order.get("reduceOnly", False))
            if (is_trigger or is_reduce) and oid is not None:
                result[str(oid)] = order

        return result

    def _get_mark_price(self, symbol: str) -> float:
        """Retourne le prix mark depuis le cache HL. 0.0 si non disponible."""
        sym = self._normalize_symbol(symbol)
        with self._hl_cache_lock:
            prices = self._hl_cache.get("prices", {})
        try:
            return float(prices.get(sym, 0) or 0)
        except (ValueError, TypeError):
            return 0.0

    def _get_orderbook_snapshot(self, symbol: str) -> Dict:
        try:
            snapshot = self.orderbook.analyze(symbol) or {}
            if isinstance(snapshot, dict):
                return snapshot
        except Exception as e:
            logger.warning("ORDERBOOK SNAPSHOT %s error %r", symbol, e)
        return {}

    def _mark_trade_exit(self, symbol: str, reason: str = "exit") -> None:
        self._exit_cooldowns[symbol] = time.time()
        logger.info("[COOLDOWN] %s exit marqué (%s), cooldown %ds", symbol, reason, EXIT_COOLDOWN_SEC)

    def _sync_manual_closures(self, current_open: Dict[str, Dict]) -> None:
        """Détecte les positions fermées manuellement (hors bot) et nettoie les guards.
        
        IMPORTANT : Ne supprime les guards QUE si la position a vraiment disparu
        ET qu'elle existait dans le cycle précédent (pour éviter de supprimer
        les guards des positions fraîchement ouvertes entre deux syncs).
        """
        previous_open = dict(self._prev_open_positions)

        for symbol in list(previous_open.keys()):
            if symbol not in current_open:
                # FIX #4: Avant de marquer comme exit externe et supprimer le guard,
                # vérifier si le guard est en période de grâce (fraîchement créé).
                # Si oui, ne pas le supprimer car le cache HL peut ne pas avoir
                # encore la position en mémoire.
                guard = self._trail_guards.get(symbol)
                if guard:
                    age = time.time() - float(guard.get("created_at", 0))
                    if age < GUARD_GRACE_PERIOD_SEC:
                        logger.debug(
                            "[CLOSURES] %s en période de grâce (%.1fs < %.1fs), suppression différée",
                            symbol, age, GUARD_GRACE_PERIOD_SEC
                        )
                        continue

                # La position existait avant mais n'existe plus → fermeture externe
                self._mark_trade_exit(symbol, "external_exit")
                self._trail_guards.pop(symbol, None)
                self._entry_regime_ctx.pop(symbol, None)

        # CRITICAL : on met à jour _prev_open APRÈS avoir vérifié les disparitions,
        # pour que les nouvelles positions du cycle actuel soient dans le "previous"
        # au prochain sync (et donc protégées de la suppression immédiate si elles
        # n'apparaissent pas encore dans le cache HL à cause d'un lag de sync).
        self._prev_open_positions = dict(current_open)

    def _get_open_positions(self) -> Dict[str, Dict]:
        if self.simulation:
            return self.memory.get_positions() or {}

        self._assert_hl_cache_fresh()
        with self._hl_cache_lock:
            positions = dict(self._hl_cache.get("positions", {}) or {})
        return positions

    def _get_current_price(self, symbol: str, fallback: float) -> float:
        if self.simulation:
            return fallback

        self._assert_hl_cache_fresh()
        sym = self._normalize_symbol(symbol)
        with self._hl_cache_lock:
            prices = dict(self._hl_cache.get("prices", {}) or {})
        px = prices.get(sym)
        try:
            return float(px or fallback)
        except Exception:
            return fallback

    def _update_native_trailing_sl(
        self,
        symbol: str,
        guard: Dict,
        protected_roe: float,
        current_price: float,
    ) -> None:
        """
        Met à jour uniquement le SL natif Hyperliquid en mode cliquet.
        Règle stricte:
        - cache first pour position / prix / ordres stop
        - jamais d'annulation
        - jamais de recréation
        - modify only
        - si le modify échoue, on logge et on ne touche à rien
        """
        try:
            self._assert_hl_cache_fresh()

            symbol = self._normalize_symbol(symbol)
            side = str(guard.get("side", "buy")).lower()
            entry_raw = guard.get("entry", 0.0)
            lev_raw = guard.get("leverage", MAX_LEVERAGE)

            try:
                entry = float(entry_raw or 0.0)
            except Exception:
                logger.warning("TRAIL NATIVE SL %s %s: entry invalide %r", symbol, side, entry_raw)
                return

            try:
                lev = float(lev_raw or MAX_LEVERAGE)
            except Exception:
                lev = MAX_LEVERAGE

            try:
                prot = float(protected_roe or 0.0)
            except Exception:
                logger.warning("TRAIL NATIVE SL %s %s: protected_roe invalide %r", symbol, side, protected_roe)
                return

            try:
                current_price = float(current_price or 0.0)
            except Exception:
                logger.warning("TRAIL NATIVE SL %s %s: current_price invalide %r", symbol, side, current_price)
                return

            if side not in {"buy", "sell"}:
                logger.warning("TRAIL NATIVE SL %s: side invalide %r", symbol, side)
                return

            if entry <= 0 or prot < 0.0 or current_price <= 0.0:
                return

            if lev <= 0:
                lev = MAX_LEVERAGE

            if side == "buy":
                raw_sl = entry * (1.0 + prot / lev)
                raw_sl = min(raw_sl, current_price)
            else:
                raw_sl = entry * (1.0 - prot / lev)
                raw_sl = max(raw_sl, current_price)

            target_sl_px = self._round_px(symbol, raw_sl)
            if target_sl_px <= 0:
                logger.warning("TRAIL NATIVE SL %s %s: target_sl_px invalide %.8f", symbol, side, target_sl_px)
                return

            current_sl_raw = guard.get("native_sl_price", 0.0)
            try:
                current_sl_px = float(current_sl_raw or 0.0)
            except Exception:
                current_sl_px = 0.0

            if current_sl_px > 0.0:
                if side == "buy" and target_sl_px <= current_sl_px + 1e-12:
                    return
                if side == "sell" and target_sl_px >= current_sl_px - 1e-12:
                    return

            open_pos = self._get_open_positions()
            pos = open_pos.get(symbol)
            if not pos:
                logger.warning("TRAIL NATIVE SL %s %s: position introuvable dans le cache", symbol, side)
                return

            qty_raw = pos.get("qty", 0.0)
            try:
                qty = abs(float(qty_raw or 0.0))
            except Exception:
                logger.warning("TRAIL NATIVE SL %s %s: qty invalide %r (pos=%r)", symbol, side, qty_raw, pos)
                return

            if qty <= 0.0:
                return

            side_close = "sell" if side == "buy" else "buy"

            sl_oid = guard.get("sl_oid")
            if not sl_oid:
                # Étape 1: tenter via le cache local (rapide)
                stop_orders = self._get_stop_orders_cached(symbol)
                if len(stop_orders) == 1:
                    sl_oid = next(iter(stop_orders.keys()))
                    guard["sl_oid"] = sl_oid
                    logger.info("TRAIL NATIVE SL %s %s: sl_oid récupéré depuis cache: %s", symbol, side, sl_oid)
                elif len(stop_orders) > 1:
                    # Plusieurs stop orders, on essaie de matcher par prix
                    logger.info("TRAIL NATIVE SL %s %s: %d stop orders, tentative de matching", symbol, side, len(stop_orders))
                    # Prix de référence pour le matching: native_sl_price si connu, sinon entry
                    known_sl_price = float(guard.get("native_sl_price", 0) or 0)
                    best_oid = None
                    best_diff = float("inf")
                    for oid_key, order_data in stop_orders.items():
                        try:
                            order_trig = float(
                                order_data.get("triggerPx", 0)
                                or order_data.get("trigger_px", 0)
                                or order_data.get("limit_px", 0)
                                or 0
                            )
                            tpsl = str(order_data.get("tpsl", "")).lower()
                            if not tpsl:
                                # Fallback: déduire depuis orderType string (après switch frontend_open_orders)
                                ot_str = str(order_data.get("orderType", "")).lower()
                                if "stop" in ot_str:
                                    tpsl = "sl"
                                elif "take profit" in ot_str or " tp" in ot_str:
                                    tpsl = "tp"
                            # Matching prioritaire par tpsl explicite
                            if tpsl == "sl":
                                ord_side = str(order_data.get("side", "")).lower()
                                if ord_side in ("a", "sell") and side == "buy":
                                    sl_oid = int(oid_key)
                                    break
                                if ord_side in ("b", "buy") and side == "sell":
                                    sl_oid = int(oid_key)
                                    break
                            # Fallback: matching par prix (le plus proche du SL attendu)
                            # Pour BUY: SL initial sous entry; pour SELL: SL initial au-dessus de entry
                            # mais le SL trailing peut être de n'importe quel côté → on use known_sl_price
                            if order_trig > 0 and not sl_oid:
                                if known_sl_price > 0:
                                    diff = abs(order_trig - known_sl_price)
                                elif side == "buy":
                                    diff = abs(order_trig - entry) if order_trig < entry else float("inf")
                                else:
                                    # SELL: SL initial au-dessus de entry; trailing SL peut être sous entry
                                    diff = abs(order_trig - entry)
                                if diff < best_diff:
                                    best_diff = diff
                                    best_oid = int(oid_key)
                        except (ValueError, TypeError):
                            continue
                    
                    if not sl_oid and best_oid:
                        sl_oid = best_oid
                        logger.info("TRAIL NATIVE SL %s %s: sl_oid résolu par matching prix: %s", symbol, side, sl_oid)
                    
                    if sl_oid:
                        guard["sl_oid"] = sl_oid
                
                if not sl_oid:
                    # Étape 2: appeler le résolveur complet (avec retries) si dispo
                    try:
                        if hasattr(self.exchange, '_client') and hasattr(self.exchange._client, '_resolve_trigger_oids'):
                            # Utiliser native_sl_price si connu (SL déjà déplacé), sinon SL initial estimé
                            known_sl = float(guard.get("native_sl_price", 0) or 0)
                            if known_sl > 0:
                                expected_sl = known_sl
                            else:
                                sl_pct = float(getattr(self, "_scalp_sl_pct", 0.015))
                                if side == "buy":
                                    expected_sl = entry * (1.0 - sl_pct / leverage)
                                else:
                                    expected_sl = entry * (1.0 + sl_pct / leverage)
                            
                            logger.info("TRAIL NATIVE SL %s %s: tentative résolution OID via fallback (expected_sl=%.6f)", 
                                        symbol, side, expected_sl)
                            resolved = self.exchange._client._resolve_trigger_oids(
                                coin=symbol,
                                is_long=(side == "buy"),
                                tp_price=None,  # on ne cherche que le SL
                                sl_price=expected_sl,
                                max_retries=3,
                                retry_delay=0.3,
                            )
                            if resolved.get("sl_oid"):
                                sl_oid = resolved["sl_oid"]
                                guard["sl_oid"] = sl_oid
                                logger.info("TRAIL NATIVE SL %s %s: sl_oid récupéré via fallback: %s", symbol, side, sl_oid)
                    except Exception as e:
                        logger.debug("TRAIL NATIVE SL %s %s: fallback résolution échoué: %r", symbol, side, e)
                
                if not sl_oid:
                    # Aucun SL natif sur HL : en placer un nouveau au lieu de simplement ignorer.
                    # On marque last_protected_roe pour éviter un double placement au prochain tick.
                    guard["last_protected_roe"] = float(protected_roe)
                    try:
                        res = self.exchange.place_tpsl_native(
                            symbol=symbol,
                            side=side,
                            qty=qty,
                            entry=0.0,
                            tp=None,
                            sl=target_sl_px,
                        )
                        new_oid = res.get("sl_oid") if isinstance(res, dict) else None
                        if new_oid:
                            guard["sl_oid"] = int(new_oid)
                            guard["native_sl_price"] = float(target_sl_px)
                            logger.info(
                                "TRAIL NATIVE SL %s %s: SL manquant → placé @ %.4f oid=%s",
                                symbol, side, target_sl_px, new_oid,
                            )
                        else:
                            logger.warning("TRAIL NATIVE SL %s %s: SL placé mais oid non résolu (res=%s)", symbol, side, res)
                    except Exception as e:
                        logger.warning("TRAIL NATIVE SL %s %s: placement SL échoué: %r", symbol, side, e)
                    return

            new_sl_oid = self.exchange.modify_stop_trigger_order(
                order_id=str(sl_oid),
                symbol=symbol,
                side_close=side_close,
                qty=qty,
                trigger_price=target_sl_px,
            )

            if new_sl_oid is None:
                logger.warning("TRAIL NATIVE SL MODIFY FAILED %s %s oid=%s qty=%.6f target=%.4f", side.upper(), symbol, sl_oid, qty, target_sl_px)
                return

            # HL cancel+recreate : le nouvel OID est retourné directement par modify_order.
            # On met à jour le guard immédiatement sans re-résolution ni VERIFY.
            guard["sl_oid"] = int(new_sl_oid)
            guard["native_sl_price"] = float(target_sl_px)
            guard["last_protected_roe"] = float(prot)

            logger.info(
                "TRAIL NATIVE SL MODIFY %s %s qty=%.6f protected=%.3f%% -> SL=%.4f (oid=%s→%s | entry=%.4f cur=%.4f lev=%.2f)",
                side.upper(),
                symbol,
                qty,
                prot * 100.0,
                target_sl_px,
                sl_oid,
                new_sl_oid,
                entry,
                current_price,
                lev,
            )

        except Exception as e:
            logger.warning(
                "TRAIL NATIVE SL guard error %s: %r (side=%s prot=%.4f%%)",
                symbol,
                e,
                guard.get("side", "?"),
                protected_roe * 100.0 if protected_roe is not None else -1.0,
            )

    def _monitor_trailing(self) -> None:
        if self.simulation:
            return

        open_pos = self._get_open_positions()

        # Cleanup mutex emergency : libère les symboles fermés (HL a confirmé)
        # ou expirés (TTL 30s dépassé sans confirmation = on retentera).
        if self._emergency_closing:
            now_ts = time.time()
            with self._emergency_lock:
                expired = [
                    s for s, ts in self._emergency_closing.items()
                    if s not in open_pos or (now_ts - ts) > 30.0
                ]
                for s in expired:
                    self._emergency_closing.pop(s, None)

        for symbol, guard in list(self._trail_guards.items()):
            if symbol not in open_pos:
                # FIX #4: Période de grâce avant suppression
                age = time.time() - float(guard.get("created_at", 0))
                if age < GUARD_GRACE_PERIOD_SEC:
                    logger.debug(
                        "[TRAIL] %s en période de grâce (%.1fs < %.1fs), guard conservé",
                        symbol, age, GUARD_GRACE_PERIOD_SEC
                    )
                    continue
                self._trail_guards.pop(symbol, None)
                continue

            # Préfère l'entry et la side de la position actuelle au cache HL plutôt
            # que celle figée du guard : si le grid re-cycle (ferme et ré-ouvre à
            # un nouveau prix), l'entry change mais le guard reste sur l'ancien.
            # Sinon les ROE calculés divergent de la réalité (cf. incident BNB).
            pos_now = open_pos.get(symbol, {})
            entry_live = float(pos_now.get("entry", 0) or 0)
            side_live = str(pos_now.get("side", "")).lower()
            entry = entry_live if entry_live > 0 else guard["entry"]
            side = side_live if side_live in {"buy", "sell"} else guard["side"]
            leverage = float(pos_now.get("leverage", guard.get("leverage", MAX_LEVERAGE)) or MAX_LEVERAGE)
            tp_arm = float(guard.get("tp_arm", TP_ARM_PCT))

            # Si la direction a changé (grid flip), le guard est obsolète : on le drop.
            # Le prochain cycle le recréera correctement via _register_trail_guard ou
            # _recover_or_place_sl si c'est un grid (ou autre).
            if side_live and side_live != guard.get("side", side_live):
                logger.warning(
                    "TRAIL %s: direction changed (%s → %s), guard dropped",
                    symbol, guard.get("side"), side_live,
                )
                self._trail_guards.pop(symbol, None)
                continue

            if entry <= 0:
                continue

            price = self._get_current_price(symbol, entry)
            pnl_brut = (
                (price - entry) / entry
                if side == "buy"
                else (entry - price) / entry
            )
            pnl_pct = pnl_brut * leverage

            # ── EMERGENCY HARD EXIT ──────────────────────────────────────
            # Garde-fou capital : si la perte dépasse EMERGENCY_LOSS_ROE_MULT × SL_PCT,
            # ferme la position de force, indépendamment de armed/sl_oid.
            # Couvre : SL natif perdu, recovery abandonnée, cache stale, flip bloqué.
            emergency_threshold = -(EMERGENCY_LOSS_ROE_MULT * SCALP_SL_PNL_PCT)
            if pnl_pct <= emergency_threshold:
                # Anti-double-fire : skippe si fermeture déjà en vol (TTL 30s).
                now_ts = time.time()
                with self._emergency_lock:
                    last_ts = self._emergency_closing.get(symbol, 0.0)
                    if now_ts - last_ts < 30.0:
                        continue
                    self._emergency_closing[symbol] = now_ts
                logger.critical(
                    "EMERGENCY EXIT %s ROE=%.3f%% <= %.3f%% — force close (entry=%.4f price=%.4f side=%s)",
                    symbol, pnl_pct * 100, emergency_threshold * 100, entry, price, side.upper(),
                )
                pos_data = open_pos[symbol]  # garanti présent (early-continue plus haut)
                try:
                    closed = self._close_position_market(symbol, pos_data)
                    if closed:
                        self._mark_trade_exit(symbol, "emergency_loss")
                        self._trail_guards.pop(symbol, None)
                        self._entry_regime_ctx.pop(symbol, None)
                except Exception as e:
                    logger.error("EMERGENCY EXIT %s échec: %r", symbol, e)
                finally:
                    # Libère le mutex au prochain tick — laisse le temps au cache HL
                    # de confirmer la fermeture. Si on libérait tout de suite, le
                    # tick suivant pourrait revoir la position et re-fire.
                    pass
                continue

            if pnl_pct > guard["best_pnl_pct"]:
                guard["best_pnl_pct"] = pnl_pct

            best = guard["best_pnl_pct"]

            # Armement du trailing
            if not guard["trail_armed"] and best >= tp_arm:
                guard["trail_armed"] = True
                logger.info(
                    "TRAIL ARM %s ROE_pic=%.3f%% (TP_arm=%.2f%%)",
                    symbol,
                    best * 100,
                    tp_arm * 100,
                )

            # Calcul du ROE protégé (breakeven + crans)
            protected_roe = 0.0
            if guard["trail_armed"]:
                raw_steps = (best - tp_arm) / TRAIL_STEP_ROE if TRAIL_STEP_ROE > 0 else 0.0
                n_steps = max(0, int(raw_steps))
                protected_roe = TRAIL_BREAKEVEN_ROE + (n_steps * TRAIL_STEP_ROE)
                if protected_roe > best:
                    protected_roe = best

            # Mise à jour du SL natif à effet cliquet
            if guard["trail_armed"] and protected_roe >= 0.0:
                last_protected = guard.get("last_protected_roe")
                last_modify_ts = guard.get("last_modify_ts", 0.0)
                now = time.time()
                
                # Throttle : on ne modifie que si le ROE protégé a vraiment changé
                # ET qu'au moins 5 secondes se sont écoulées depuis le dernier modify
                roe_changed = (last_protected is None) or (protected_roe > float(last_protected) + 1e-9)
                time_elapsed = (now - last_modify_ts) >= 5.0
                
                if roe_changed and time_elapsed:
                    self._update_native_trailing_sl(
                        symbol=symbol,
                        guard=guard,
                        protected_roe=protected_roe,
                        current_price=price,
                    )
                    guard["last_modify_ts"] = now

            logger.info(
                "TRAIL %s %s ROE=%.3f%% best=%.3f%% protected=%.3f%% armed=%s | TP_arm=%.2f%% lev=%.1fx brut=%.3f%%",
                side.upper(),
                symbol,
                pnl_pct * 100,
                best * 100,
                protected_roe * 100,
                guard["trail_armed"],
                tp_arm * 100,
                leverage,
                pnl_brut * 100,
            )

        # ── EMERGENCY HARD EXIT pour positions HORS trail_guards ─────────────
        # Couvre les positions ouvertes par le grid_manager qui ne passent pas
        # par _register_trail_guard. Sans ce filet, un grid breakout violent
        # n'a aucune protection (ni SL natif, ni trail).
        emergency_threshold = -(EMERGENCY_LOSS_ROE_MULT * SCALP_SL_PNL_PCT)
        for symbol, pos in list(open_pos.items()):
            if symbol in self._trail_guards:
                continue  # déjà couvert par la boucle précédente
            try:
                side = str(pos.get("side", "")).lower()
                entry = float(pos.get("entry", 0) or 0)
                qty = abs(float(pos.get("qty", 0) or 0))
                leverage = float(pos.get("leverage", MAX_LEVERAGE) or MAX_LEVERAGE)
                if side not in {"buy", "sell"} or entry <= 0 or qty <= 0:
                    continue
                # Skip dust (HL refuse les ordres < $10)
                price = self._get_current_price(symbol, entry)
                if price <= 0 or qty * price < 10.0:
                    continue
                pnl_brut = (price - entry) / entry if side == "buy" else (entry - price) / entry
                pnl_pct = pnl_brut * leverage
                if pnl_pct <= emergency_threshold:
                    now_ts = time.time()
                    with self._emergency_lock:
                        last_ts = self._emergency_closing.get(symbol, 0.0)
                        if now_ts - last_ts < 30.0:
                            continue
                        self._emergency_closing[symbol] = now_ts
                    logger.critical(
                        "EMERGENCY EXIT (grid/orphan) %s ROE=%.3f%% <= %.3f%% — force close (entry=%.4f price=%.4f side=%s)",
                        symbol, pnl_pct * 100, emergency_threshold * 100, entry, price, side.upper(),
                    )
                    closed = self._close_position_market(symbol, pos)
                    if closed:
                        self._mark_trade_exit(symbol, "emergency_loss_grid")
                        # Au cas où grid_manager a un état pour ce symbole, on le désactive
                        try:
                            if hasattr(self, "grid_manager") and self.grid_manager.is_active(symbol):
                                self.grid_manager.deactivate(symbol, cancel=True)
                                logger.warning("EMERGENCY EXIT %s: grille désactivée + ordres annulés", symbol)
                        except Exception as ge:
                            logger.warning("EMERGENCY EXIT %s: désactivation grid échouée: %r", symbol, ge)
            except Exception as e:
                logger.warning("EMERGENCY EXIT (orphan) %s erreur: %r", symbol, e)

    def _register_trail_guard(
        self,
        symbol: str,
        side: str,
        entry: float,
        leverage: float,
        scalper_prices: Optional[Dict] = None,
        sl_oid: Optional[str] = None,
        tp_oid: Optional[str] = None,
    ) -> None:
        tp_arm = TP_ARM_PCT
        raw_tp = None

        if scalper_prices:
            try:
                raw_tp = scalper_prices.get("pnl_tp")
                raw_tp_f = float(raw_tp or 0)
                if raw_tp_f > 0:
                    # FIX #5: Trail s'arme à 40% du TP cible, pas 100%
                    # Avec floor à TP_ARM_PCT (0.30%) pour ne pas s'armer trop tôt
                    full_tp_roe = raw_tp_f / 100.0 / max(1.0, leverage)
                    tp_arm = max(TP_ARM_PCT, full_tp_roe * 0.40)
                logger.info(
                    "TRAIL INPUT %s | raw_tp=%s lev=%.1fx -> tp_arm=%.4f (40%% du TP cible, floor %.4f)",
                    symbol,
                    raw_tp,
                    leverage,
                    tp_arm,
                    TP_ARM_PCT,
                )
            except Exception as e:
                logger.warning(
                    "_register_trail_guard: impossible de lire pnl_tp (raw_tp=%r): %r",
                    raw_tp,
                    e,
                )

        self._trail_guards[symbol] = {
            "side": side,
            "entry": float(entry),
            "leverage": float(leverage),
            "tp_arm": float(tp_arm),
            "best_pnl_pct": 0.0,
            "trail_armed": False,

            # Trailing natif cliquet
            "native_sl_price": 0.0,
            "last_protected_roe": None,
            "breakeven_moved": False,

            # Oids des ordres TP/SL (pour modify_stop_trigger_order)
            "sl_oid": sl_oid,
            "tp_oid": tp_oid,

            # FIX #4: Race condition protection
            # Timestamp de création pour éviter qu'un sync HL en retard supprime
            # un guard fraîchement créé (avant que la position apparaisse dans le cache HL).
            "created_at": time.time(),
        }

        logger.info(
            "TRAIL GUARD enregistré %s %s | entry=%.4f lev=%.1fx TP_arm=%.2f%% ROE | sl_oid=%s tp_oid=%s",
            side.upper(),
            symbol,
            entry,
            leverage,
            tp_arm * 100,
            sl_oid or "?",
            tp_oid or "?",
        )

    def _cancel_orphan_triggers(self, symbol: str, expected_close_side: str) -> int:
        """
        Annule les ordres trigger reduce_only dont le côté ne correspond PAS au
        close_side de la position actuelle.

        Un SL/TP est de l'autre côté de la position : pour fermer un short on BUY,
        pour fermer un long on SELL. Si la position direction a changé (long → short
        ou inverse) sans que les anciens SL/TP soient annulés, ils restent en zombi.

        Returns: nombre d'ordres annulés.
        """
        try:
            with self._hl_cache_lock:
                cached_orders = list(self._hl_cache.get("open_orders", []))
        except Exception as e:
            logger.warning("[ORPHAN] %s: lecture cache échouée: %r", symbol, e)
            return 0

        cancelled = 0
        for o in cached_orders:
            if not isinstance(o, dict):
                continue
            order_coin = str(o.get("coin", "")).upper()
            if order_coin != symbol.upper() and not order_coin.startswith(symbol.upper() + "-"):
                continue
            tpsl = str(o.get("tpsl", "")).lower()
            is_trigger = (
                bool(o.get("isTrigger", False))
                or tpsl in ("tp", "sl")
                or "trigger" in str(o.get("orderType", "")).lower()
            )
            if not is_trigger:
                continue
            if not bool(o.get("reduceOnly", False)):
                continue  # ne touche pas aux ordres non-reduce_only (ex: limit grid d'entrée)

            o_side = str(o.get("side", "")).lower()
            o_side_norm = "buy" if o_side in ("b", "buy") else ("sell" if o_side in ("a", "sell") else o_side)
            if o_side_norm == expected_close_side:
                continue  # bonne direction, on garde

            oid = o.get("oid")
            if oid is None:
                continue
            try:
                ok = self.exchange.cancel_order(str(oid))
                if getattr(ok, "ok", False) or ok is True:
                    cancelled += 1
                    trigger_px = o.get("triggerPx") or o.get("limitPx") or "?"
                    logger.warning(
                        "[ORPHAN] %s: ordre zombi annulé oid=%s side=%s tpsl=%s px=%s",
                        symbol, oid, o_side_norm, tpsl or "?", trigger_px,
                    )
            except Exception as e:
                logger.warning("[ORPHAN] %s: cancel oid=%s échec: %r", symbol, oid, e)

        return cancelled

    def _recover_or_place_sl(
        self,
        symbol: str,
        side: str,
        entry: float,
        qty: float,
        leverage: float,
        scalp_sl_pnl_pct: float,
    ) -> Optional[str]:
        """
        Appelé lors du recovery quand sl_oid=None après _resolve_trigger_oids.

        Étape 1 — scan large du cache d'ordres ouverts : si un ordre trigger
        correspond au bon côté de fermeture ET est du côté adverse de l'entrée,
        on l'adopte comme SL (évite de poser un doublon).

        Étape 2 — si aucun SL trouvé : placement d'un nouveau SL via
        place_tpsl_native. Garde-fou : on annule si le prix actuel a déjà
        dépassé le niveau SL calculé (évite un fill immédiat).

        Returns: sl_oid (str) ou None.
        """
        is_long = (side == "buy")
        close_side = "sell" if is_long else "buy"
        est_sl = (
            entry * (1.0 - scalp_sl_pnl_pct / max(1.0, leverage))
            if is_long
            else entry * (1.0 + scalp_sl_pnl_pct / max(1.0, leverage))
        )

        # --- Étape 1 : scan large du cache ---
        try:
            with self._hl_cache_lock:
                cached_orders = list(self._hl_cache.get("open_orders", []))

            best: Optional[tuple] = None  # (diff, oid_int, trigger_px)
            for o in cached_orders:
                if not isinstance(o, dict):
                    continue
                order_coin = str(o.get("coin", "")).upper()
                if order_coin != symbol.upper() and not order_coin.startswith(symbol.upper() + "-"):
                    continue
                tpsl = str(o.get("tpsl", "")).lower()
                is_trigger = (
                    bool(o.get("isTrigger", False))
                    or tpsl in ("tp", "sl")
                    or "trigger" in str(o.get("orderType", "")).lower()
                )
                if not is_trigger:
                    continue
                o_side = str(o.get("side", "")).lower()
                o_side_norm = "buy" if o_side in ("b", "buy") else ("sell" if o_side in ("a", "sell") else o_side)
                if o_side_norm != close_side:
                    continue
                try:
                    trigger_px = float(o.get("triggerPx") or o.get("limitPx") or 0)
                except (ValueError, TypeError):
                    continue
                if trigger_px <= 0:
                    continue
                oid = o.get("oid")
                if oid is None:
                    continue
                is_adverse = (is_long and trigger_px < entry) or (not is_long and trigger_px > entry)
                if tpsl == "sl" or is_adverse:
                    diff = abs(trigger_px - est_sl)
                    if best is None or diff < best[0]:
                        best = (diff, int(oid), trigger_px)

            if best is not None:
                _, found_oid, found_px = best
                logger.info(
                    "[RECOVERY] %s: SL existant retrouvé (scan large) oid=%s trigger=%.4f",
                    symbol, found_oid, found_px,
                )
                return str(found_oid)

        except Exception as e:
            logger.warning("[RECOVERY] %s: scan large SL échoué: %r", symbol, e)

        # --- Étape 2 : placement d'un nouveau SL ---
        try:
            current_px = self._get_current_price(symbol, entry)
            sl_already_passed = (is_long and current_px <= est_sl) or (not is_long and current_px >= est_sl)

            # FIX permanent : si le SL théorique est dépassé, on ne ferme PAS la position
            # silencieusement (cf. bug BTC -5.88% du 2026-05-04). On place un SL serré
            # à current ± SL_FALLBACK_BUFFER_PCT pour verrouiller la perte courante au
            # lieu de laisser la position dériver. L'emergency exit dans _monitor_trailing
            # sert de filet supplémentaire si même ce fallback ne tient pas.
            if sl_already_passed:
                fallback_sl = (
                    current_px * (1.0 - SL_FALLBACK_BUFFER_PCT) if is_long
                    else current_px * (1.0 + SL_FALLBACK_BUFFER_PCT)
                )
                logger.warning(
                    "[RECOVERY] %s: SL théorique (%.4f) dépassé par prix (%.4f) — fallback SL serré à %.4f (buffer=%.2f%%)",
                    symbol, est_sl, current_px, fallback_sl, SL_FALLBACK_BUFFER_PCT * 100,
                )
                sl_to_place = fallback_sl
            else:
                sl_to_place = est_sl

            # Nettoyage orphelins : annule tout trigger reduce_only dont le côté
            # NE correspond PAS au close_side de la position actuelle. Ces ordres
            # viennent de positions précédentes (ex: long fermé puis short ouvert)
            # et restent dans l'order book en zombi car positionTpsl ne les remplace
            # pas quand la direction change.
            self._cancel_orphan_triggers(symbol, close_side)

            logger.info(
                "[RECOVERY] %s: placement nouveau SL | side=%s entry=%.4f sl=%.4f qty=%.6f lev=%.1fx",
                symbol, side, entry, sl_to_place, qty, leverage,
            )
            result = self.exchange.place_tpsl_native(
                symbol=symbol,
                side=side,
                qty=qty,
                entry=entry,
                tp=None,
                sl=sl_to_place,
            )
            new_oid = result.get("sl_oid")
            if new_oid is not None:
                logger.info("[RECOVERY] %s: nouveau SL PLACÉ oid=%s sl=%.4f", symbol, new_oid, sl_to_place)
                return str(new_oid)
            logger.warning("[RECOVERY] %s: SL placé mais OID non résolu — trail inactif", symbol)
            return None

        except Exception as e:
            logger.error("[RECOVERY] %s: placement nouveau SL ÉCHOUÉ: %r", symbol, e)
            return None

    def _recover_trail_guards(self) -> None:
        """
        Recrée les trail guards pour les positions déjà ouvertes au démarrage du bot.

        - tp_arm calculé avec la formule fix #5 (SCALP_TP_PNL_PCT comme raw_tp par défaut)
        - sl_oid/tp_oid résolus via _resolve_trigger_oids (avec retries)
        - Si résolution OID échoue : guard créé quand même avec sl_oid=None
          (le mécanisme de recovery a posteriori dans _update_native_trailing_sl prendra le relais)
        - best_pnl_pct initialisé au ROE actuel si la position est déjà en gain
        - trail_armed reste toujours False au démarrage (s'armera quand best >= tp_arm)
        - Dust positions (notional < $10) : skippées
        """
        open_pos = self._get_open_positions()
        if not open_pos:
            logger.info("[RECOVERY] Aucune position ouverte à récupérer.")
            return

        logger.info("[RECOVERY] %d position(s) détectée(s) — recréation des trail guards.", len(open_pos))

        scalp_tp_pnl_pct = float(getattr(SETTINGS, "SCALP_TP_PNL_PCT", 0.03))
        scalp_sl_pnl_pct = float(getattr(SETTINGS, "SCALP_SL_PNL_PCT", 0.015))

        for symbol, pos in open_pos.items():
            # Ne pas écraser un guard déjà présent en mémoire
            if symbol in self._trail_guards:
                logger.info("[RECOVERY] %s: trail guard déjà présent, ignoré.", symbol)
                continue

            try:
                side = str(pos.get("side", "")).lower()
                entry = float(pos.get("entry", 0) or 0)
                leverage = float(pos.get("leverage", MAX_LEVERAGE) or MAX_LEVERAGE)

                if side not in {"buy", "sell"} or entry <= 0:
                    logger.warning("[RECOVERY] %s: données invalides (side=%s entry=%.4f), ignoré.", symbol, side, entry)
                    continue

                # Dust check : Hyperliquid refuse les ordres < $10
                qty = float(pos.get("qty", 0) or 0)
                mark_price = self._get_mark_price(symbol)
                if mark_price > 0 and qty > 0 and qty * mark_price < 10.0:
                    logger.warning(
                        "[RECOVERY] %s: dust position (qty=%.6f notional=$%.2f < $10), ignoré.",
                        symbol, qty, qty * mark_price,
                    )
                    continue

                # Calcul tp_arm avec la formule fix #5
                # SCALP_TP_PNL_PCT est en décimal (0.03 = 3%) → convertir en % pour la formule
                raw_tp_pct = scalp_tp_pnl_pct * 100.0
                full_tp_roe = raw_tp_pct / 100.0 / max(1.0, leverage)
                tp_arm = max(TP_ARM_PCT, full_tp_roe * 0.40)

                # Résolution sl_oid / tp_oid via _resolve_trigger_oids
                sl_oid: Optional[str] = None
                tp_oid: Optional[str] = None
                try:
                    is_long = (side == "buy")
                    # Prix estimés pour le matching (tolérance 0.5% dans _resolve_trigger_oids)
                    if is_long:
                        est_sl = entry * (1.0 - scalp_sl_pnl_pct / max(1.0, leverage))
                        est_tp = entry * (1.0 + scalp_tp_pnl_pct / max(1.0, leverage))
                    else:
                        est_sl = entry * (1.0 + scalp_sl_pnl_pct / max(1.0, leverage))
                        est_tp = entry * (1.0 - scalp_tp_pnl_pct / max(1.0, leverage))

                    resolved = self.exchange._client._resolve_trigger_oids(
                        coin=symbol,
                        is_long=is_long,
                        tp_price=est_tp,
                        sl_price=est_sl,
                        max_retries=5,
                        retry_delay=0.3,
                    )
                    if resolved.get("sl_oid") is not None:
                        sl_oid = str(resolved["sl_oid"])
                    if resolved.get("tp_oid") is not None:
                        tp_oid = str(resolved["tp_oid"])
                except Exception as e:
                    logger.warning(
                        "[RECOVERY] %s: résolution OID échouée: %r — guard créé sans sl_oid (recovery a posteriori actif)",
                        symbol, e,
                    )

                # Fallback : si sl_oid non résolu, chercher un SL existant ou en placer un nouveau
                if sl_oid is None:
                    sl_oid = self._recover_or_place_sl(
                        symbol=symbol,
                        side=side,
                        entry=entry,
                        qty=qty,
                        leverage=leverage,
                        scalp_sl_pnl_pct=scalp_sl_pnl_pct,
                    )

                # Créer le trail guard (trail_armed toujours False au démarrage)
                self._register_trail_guard(
                    symbol=symbol,
                    side=side,
                    entry=entry,
                    leverage=leverage,
                    scalper_prices={"pnl_tp": raw_tp_pct},
                    sl_oid=sl_oid,
                    tp_oid=tp_oid,
                )

                # Initialiser best_pnl_pct au ROE actuel si la position est déjà en gain
                # trail_armed reste False — il s'armera quand best dépassera tp_arm
                price = self._get_current_price(symbol, entry)
                if price > 0 and entry > 0:
                    pnl_brut = (price - entry) / entry if side == "buy" else (entry - price) / entry
                    pnl_pct = pnl_brut * leverage
                    guard = self._trail_guards.get(symbol)
                    if guard and pnl_pct > 0:
                        guard["best_pnl_pct"] = pnl_pct

                cur_roe = self._trail_guards.get(symbol, {}).get("best_pnl_pct", 0.0)
                logger.info(
                    "TRAIL RECOVERY: trail guard recréé pour %s | entry=%.4f lev=%.1fx tp_arm=%.2f%% ROE cur_roe=%.3f%% | sl_oid=%s tp_oid=%s",
                    symbol, entry, leverage, tp_arm * 100, cur_roe * 100,
                    sl_oid or "None", tp_oid or "None",
                )

            except Exception as e:
                logger.warning("[RECOVERY] %s: erreur inattendue: %r", symbol, e)

    def _recent_symbol_trades(self, symbol: str, lookback: int = FREEZE_LOOKBACK_TRADES) -> List[Dict]:
        try:
            trades = self.memory.get_recent_trades(max(lookback * 3, 20)) or []
        except Exception:
            trades = self.memory.get("trade_history", [])[-max(lookback * 3, 20):]

        out = [t for t in trades if str(t.get("symbol", "")).upper() == symbol.upper()]
        return out[-lookback:]
    
    def _freeze_symbol(self, symbol: str, seconds: int, cause: str, stats: Optional[Dict] = None) -> None:
        now = time.time()
        until = now + max(60, int(seconds))
        prev = self._freeze_until.get(symbol, 0.0)

        if until <= prev:
            return

        self._freeze_until[symbol] = until
        remain = int(until - now)

        if stats:
            logger.warning(
                "FREEZE %s %ds cause=%s pnl=%.4f count=%s winrate=%.2f consec=%s",
                symbol,
                remain,
                cause,
                float(stats.get("pnl_sum", 0.0) or 0.0),
                stats.get("count_str", "?"),
                float(stats.get("winrate", 0.0) or 0.0),
                int(stats.get("consec_losses", 0) or 0),
            )
        else:
            logger.warning("FREEZE %s %ds cause=%s", symbol, remain, cause)

    def _assess_symbol_penalty(self, symbol: str) -> Optional[Dict]:
        """
        Évalue si un symbole doit être freeze.

        FIX #6: La logique a été restructurée pour que le learner soit consulté EN PREMIER,
        avant la vérification du nombre de trades récents. Auparavant, si recent < FREEZE_MIN_TRADES,
        on retournait None immédiatement, ce qui empêchait le learner de freezer un symbole
        catastrophique (ex: ETH winrate=0% n=3) si pas de trades très récents.

        Trois niveaux de freeze :
        - learner_very_bad : winrate quasi-nul → freeze 2× FREEZE_SEC_LONG
        - learner_bad      : winrate < 40% avec PnL négatif → freeze FREEZE_SEC_LONG
        - classique        : pertes consécutives ou pattern de pertes → freeze short/long
        """
        # Étape 1 : Lire le profil learner EN PREMIER (avant tout)
        learner_very_bad = False
        learner_bad = False
        lp_samples = 0
        lp_wr = 0.0
        lp_avg = 0.0
        try:
            regime = self.memory.get_regime() or {}
            regime_key = f"{regime.get('trend', 'range')}_{regime.get('volatility', 'medium')}"
            learner_profile = self.memory.get_scalper_profile(symbol, regime_key) or {}
            lp_samples = int(learner_profile.get("samples", 0) or 0)
            lp_avg = float(learner_profile.get("avg_pnl", 0.0) or 0.0)
            lp_wr = float(learner_profile.get("winrate", 0.0) or 0.0)

            # Très mauvais : winrate quasi-nul → freeze long renforcé
            learner_very_bad = (
                (lp_samples >= 3 and lp_wr <= 0.001)  # 0% winrate sur n>=3
                or (lp_samples >= 5 and lp_wr < 0.25)  # <25% winrate sur n>=5
            )
            # Mauvais classique : winrate <= 40% avec PnL négatif
            learner_bad = lp_samples >= 3 and lp_avg < 0 and lp_wr <= 0.40
        except Exception as e:
            logger.debug("_assess_symbol_penalty learner lookup error %s: %r", symbol, e)

        # Étape 2 : Si le learner dit "très mauvais", freeze IMMÉDIAT (sans regarder les trades récents)
        if learner_very_bad:
            return {
                "freeze_sec": FREEZE_SEC_LONG * 2,
                "cause": "learner_very_bad",
                "pnl_sum": lp_avg,
                "winrate": lp_wr,
                "consec_losses": 0,
                "bad_count": lp_samples,
                "count_str": f"learner: wr={lp_wr*100:.1f}% n={lp_samples}",
                "recent": [],
            }

        # Étape 3 : Si learner_bad, on freeze long même sans trades récents
        if learner_bad:
            return {
                "freeze_sec": FREEZE_SEC_LONG,
                "cause": "learner_bad",
                "pnl_sum": lp_avg,
                "winrate": lp_wr,
                "consec_losses": 0,
                "bad_count": lp_samples,
                "count_str": f"learner: wr={lp_wr*100:.1f}% n={lp_samples} avg_pnl={lp_avg:.4f}",
                "recent": [],
            }

        # Étape 4 : Logique classique sur trades récents
        recent = self._recent_symbol_trades(symbol, FREEZE_LOOKBACK_TRADES)
        if len(recent) < FREEZE_MIN_TRADES:
            return None

        pnls = [float(t.get("pnl", 0.0) or 0.0) for t in recent]
        losses = [p for p in pnls if p <= 0]
        wins = [p for p in pnls if p > 0]

        consec_losses = 0
        for p in reversed(pnls):
            if p <= 0:
                consec_losses += 1
            else:
                break

        pnl_sum = sum(pnls)
        winrate = len(wins) / max(1, len(pnls))
        bad_count = len(losses)

        short_freeze = consec_losses >= FREEZE_CONSEC_LOSSES
        long_freeze = (
            bad_count >= FREEZE_BAD_COUNT
            and winrate <= FREEZE_WINRATE_MAX
            and pnl_sum <= FREEZE_PNL_SUM_MAX
        )

        if not short_freeze and not long_freeze:
            return None

        return {
            "freeze_sec": FREEZE_SEC_LONG if long_freeze else FREEZE_SEC_SHORT,
            "cause": "losses_recent_long" if long_freeze else "losses_recent_short",
            "pnl_sum": pnl_sum,
            "winrate": winrate,
            "consec_losses": consec_losses,
            "bad_count": bad_count,
            "count_str": f"{bad_count}/{len(pnls)}",
            "recent": pnls,
        }

    def _extract_volratio(self, tech: Dict) -> float:
        ind = tech.get("indicators", {}) if isinstance(tech, dict) else {}
        for key in ("vol_ratio", "volratio"):
            v = ind.get(key)
            if v is not None:
                try:
                    return float(v)
                except Exception:
                    pass
        return 0.0

    def _skip_reason(self, symbol: str, tech: Dict, open_positions: Dict[str, Dict], new_side: str) -> Optional[str]:
        existing = open_positions.get(symbol)
        if existing:
            existing_side = existing.get("side", "")
            if existing_side == new_side:
                return f"position déjà ouverte dans le même sens ({existing_side})"

        now = time.time()

        freeze_ts = self._freeze_until.get(symbol)
        if freeze_ts is not None and now < freeze_ts:
            return f"freeze symbole actif ({int(freeze_ts - now)}s)"

        penalty = self._assess_symbol_penalty(symbol)
        if penalty:
            self._freeze_symbol(
                symbol,
                penalty["freeze_sec"],
                penalty["cause"],
                stats=penalty,
            )
            return f"freeze symbole {penalty['cause']} ({penalty['count_str']} pnl={penalty['pnl_sum']:.4f})"

        last = self.last_entry_ts.get(symbol)
        if last is not None and (now - last) < COOLDOWN_SEC and not existing:
            return f"cooldown actif ({int(COOLDOWN_SEC - (now - last))}s)"

        if not existing and len(open_positions) >= MAX_OPEN_POSITIONS:
            return f"MAX_OPEN_POSITIONS atteint ({MAX_OPEN_POSITIONS})"

        volratio = self._extract_volratio(tech)
        if volratio < MIN_VOLRATIO:
            return f"vol_ratio trop faible ({volratio:.3f} < {MIN_VOLRATIO:.3f})"

        exit_ts = self._exit_cooldowns.get(symbol)
        if exit_ts is not None and (now - exit_ts) < EXIT_COOLDOWN_SEC:
            return f"cooldown post-exit ({int(EXIT_COOLDOWN_SEC - (now - exit_ts))}s)"

        return None

    def _get_account_value_usdt(self) -> float:
        if self.simulation:
            try:
                bals = self.exchange.get_balances()
                if not bals:
                    return 0.0
                if isinstance(bals, dict):
                    return float(bals.get("USDT", 0.0) or 0.0)
                first = bals[0]
                return float(getattr(first, "free", 0) or 0)
            except Exception as e:
                logger.warning("get_balances error: %r", e)
                return 0.0

        self._assert_hl_cache_fresh()
        with self._hl_cache_lock:
            return float(self._hl_cache.get("account_value_usdt", 0.0) or 0.0)

    def _get_tick_decimals(self, symbol: str) -> int:
        if symbol in self._tick_decimals:
            return self._tick_decimals[symbol]
        try:
            meta = self.exchange._client.get_meta()
            universe = meta.get("meta", {}).get("universe", []) or meta.get("universe", [])
            for u in universe:
                if str(u.get("name", "")).upper() == symbol.upper():
                    sz_dec = int(u.get("szDecimals", 3) or 3)
                    px_dec = max(0, 6 - sz_dec)
                    self._tick_decimals[symbol] = px_dec
                    return px_dec
        except Exception:
            pass

        fallback = {
            "BTC": 1, "ETH": 2, "SOL": 3, "AVAX": 3,
            "ARB": 4, "HYPE": 4, "ATOM": 4, "DYDX": 4,
        }.get(symbol.upper(), 4)
        self._tick_decimals[symbol] = fallback
        return fallback

    def _round_px(self, symbol: str, px: float) -> float:
        return round(float(px), self._get_tick_decimals(symbol))

    def _compute_position_size(self, symbol: str, entry: float, sl: float, confidence: Optional[float] = None) -> float:
        equity = self._get_account_value_usdt()
        if equity <= 0 or entry <= 0 or sl <= 0 or entry == sl:
            return 0.0

        risk_usdt = max(1.0, equity * RISK_PER_TRADE_PCT)
        diff = abs(entry - sl)
        if diff <= 0:
            return 0.0

        qty_raw = risk_usdt / diff
        max_notional = equity * MAX_NOTIONAL_PCT
        if qty_raw * entry > max_notional:
            qty_raw = max_notional / entry

        qty = max(0.0, round(qty_raw, 6))

        if confidence is not None and MIN_CONFIDENCE < 1.0:
            c = max(MIN_CONFIDENCE, min(1.0, float(confidence)))
            conf_factor = SIZING_CONF_FLOOR + (1.0 - SIZING_CONF_FLOOR) * (c - MIN_CONFIDENCE) / (1.0 - MIN_CONFIDENCE)
            qty = round(qty * conf_factor, 6)
        else:
            conf_factor = 1.0

        margin = qty * entry / MAX_LEVERAGE
        equity_est = equity
        logger.info(
            "SIZING %s | equity=%.2f conf=%.2f factor=%.2f risk=%.2f diff=%.5f => qty=%.6f notional=%.2f margin=%.2f (%.1f%%)",
            symbol,
            equity,
            confidence if confidence is not None else 1.0,
            conf_factor,
            risk_usdt,
            diff,
            qty,
            qty * entry,
            margin,
            margin / equity_est * 100 if equity_est > 0 else 0,
        )
        return qty

    def _place_tpsl(self, symbol: str, side: str, qty: float, tp: float, sl: float) -> Dict:
        """Pose TP/SL et retourne {tp_oid, sl_oid} si trouvés dans la réponse."""
        result: Dict = {"tp_oid": None, "sl_oid": None}
        try:
            res = self.exchange.place_tpsl_native(symbol, side, qty, 0.0, tp, sl)
            logger.info("[LIVE] TP/SL attachés %s | tp=%.4f sl=%.4f resp=%s", symbol, tp, sl, res)

            # Extraction des oids depuis la réponse (format fallback ou natif)
            if isinstance(res, dict):
                # Format direct : {tp_oid, sl_oid}
                if res.get("tp_oid") is not None:
                    result["tp_oid"] = str(res["tp_oid"])
                if res.get("sl_oid") is not None:
                    result["sl_oid"] = str(res["sl_oid"])

                # Format détaillé : orders=[{kind:tp,oid:...}, {kind:sl,oid:...}]
                if not result["tp_oid"] or not result["sl_oid"]:
                    for order in res.get("orders", []) or []:
                        if not isinstance(order, dict):
                            continue
                        kind = str(order.get("kind", "")).lower()
                        oid = order.get("oid")
                        if oid is None:
                            continue
                        if kind == "tp" and not result["tp_oid"]:
                            result["tp_oid"] = str(oid)
                        elif kind == "sl" and not result["sl_oid"]:
                            result["sl_oid"] = str(oid)
        except Exception as e:
            logger.warning("[LIVE] TP/SL ECHEC %s: %r", symbol, e)

        return result

    def _recalc_tpsl_from_fill(
        self,
        symbol: str,
        side: str,
        planned_entry: float,
        planned_tp: float,
        planned_sl: float,
        fill_price: float,
    ) -> tuple[float, float]:
        side = side.lower()
        planned_entry = float(planned_entry)
        planned_tp = float(planned_tp)
        planned_sl = float(planned_sl)
        fill_price = float(fill_price)

        if side == "buy":
            tp_dist = max(0.0, planned_tp - planned_entry)
            sl_dist = max(0.0, planned_entry - planned_sl)
            new_tp = fill_price + tp_dist
            new_sl = fill_price - sl_dist
        elif side == "sell":
            tp_dist = max(0.0, planned_entry - planned_tp)
            sl_dist = max(0.0, planned_sl - planned_entry)
            new_tp = fill_price - tp_dist
            new_sl = fill_price + sl_dist
        else:
            return planned_tp, planned_sl

        new_tp = self._round_px(symbol, new_tp)
        new_sl = self._round_px(symbol, new_sl)

        logger.info(
            "[LIVE] RECALC TPSL %s %s | planned_entry=%.4f fill=%.4f | old_tp=%.4f old_sl=%.4f -> new_tp=%.4f new_sl=%.4f",
            side.upper(),
            symbol,
            planned_entry,
            fill_price,
            planned_tp,
            planned_sl,
            new_tp,
            new_sl,
        )
        return new_tp, new_sl

    def _send_live_order(
        self,
        symbol: str,
        side: str,
        entry: float,
        sl: float,
        tp: float,
        leverage: float = 3.0,
        scalper_prices: Optional[Dict] = None,
        confidence: Optional[float] = None,
        regime: Optional[Dict] = None,
    ) -> Optional[Dict]:
        if not math.isfinite(entry) or entry <= 0:
            logger.warning("LIVE %s: entry invalide %s", symbol, entry)
            return None

        leverage = min(float(leverage), MAX_LEVERAGE)
        qty = self._compute_position_size(symbol, entry, sl, confidence=confidence)
        if qty <= 0:
            logger.warning("LIVE %s: taille calculée nulle (entry=%.4f sl=%.4f)", symbol, entry, sl)
            return None
        if qty * entry < 10.5:
            logger.info("SKIP %s — notional trop faible (%.2f USDT < $10.5 min)", symbol, qty * entry)
            return None

        side = side.lower()
        if side not in {"buy", "sell"}:
            logger.warning("LIVE %s: side invalide %s", symbol, side)
            return None

        planned_entry = entry
        planned_tp = tp
        planned_sl = sl

        if regime is None:
            try:
                regime = self.memory.get_regime() or {}
            except Exception:
                regime = {}

        margin_est = qty * entry / leverage
        equity_est = self._get_account_value_usdt()
        logger.info(
            "[LIVE] ENVOI ORDER %s %s qty=%.6f entry=%.4f sl=%.4f tp=%.4f lev=%.1fx | margin=%.2f USDT (%.1f%% equity)",
            side.upper(),
            symbol,
            qty,
            entry,
            sl,
            tp,
            leverage,
            margin_est,
            margin_est / equity_est * 100 if equity_est > 0 else 0,
        )

        try:
            smart = self.exchange.place_order_smart(
                symbol=symbol,
                side=side,
                qty=qty,
                confidence=float(confidence or 0.0),
                regime=regime,
                leverage=int(leverage),
                timeout_sec=float(getattr(SETTINGS, "LIMIT_FILL_TIMEOUT_SEC", 30.0)),
                min_confidence=float(getattr(SETTINGS, "LIMIT_USE_MIN_CONFIDENCE", 0.70)),
                max_spread_pct=float(getattr(SETTINGS, "LIMIT_MAX_SPREAD_PCT", 0.002)),
                ok_volatilities=tuple(getattr(SETTINGS, "LIMIT_OK_VOLATILITY", ("low", "medium"))),
                stale_pct=float(getattr(SETTINGS, "LIMIT_STALE_PCT", 0.003)),
            )
        except Exception as e:
            logger.warning("place_order_smart %s %s: %r", symbol, side, e)
            return None

        if not smart or smart.get("order_type") == "failed":
            logger.warning("[LIVE] %s %s: ordre échoué (smart=%r)", symbol, side, smart)
            return None

        fill_price = float(smart.get("fill_price") or 0.0) or planned_entry
        fill_qty = float(smart.get("fill_qty") or 0.0) or qty
        order_type_used = str(smart.get("order_type", "market"))
        spread_pct = float(smart.get("spread_pct") or 0.0)
        slippage_pct = float(smart.get("slippage_pct") or 0.0)
        effective_leverage = float(leverage or MAX_LEVERAGE)

        # Estimation des frais
        notional = fill_price * fill_qty
        if order_type_used == "limit":
            fee_rate = float(getattr(SETTINGS, "HL_MAKER_FEE_PCT", 0.0001))
        else:
            fee_rate = float(getattr(SETTINGS, "HL_TAKER_FEE_PCT", 0.00045))
        # Si on a la fee réelle reportée par HL, on l'utilise
        real_fee = smart.get("fee")
        fees_usd = float(real_fee) if (real_fee is not None and real_fee > 0) else notional * fee_rate

        logger.info(
            "[ENTRY] %s %s | type=%s fill=%.4f | spread=%.4f%% slippage=%.4f%% | fees_est=$%.4f conf=%.2f",
            side.upper(), symbol,
            order_type_used, fill_price,
            spread_pct * 100, slippage_pct * 100,
            fees_usd, float(confidence or 0.0),
        )

        # Compat : on garde un objet "res" minimal pour les blocs aval qui en dépendent
        class _SmartRes:
            def __init__(self, fill_price, fill_qty, oid):
                self.price = fill_price
                self.qty = fill_qty
                self.order_id = str(oid) if oid is not None else ""
                self.status = "filled"
        res = _SmartRes(fill_price, fill_qty, smart.get("oid"))

        logger.info(
            "[LIVE] Résultat ordre: status=%s id=%s price=%.4f",
            res.status, res.order_id, fill_price,
        )

        adj_tp, adj_sl = self._recalc_tpsl_from_fill(
            symbol=symbol,
            side=side,
            planned_entry=planned_entry,
            planned_tp=planned_tp,
            planned_sl=planned_sl,
            fill_price=fill_price,
        )

        tpsl_oids = self._place_tpsl(symbol, side, fill_qty, adj_tp, adj_sl)
        self._register_trail_guard(
            symbol,
            side,
            fill_price,
            effective_leverage,
            scalper_prices,
            sl_oid=tpsl_oids.get("sl_oid"),
            tp_oid=tpsl_oids.get("tp_oid"),
        )

        return {
            "order": res,
            "fill_price": fill_price,
            "fill_qty": fill_qty,
            "effective_leverage": effective_leverage,
            "tp": adj_tp,
            "sl": adj_sl,
        }

    def _close_position_market(self, symbol: str, existing: Dict) -> bool:
        try:
            qty = abs(float(existing.get("qty", 0)))
            old_side = existing.get("side", "buy")
            close_side = "sell" if old_side == "buy" else "buy"
            if qty <= 0:
                return True

            # FIX #2: Détection des dust positions (notional < $10)
            # Hyperliquid refuse les ordres < $10. Si la position est trop petite,
            # on la marque comme fermée côté bot et on alerte l'utilisateur.
            try:
                mark_price = self._get_mark_price(symbol)
                if mark_price > 0:
                    notional = qty * mark_price
                    if notional < 10.0:
                        logger.warning(
                            "[CLOSE] %s DUST POSITION détectée: qty=%.6f mark=%.6f notional=$%.2f < $10. "
                            "Position non fermable via API. Action manuelle requise (augmenter la taille puis fermer, ou laisser expirer).",
                            symbol, qty, mark_price, notional
                        )
                        # On considère cette position comme "fermée" côté bot pour ne pas bloquer les flips
                        return True
            except Exception as e:
                logger.debug("[CLOSE] %s erreur check dust: %r", symbol, e)

            req = OrderRequest(
                symbol=symbol,
                side=close_side,
                qty=qty,
                order_type="market",
                price=0,
                leverage=MAX_LEVERAGE,
                reduce_only=True,
                client_id=None,
            )
            self.exchange.place_order(req)
            logger.info("[CLOSE] Position %s fermée (side=%s qty=%.6f)", symbol, old_side, qty)
            return True
        except Exception as e:
            error_msg = str(e)
            # Capturer spécifiquement l'erreur dust pour ne pas bloquer
            if "minimum value of $10" in error_msg or "minimum value" in error_msg:
                logger.warning(
                    "[CLOSE] %s DUST POSITION détectée à la fermeture: %s. Considérée comme fermée côté bot.",
                    symbol, error_msg
                )
                return True
            logger.error("[CLOSE] _close_position_market %s: %r", symbol, e)
            return False

    def _handle_flip(
        self,
        symbol: str,
        existing: Dict,
        new_side: str,
        entry: float,
        sl: float,
        tp: float,
        reason: str,
        scalper_prices: Optional[Dict] = None,
        confidence: Optional[float] = None,
    ) -> bool:
        old_side = existing.get("side", "?")
        old_entry = float(existing.get("entry", entry) or entry)

        if old_entry <= 0:
            pnl = 0.0
        elif old_side == "buy":
            pnl = (entry - old_entry) / old_entry
        else:
            pnl = (old_entry - entry) / old_entry

        logger.info(
            "[FLIP] %s %s→%s | PnL estimé=%.2f%% | Raison: %s",
            symbol,
            old_side.upper(),
            new_side.upper(),
            pnl * 100,
            reason[:80],
        )

        # FIX #3: Close-first ordering — annuler les TP/SL UNIQUEMENT si la close a réussi.
        # Si on cancel d'abord et que la close échoue, on se retrouve avec une position nue.
        close_ok = self._close_position_market(symbol, existing)
        if close_ok:
            try:
                self.trader.cancel_all_tpsl(symbol)
            except Exception as e:
                logger.warning("[FLIP] cancel_all_tpsl %s après close OK: %r", symbol, e)
            self._trail_guards.pop(symbol, None)
        else:
            logger.warning(
                "[FLIP] %s close échouée, on n'annule PAS les TP/SL pour ne pas laisser la position nue. Flip abandonné.",
                symbol
            )
            return False

        try:
            self.trader.wait_flat_and_clean(symbol, timeout=12.0)
        except Exception as e:
            logger.warning("[FLIP] wait_flat_and_clean %s: %r", symbol, e)

        if not self.simulation:
            try:
                refreshed = self._get_open_positions()
                if symbol in refreshed:
                    logger.warning("[FLIP] %s encore ouvert après cleanup, flip annulé pour éviter doublon.", symbol)
                    return False
            except Exception as e:
                logger.warning("[FLIP] recheck positions %s: %r", symbol, e)

        if self.simulation:
            logger.info(
                "[SIM][FLIP] ENTER %s %s | entry=%.4f sl=%.4f tp=%.4f | %s",
                new_side.upper(), symbol, entry, sl, tp, reason[:120]
            )
            self.memory.update_position(symbol, {
                "side": new_side,
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "qty": 0,
                "sim": True,
                "reason": reason[:200],
            })
            success = True
        else:
            live = self._send_live_order(
                symbol,
                new_side,
                entry,
                sl,
                tp,
                leverage=MAX_LEVERAGE,
                scalper_prices=scalper_prices,
                confidence=confidence,
            )
            if live is None:
                logger.warning("[FLIP] Ouverture nouvelle position %s échouée.", symbol)
                return False

            fill_price = float(live["fill_price"])
            fill_qty = float(live["fill_qty"])
            adj_tp = float(live["tp"])
            adj_sl = float(live["sl"])

            self.memory.update_position(symbol, {
                "side": new_side,
                "entry": fill_price,
                "sl": adj_sl,
                "tp": adj_tp,
                "qty": fill_qty,
                "sim": False,
                "reason": reason[:200],
            })
            logger.info(
                "[LIVE][FLIP] ENTER %s %s | entry=%.4f sl=%.4f tp=%.4f | %s",
                new_side.upper(), symbol, fill_price, adj_sl, adj_tp, reason[:120]
            )
            success = True

        try:
            self.trader.log_flip_event(
                symbol,
                old_side,
                new_side,
                pnl,
                context={"regime": str(self.memory.get_regime()), "entry": entry},
            )
        except Exception as e:
            logger.warning("[FLIP] log_flip_event %s: %r", symbol, e)

        self.last_entry_ts[symbol] = time.time()
        return success

    def _current_cycle_symbols(self) -> List[str]:
        if not self.symbols:
            return []
        n = max(1, min(SYMBOLS_PER_CYCLE, len(self.symbols)))
        start = self._symbol_idx % len(self.symbols)
        selected = [self.symbols[(start + i) % len(self.symbols)] for i in range(n)]
        self._symbol_idx = (start + n) % len(self.symbols)
        return selected

    def _store_regime(self, regime: Dict) -> None:
        trend = str(regime.get("trend", "range"))
        volatility = str(regime.get("volatility", "medium"))
        risk = str(regime.get("risk", "medium"))
        try:
            self.memory.update_regime(trend, volatility)
        except Exception as e:
            logger.warning("update_regime incompatible: %r", e)

        for method_name in ("set", "set_value", "update_meta"):
            try:
                method = getattr(self.memory, method_name, None)
                if callable(method):
                    method("market_risk", risk)
                    break
            except Exception:
                pass

    def run_forever(self) -> None:
        logger.info("SalleDesMarches V6.1.0 — SIMULATION=%s", self.simulation)
        logger.info("LocalAI: %s | models=%s", LOCALAI_BASE_URL, list(MODELS.keys())[:10])
        logger.info("Symboles: %s", self.symbols)
        logger.info(
            "Boucle démarrée (cycle=%ss cooldown=%ss max_pos=%d min_conf=%.2f risk/trade=%.2f%% lev_max=%.1fx notional_max=%.0f%% min_volratio=%.3f syms/cycle=%d)",
            SCAN_INTERVAL_SEC,
            COOLDOWN_SEC,
            MAX_OPEN_POSITIONS,
            MIN_CONFIDENCE,
            RISK_PER_TRADE_PCT * 100,
            MAX_LEVERAGE,
            MAX_NOTIONAL_PCT * 100,
            MIN_VOLRATIO,
            SYMBOLS_PER_CYCLE,
        )
        logger.info(
            "Trailing profit-only (ROE dynamique | check=%ds): TP_arm_fallback=%.2f%% Trail_recul=%.2f%%",
            TRAIL_CHECK_SEC,
            TP_ARM_PCT * 100,
            TRAIL_DROP_PCT * 100,
        )
        logger.info("Logs fichier: logs/sdm.log (rotation 10Mo × 5)")
        logger.info("Pipeline V6 active: FeatureEngine + RegimeEngine + HyperliquidSyncCache")

        while True:
            t0 = time.time()

            if (t0 - self.last_news_ts) > NEWS_REFRESH_SEC:
                try:
                    self.news.analyze(self.symbols)
                except Exception as e:
                    logger.warning("news analyze: %r", e)
                self.last_news_ts = t0

            if (t0 - self.last_whales_ts) > WHALES_REFRESH_SEC:
                try:
                    self.whales.analyze(self.symbols)
                except Exception as e:
                    logger.warning("whales analyze: %r", e)
                self.last_whales_ts = t0

            market_data = {"symbols": self.symbols, "ts": int(t0)}
            try:
                regime = self.orchestrator.assess_regime(market_data)
            except Exception as e:
                logger.warning("assess_regime: %r", e)
                regime = self.memory.get_regime() or {
                    "trend": "range",
                    "volatility": "medium",
                    "risk": "medium",
                }

            self._store_regime(regime)

            # Note: _monitor_trailing tourne désormais dans son propre thread (_trail_loop)
            # pour ne pas être bloqué par les longs cycles d'analyse LLM.

            open_positions = self._get_open_positions()
            self._sync_manual_closures(open_positions)
            open_positions = self._get_open_positions()
            stats = {"analyzed": 0, "skipped": 0, "entered": 0, "flipped": 0}

            cycle_symbols = self._current_cycle_symbols()
            logger.info("Cycle symboles: %s", cycle_symbols)

            for symbol in cycle_symbols:
                try:
                    tech = self.technical.analyze(symbol) or {}
                    if not tech:
                        continue

                    stats["analyzed"] += 1
                    symbol_regime = regime
                    
                    try:
                        snapshot = self._get_orderbook_snapshot(symbol)
                        if snapshot:
                            self.memory.update_orderbook_snapshot(symbol, snapshot)

                        previous_features = self.memory.get_advanced_features(symbol) or {}
                        features = self.featureengine.compute(
                            symbol=symbol,
                            technical=tech,
                            orderbook=snapshot,
                            regime=regime,
                            previous=previous_features,
                        )
                        self.memory.update_advanced_features(symbol, features)

                        previous_symbol_regime = self.memory.get_regime_for_symbol(symbol) or {}
                        symbol_regime = self.regimeengine.assess(
                            symbol,
                            features,
                            previous_regime=previous_symbol_regime,
                        )
                        self.memory.update_regime_for_symbol(symbol, symbol_regime)

                        logger.info(
                            "FEATURE PIPELINE %s slope=%.3f vol=%s micro=%s ob=%.3f",
                            symbol,
                            float(features.get("slope_alignment_score", 0.0) or 0.0),
                            str(features.get("volatility_state", "unknown")),
                            str(features.get("microtrend", "unknown")),
                            float(features.get("orderbook_imbalance", 0.0) or 0.0),
                        )
                        logger.info(
                            "REGIME PIPELINE %s trend=%s vol=%s risk=%s",
                            symbol,
                            str(symbol_regime.get("trend", "unknown")),
                            str(symbol_regime.get("volatility", "medium")),
                            str(symbol_regime.get("risk", "medium")),
                        )
                    except Exception as e:
                        logger.warning(
                            "PIPELINE V5.9 %s fallback regime global error: %r",
                            symbol,
                            e,
                        )
                        symbol_regime = regime

                    # ── Grid bot : activation/désactivation selon régime ──────
                    if GRID_ENABLED:
                        regime_trend = str(symbol_regime.get("trend", "unknown"))
                        existing_pos = open_positions.get(symbol)
                        forced = symbol in GRID_FORCE_SYMBOLS

                        if forced and not self.grid_manager.is_active(symbol):
                            atr_val = float(tech.get("atr", 0) or 0)
                            mid = self._get_current_price(symbol, 0.0)
                            if mid <= 0:
                                logger.warning("GRID %s force: prix=0, skip", symbol)
                            elif atr_val <= 0:
                                atr_val = mid * 0.005  # fallback 0.5% si ATR absent
                                logger.info("GRID %s force: ATR absent, fallback=%.4f", symbol, atr_val)
                                self.grid_manager.activate(symbol, mid, atr_val)
                            else:
                                logger.info("GRID %s activation forcée mid=%.4f atr=%.4f", symbol, mid, atr_val)
                                self.grid_manager.activate(symbol, mid, atr_val)
                        elif regime_trend == "range" and not existing_pos:
                            n_grids = len(self.grid_manager.active_symbols())
                            if not self.grid_manager.is_active(symbol) and n_grids < GRID_MAX_SYMBOLS:
                                atr_val = float(tech.get("atr", 0) or 0)
                                mid = self._get_current_price(symbol, 0.0)
                                logger.debug("GRID %s candidat mid=%.4f atr=%.4f", symbol, mid, atr_val)
                                if atr_val > 0 and mid > 0:
                                    self.grid_manager.activate(symbol, mid, atr_val)
                        elif regime_trend in ("bull", "bear", "trend") and not forced:
                            if self.grid_manager.is_active(symbol):
                                logger.info("GRID %s désactivé (régime→%s)", symbol, regime_trend)
                                self.grid_manager.deactivate(symbol, cancel=True)

                    # Symbole en mode grille : skip pipeline scalp (peu importe si position ouverte).
                    # Évite que le scalp flip/ferme une position grid, ce qui rendrait le TP
                    # reduce_only invalide et déclencherait une désactivation prématurée.
                    if GRID_ENABLED and self.grid_manager.is_active(symbol):
                        continue

                    # Master switch SCALP_ENABLED : si False, on ne lance ni bull/bear ni
                    # consensus ni scalper sur ce symbole. Les positions existantes restent
                    # monitorées par le trail loop + emergency exit. Utile pour isoler la
                    # rentabilité du grid (cf. analyse profit 2026-05-06).
                    if not SCALP_ENABLED:
                        stats["skipped"] += 1
                        continue

                    bull = self.bull.analyze(symbol, symbol_regime, tech)
                    bear = self.bear.analyze(symbol, symbol_regime, tech)
                    cons = _consensus(bull, bear, tech)

                    side = cons.get("side", "wait")
                    conf = float(cons.get("confidence", 0) or 0)
                    reason = cons.get("reason", "")

                    logger.info("CONSENSUS %s | side=%s conf=%.2f | %s", symbol, side, conf, reason)

                    # Distinction LLM_DEGRADED vs vrai signal neutre
                    if side == "llm_down":
                        stats["skipped"] += 1
                        logger.warning("LLM_DEGRADED %s — skip cycle | %s", symbol, reason)
                        continue
                    if side == "wait" or conf < MIN_CONFIDENCE:
                        stats["skipped"] += 1
                        logger.info("SKIP %s — conf trop faible (%.2f < %.2f) ou côté=wait", symbol, conf, MIN_CONFIDENCE)
                        continue

                    # Bloquer les signaux contradictoires bull/side
                    bconf = float(bull.get("confidence", 0) or 0)
                    if side == "sell" and bconf > 0.80:
                        stats["skipped"] += 1
                        logger.info("SKIP %s — signal contradictoire (SELL mais bull=%.2f)", symbol, bconf)
                        continue
                    if side == "buy" and bconf < 0.30:
                        stats["skipped"] += 1
                        logger.info("SKIP %s — signal contradictoire (BUY mais bull=%.2f)", symbol, bconf)
                        continue

                    # Filtre horaire : pas d'entrée fraîche ni de flip durant
                    # les fenêtres UTC à EV historique négative (#2 diagnostic).
                    # Le management des positions ouvertes (trail) reste actif.
                    if BLOCKED_HOURS_UTC:
                        hour_utc = datetime.now(timezone.utc).hour
                        if hour_utc in BLOCKED_HOURS_UTC:
                            stats["skipped"] += 1
                            logger.info("SKIP %s — heure bloquée (%02dh UTC)", symbol, hour_utc)
                            continue

                    existing = open_positions.get(symbol)
                    skip_reason = self._skip_reason(symbol, tech, open_positions, new_side=side)
                    if skip_reason:
                        stats["skipped"] += 1
                        if DEBUG_SKIPS:
                            logger.info("SKIP %s — %s", symbol, skip_reason)
                        continue

                    prices = self.scalper.decide(symbol, side, tech, symbol_regime, cons)
                    entry = self._round_px(symbol, float(prices.get("entry", 0)))
                    sl = self._round_px(symbol, float(prices.get("sl", 0)))
                    tp = self._round_px(symbol, float(prices.get("tp", 0)))

                    if existing and existing.get("side") != side:
                        # Filtre conf pour les flips (#1 diagnostic) : les flips à
                        # conf < 0.80 ont une EV de -0.34% sur l'échantillon, vs
                        # +0.19% pour les flips à conf >= 0.80. Refus pur et simple.
                        # OVERRIDE D'URGENCE : si position en perte importante ET signal
                        # de flip aligné avec le régime global, on autorise au seuil
                        # MIN_CONFIDENCE. Évite le blocage en retournement de tendance
                        # (cf. bug BTC -5.88% du 2026-05-04 où le bot voulait flipper
                        # depuis 07:25 mais était verrouillé à 0.95).
                        flip_threshold = FLIP_MIN_CONFIDENCE
                        try:
                            ex_entry = float(existing.get("entry", 0) or 0)
                            ex_lev = float(existing.get("leverage", MAX_LEVERAGE) or MAX_LEVERAGE)
                            cur_px = self._get_current_price(symbol, ex_entry)
                            if ex_entry > 0 and cur_px > 0:
                                ex_side = str(existing.get("side", "")).lower()
                                ex_brut = (cur_px - ex_entry) / ex_entry if ex_side == "buy" else (ex_entry - cur_px) / ex_entry
                                ex_roe = ex_brut * ex_lev
                                regime_trend = str(regime.get("trend", "range")).lower()
                                aligned = (
                                    (side == "buy" and regime_trend in ("bull", "trend"))
                                    or (side == "sell" and regime_trend in ("bear",))
                                )
                                if ex_roe <= -FLIP_EMERGENCY_LOSS_PCT and aligned:
                                    flip_threshold = MIN_CONFIDENCE
                                    logger.warning(
                                        "FLIP EMERGENCY OVERRIDE %s | ROE=%.2f%% trend=%s flip→%s | seuil %.2f→%.2f",
                                        symbol, ex_roe * 100, regime_trend, side, FLIP_MIN_CONFIDENCE, flip_threshold,
                                    )
                        except Exception as _e:
                            logger.debug("flip override compute %s: %r", symbol, _e)

                        if conf < flip_threshold:
                            stats["skipped"] += 1
                            logger.info("SKIP %s — flip refusé (conf %.2f < %.2f)", symbol, conf, flip_threshold)
                            continue
                        flip_ts = self._flip_cooldowns.get(symbol, 0.0)
                        flip_wait = FLIP_COOLDOWN_SEC - (time.time() - flip_ts)
                        if flip_wait > 0:
                            stats["skipped"] += 1
                            logger.info("SKIP %s — cooldown anti-flip (%ds)", symbol, int(flip_wait))
                            continue
                        ok = self._handle_flip(symbol, existing, side, entry, sl, tp, reason, scalper_prices=prices, confidence=conf)
                        if ok:
                            self._flip_cooldowns[symbol] = time.time()
                            stats["flipped"] += 1
                        continue

                    if self.simulation:
                        logger.info(
                            "[SIM] ENTER %s %s | entry=%.4f sl=%.4f tp=%.4f | %s",
                            side.upper(), symbol, entry, sl, tp, reason[:120]
                        )
                        self.memory.update_position(symbol, {
                            "side": side,
                            "entry": entry,
                            "sl": sl,
                            "tp": tp,
                            "qty": 0,
                            "sim": True,
                            "reason": reason[:200],
                        })
                        self._entry_regime_ctx[symbol] = {
                            "ts": time.time(),
                            "trend": str(symbol_regime.get("trend", "range")),
                            "volatility": str(symbol_regime.get("volatility", "medium")),
                            "risk": str(symbol_regime.get("risk", "medium")),
                        }
                        stats["entered"] += 1
                        self.last_entry_ts[symbol] = time.time()
                        continue

                    live = self._send_live_order(
                        symbol,
                        side,
                        entry,
                        sl,
                        tp,
                        leverage=MAX_LEVERAGE,
                        scalper_prices=prices,
                        confidence=conf,
                    )
                    if live is None:
                        continue

                    fill_price = float(live["fill_price"])
                    fill_qty = float(live["fill_qty"])
                    adj_tp = float(live["tp"])
                    adj_sl = float(live["sl"])

                    self.memory.update_position(symbol, {
                        "side": side,
                        "entry": fill_price,
                        "sl": adj_sl,
                        "tp": adj_tp,
                        "qty": fill_qty,
                        "sim": False,
                        "reason": reason[:200],
                    })
                    self._entry_regime_ctx[symbol] = {
                        "ts": time.time(),
                        "trend": str(symbol_regime.get("trend", "range")),
                        "volatility": str(symbol_regime.get("volatility", "medium")),
                        "risk": str(symbol_regime.get("risk", "medium")),
                    }
                    logger.info(
                        "[LIVE] ENTER %s %s | entry=%.4f sl=%.4f tp=%.4f | %s",
                        side.upper(), symbol, fill_price, adj_sl, adj_tp, reason[:120]
                    )

                    stats["entered"] += 1
                    self.last_entry_ts[symbol] = time.time()

                except Exception as e:
                    logger.warning("Cycle %s error: %r", symbol, e)

            if (t0 - self.last_learn_ts) > BULL_BEAR_REFRESH_SEC:
                try:
                    self.learner.update_profiles()
                except Exception as e:
                    logger.warning("Learner.update_profiles: %r", e)
                self.last_learn_ts = t0

            logger.info(
                "Stats cycle: analyzed=%d skipped=%d entered=%d flipped=%d open=%d trail_guards=%d",
                stats["analyzed"],
                stats["skipped"],
                stats["entered"],
                stats["flipped"],
                len(self._get_open_positions()),
                len(self._trail_guards),
            )

            elapsed = time.time() - t0
            delay = max(1.0, SCAN_INTERVAL_SEC - elapsed)
            time.sleep(delay)


PID_FILE = "logs/sdm.pid"


def _acquire_singleton_lock() -> int:
    """Empêche deux instances du bot de tourner en parallèle (cf. code_proposals.md
    2026-05-06 [WARNING]). Prend un lock fcntl exclusif non-bloquant sur logs/sdm.pid.
    Si un autre process tient déjà le lock, on log et on quitte.
    Le lock est libéré automatiquement à la mort du process (kernel)."""
    os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
    fd = os.open(PID_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            with open(PID_FILE) as f:
                existing = f.read().strip()
        except Exception:
            existing = "?"
        logger.error("Une autre instance tourne déjà (PID=%s) — abandon.", existing)
        sys.exit(1)
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())

    def _cleanup_pid():
        try:
            if os.path.exists(PID_FILE):
                os.unlink(PID_FILE)
        except Exception:
            pass
    atexit.register(_cleanup_pid)
    return fd  # gardé en scope dans main() pour ne pas fermer le fd


def main() -> None:
    _lock_fd = _acquire_singleton_lock()  # noqa: F841 — gardé en scope pour le lock
    symbols = getattr(SETTINGS, "SYMBOLS", ["BTC", "ETH", "ATOM", "DYDX", "SOL"])
    bot = SalleDesMarchesV6(symbols=symbols, simulation=SIMULATION_MODE)

    def _shutdown(signum, frame):
        logger.info("Signal %d reçu — arrêt propre", signum)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        bot.run_forever()
    except KeyboardInterrupt:
        logger.info("Arrêt — désactivation grilles actives")
        if GRID_ENABLED:
            bot.grid_manager.deactivate_all()
    finally:
        if GRID_ENABLED:
            bot.grid_manager.deactivate_all()


if __name__ == "__main__":
    main()

"""
GridManager — grille multi-niveaux pour marchés en range (Hyperliquid perpetuals).

Principe :
  À l'activation, place N niveaux de chaque côté du `center` avec un step
  = ATR × GRID_ATR_FACTOR. Chaque niveau a son propre micro-FSM :

    pending  → ordre limit en attente de fill
    filled   → fill détecté, on doit placer le TP
    tp_placed→ TP limit reduce_only au niveau adjacent en attente
    done     → TP rempli (= profit du step). Le niveau est ré-armé.

  Buy filled @ k → TP sell @ k+spacing
  Sell filled @ k → TP buy @ k-spacing

  Breakout : si |price - center| > (LEVELS+1)*spacing → désactivation totale.
  Trail SL : NON, jamais ; le grid n'a pas de stop loss directionnel.
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from execution.types import OrderRequest
from execution.order_registry import (
    SOURCE_GRID_PENDING,
    SOURCE_GRID_TP,
    get_order_registry,
)

logger = logging.getLogger("sdm.grid")


@dataclass
class GridLevel:
    """Un niveau du ladder."""
    side: str                                # "buy" ou "sell"
    target_px: float                         # prix initial du niveau
    qty: float
    pending_oid: Optional[int] = None        # ordre limit en attente de fill
    fill_px: Optional[float] = None
    tp_oid: Optional[int] = None             # TP reduce_only après fill
    tp_target_px: Optional[float] = None
    state: str = "pending"                   # pending|filled|tp_placed|done|frozen
    # Anti faux-positif fill (fix 24/05) : compteur d'occurrences consécutives
    # où l'oid est absent de open_oids. Évite de conclure "filled" sur un blip
    # de cache HL (cache stale 60s observé pendant le storm BCH du 24/05 16:01).
    miss_pending: int = 0                    # pour pending_oid
    miss_tp: int = 0                         # pour tp_oid
    # Frozen guard (fix 28/05 V6 porté V7) : timestamp où le level a été gelé
    # suite à G2 skip (TP refusé par HL car position du mauvais côté). Tant
    # que frozen, aucun nouveau pending n'est posé au target_px → évite la
    # boucle de re-fill observée sur ADA 28/05 21:57.
    frozen_since: Optional[float] = None


@dataclass
class GridState:
    symbol: str
    center: float
    spacing: float
    qty_per_level: float
    levels: List[GridLevel]
    breakout_limit: float
    created_at: float = field(default_factory=time.time)
    last_update: float = field(default_factory=time.time)
    total_pnl_pct: float = 0.0
    trade_count: int = 0
    # Drift guard (fix 25/05) : timestamp où le prix est sorti du seuil
    # GRID_DRIFT_K·spacing autour du center. None = pas en dérive.
    drift_since: Optional[float] = None
    # Ladder health check : timestamp du dernier check d'intégrité.
    last_health_check: float = 0.0


class GridEngine:
    """FSM grid portée depuis V6 (agents/grid_manager.GridManager).

    Adaptation V7 : la config est injectée au constructeur (au lieu de
    `from config.settings import GRID_*` éparpillés). Le reste de la FSM
    (drift guard, health check, anti-superposition, faux-positif cache stale,
    G2 recycle, tick decimals par symbole) est identique à V6.

    Paramètre `grid_config` : instance de core.config.GridStrategyConfig
    ou tout objet exposant les attributs :
      - atr_factor, levels, notional_per_level_usdc
      - drift_k, drift_window_sec, health_check_sec
      - activation_threshold_usdc
    Plus des constantes globales lues via getattr (cooldown, grace, leverage,
    miss_threshold) avec valeurs par défaut.
    """

    def __init__(self, exchange, grid_config) -> None:
        self._exchange = exchange
        self._cfg = grid_config
        self._grids: Dict[str, GridState] = {}
        self._deactivation_ts: Dict[str, float] = {}
        # Cache des tick decimals par symbole (lookup via HL meta universe).
        # HL exige max (6 - szDecimals) décimales pour le prix. BNB=3, BTC=1, etc.
        self._tick_decimals: Dict[str, int] = {}

    # ─── API publique ─────────────────────────────────────────────────────────

    def is_active(self, symbol: str) -> bool:
        return symbol in self._grids

    def active_symbols(self) -> list:
        return list(self._grids.keys())

    def can_activate(self, symbol: str) -> bool:
        cooldown = int(getattr(self._cfg, "cooldown_sec", 300))
        last = self._deactivation_ts.get(symbol, 0.0)
        remaining = cooldown - (time.time() - last)
        if remaining > 0:
            logger.debug("GRID %s cooldown actif (%.0fs restantes)", symbol, remaining)
            return False
        return True

    def activate(self, symbol: str, center: float, atr: float) -> bool:
        """Place N niveaux de chaque côté du center. Retourne True si succès."""
        if self.is_active(symbol):
            return False
        if not self.can_activate(symbol):
            return False

        # Anti-superposition (2026-05-22) : annule tout reliquat d'une activation
        # précédente encore présent en registre (cas observé sur ETH : ancienne
        # grille spacing=7.9 cumulée avec la nouvelle spacing=4.7).
        try:
            self.cleanup_dangling_orders(symbol=symbol)
        except Exception as e:
            logger.warning("GRID %s cleanup_dangling pré-activation: %r", symbol, e)

        GRID_ATR_FACTOR = float(self._cfg.atr_factor)
        GRID_LEVELS = int(self._cfg.levels)
        GRID_NOTIONAL = float(self._cfg.notional_per_level_usdc)
        GRID_LEVERAGE = int(getattr(self._cfg, "leverage", 3))

        spacing = atr * GRID_ATR_FACTOR
        if spacing <= 0 or center <= 0:
            logger.warning("GRID %s: paramètres invalides (center=%.4f atr=%.4f)", symbol, center, atr)
            return False

        qty = round(GRID_NOTIONAL / center, 6)
        if qty * center < 10.5:
            logger.warning("GRID %s: notional trop faible (%.2f < $10.5)", symbol, qty * center)
            return False

        n_levels = int(GRID_LEVELS)
        lev = int(GRID_LEVERAGE)
        breakout_limit = spacing * (n_levels + 1)

        levels: List[GridLevel] = []
        placed: List[int] = []

        # Buy levels sous le center (level 1 = le plus proche, level N = le plus bas)
        for k in range(1, n_levels + 1):
            price = self._round_px(center - k * spacing, symbol)
            if price <= 0:
                continue
            oid = self._place_limit(symbol, "buy", qty, price, lev, reduce_only=False)
            if oid is None:
                # Rollback : cancel tout ce qui est posé puis abort.
                logger.warning("GRID %s: échec place buy@%.4f niveau %d — rollback", symbol, price, k)
                for p in placed:
                    self._cancel_oid(symbol, p)
                return False
            placed.append(oid)
            levels.append(GridLevel(side="buy", target_px=price, qty=qty, pending_oid=oid))

        # Sell levels au-dessus du center
        for k in range(1, n_levels + 1):
            price = self._round_px(center + k * spacing, symbol)
            if price <= 0:
                continue
            oid = self._place_limit(symbol, "sell", qty, price, lev, reduce_only=False)
            if oid is None:
                logger.warning("GRID %s: échec place sell@%.4f niveau %d — rollback", symbol, price, k)
                for p in placed:
                    self._cancel_oid(symbol, p)
                return False
            placed.append(oid)
            levels.append(GridLevel(side="sell", target_px=price, qty=qty, pending_oid=oid))

        self._grids[symbol] = GridState(
            symbol=symbol,
            center=center,
            spacing=spacing,
            qty_per_level=qty,
            levels=levels,
            breakout_limit=breakout_limit,
        )
        logger.info(
            "GRID %s ACTIVÉ center=%.4f spacing=%.4f qty=%.6f levels=%d (range %.4f → %.4f)",
            symbol, center, spacing, qty, n_levels,
            center - n_levels * spacing, center + n_levels * spacing,
        )
        return True

    def on_tick(
        self, symbol: str, open_oids: Set[int],
        current_price: float, position_szi: float = 0.0,
        cache_fresh: bool = True,
    ) -> None:
        """Mise à jour ladder. Appelé toutes les TRAIL_CHECK_SEC secondes depuis main_v6._grid_loop.

        position_szi : szi signé de la position globale du symbole (positif=long,
        négatif=short, 0=flat). Permet (G2) d'éviter de placer un TP qui serait
        refusé par HL avec "Reduce only would increase position".

        cache_fresh : False si le cache HL est périmé (>10s). Bloque les
        transitions "filled" basées sur oid manquant de open_oids — évite
        les faux positifs qui empilent des TPs duplicat (cas BCH 24/05).
        """
        g = self._grids.get(symbol)
        if g is None:
            return

        GRID_LEVERAGE = int(getattr(self._cfg, "leverage", 3))
        GRID_GRACE_SEC = float(getattr(self._cfg, "grace_sec", 8.0))
        if time.time() - g.created_at < GRID_GRACE_SEC:
            return

        g.last_update = time.time()
        lev = int(GRID_LEVERAGE)

        # Breakout guard : mouvement violent qui sort de la grille
        if abs(current_price - g.center) > g.breakout_limit:
            logger.info(
                "GRID %s BREAKOUT (price=%.4f center=%.4f ±%.4f) → désactivation",
                symbol, current_price, g.center, g.breakout_limit,
            )
            self.deactivate(symbol, cancel=True)
            return

        # Drift guard (fix 25/05) : dérive lente qui ne déclenche pas le breakout
        # mais empile une position résiduelle perdante (cas BCH 23-24/05).
        GRID_DRIFT_K = float(self._cfg.drift_k)
        GRID_DRIFT_WINDOW_SEC = int(self._cfg.drift_window_sec)
        drift_threshold = float(GRID_DRIFT_K) * g.spacing
        if abs(current_price - g.center) > drift_threshold:
            if g.drift_since is None:
                g.drift_since = time.time()
                logger.info(
                    "GRID %s DRIFT détecté (price=%.4f center=%.4f écart=%.4f > %.4f) → timer démarré (%ds avant désactivation)",
                    symbol, current_price, g.center,
                    abs(current_price - g.center), drift_threshold,
                    int(GRID_DRIFT_WINDOW_SEC),
                )
            elif time.time() - g.drift_since > GRID_DRIFT_WINDOW_SEC:
                elapsed = int(time.time() - g.drift_since)
                logger.warning(
                    "GRID %s DRIFT confirmé (>%ds soutenus, price=%.4f center=%.4f) → désactivation",
                    symbol, elapsed, current_price, g.center,
                )
                self.deactivate(symbol, cancel=True)
                return
        else:
            if g.drift_since is not None:
                logger.info(
                    "GRID %s drift terminé (price revenu dans la zone center±%.4f)",
                    symbol, drift_threshold,
                )
                g.drift_since = None

        # FSM par niveau
        for lvl in g.levels:
            self._tick_level(g, lvl, open_oids, current_price, lev, position_szi, cache_fresh)

        # Ladder health check périodique (toutes les ~5 min).
        # Indépendant de la FSM : si un level n'a pas son ordre sur HL, on
        # le repose. Pas de question, pas d'analyse de cause.
        GRID_HEALTH_CHECK_SEC = int(self._cfg.health_check_sec)
        if cache_fresh and time.time() - g.last_health_check > GRID_HEALTH_CHECK_SEC:
            self._ladder_health_check(g, open_oids, lev)
            g.last_health_check = time.time()

    def deactivate(self, symbol: str, cancel: bool = True, close_position: bool = False) -> None:
        """
        close_position=True : ferme aussi la position résiduelle via market reduce_only.
        À utiliser en sortie d'erreur. Sinon on laisse uniquement les TPs cancellés.
        """
        g = self._grids.pop(symbol, None)
        if g is None:
            return
        if cancel:
            for lvl in g.levels:
                if lvl.pending_oid is not None:
                    self._cancel_oid(symbol, lvl.pending_oid)
                if lvl.tp_oid is not None:
                    self._cancel_oid(symbol, lvl.tp_oid)
        self._deactivation_ts[symbol] = time.time()
        logger.info(
            "GRID %s désactivé trades=%d pnl_cumul=%.3f%%",
            symbol, g.trade_count, g.total_pnl_pct * 100,
        )
        if close_position:
            self._close_position_if_open(symbol)

    def deactivate_all(self) -> None:
        for sym in list(self._grids.keys()):
            self.deactivate(sym, cancel=True)

    def _find_orphan_grid_pending(
        self, symbol: str, side: str, target_px: float,
    ) -> Optional[int]:
        """Cherche dans le registry un grid_pending matchant (symbol, side, ~target_px)
        qui n'est pas déjà tracké par un level actif. Permet à _ladder_health_check
        d'adopter un OID existant côté HL au lieu d'en placer un doublon.

        Port V6 fix 5316822 (29/05) : doublons observés sur SOL/BNB/XRP quand
        un place_limit antérieur a réussi côté HL mais que l'OID n'a pas pu
        être propagé au lvl.pending_oid (cache stale, FSM glitch).

        Tolérance prix : 0.01% (rounding _round_px entre placements/lectures).
        """
        try:
            reg = get_order_registry()
            # Re-aligne registry depuis disque (no-op chez le writer).
            try:
                reg.reload_if_stale()
            except Exception:
                pass
            tracked: Set[int] = set()
            for g in self._grids.values():
                for lvl in g.levels:
                    if lvl.pending_oid is not None:
                        tracked.add(int(lvl.pending_oid))
                    if lvl.tp_oid is not None:
                        tracked.add(int(lvl.tp_oid))
            tol = max(abs(target_px) * 1e-4, 1e-9)
            sym_up = str(symbol).upper()
            side_lc = str(side).lower()
            for r in reg.all():
                if r.source != SOURCE_GRID_PENDING:
                    continue
                if str(r.symbol).upper() != sym_up:
                    continue
                if str(r.side).lower() != side_lc:
                    continue
                try:
                    if abs(float(r.price) - float(target_px)) > tol:
                        continue
                except Exception:
                    continue
                oid = int(r.oid)
                if oid in tracked:
                    continue
                return oid
        except Exception as e:
            logger.warning("GRID %s _find_orphan_grid_pending: %r", symbol, e)
        return None

    def _ladder_health_check(
        self, g: "GridState", open_oids: Set[int], lev: int,
    ) -> None:
        """Vérifie l'intégrité du ladder côté HL et repose les ordres manquants.

        Logique brutale : pour chaque level dont l'OID attendu n'est pas dans
        open_oids (ou inexistant), on repose. Pas d'analyse de cause :
          - state="done"               → ré-arme en pending (target_px)
          - state="pending" sans OID   → ré-arme en pending
          - state="pending" OID absent → ré-arme en pending
          - state="tp_placed" OID absent → laisse la FSM gérer (transition
                                          normale tp_placed→recycle si confirmé)

        Le check tp_placed est délégué à la FSM normale (qui a déjà miss_tp +
        cache_fresh) pour éviter de doubler les TPs si vraiment filled.
        """
        repairs = 0
        for lvl in g.levels:
            need_rearm = False
            reason = ""
            if lvl.state == "done":
                need_rearm = True
                reason = "state=done"
            elif lvl.state == "pending":
                if lvl.pending_oid is None or int(lvl.pending_oid) not in open_oids:
                    need_rearm = True
                    reason = "pending OID absent"
            # state == "frozen" : volontairement non géré ici (fix 28/05).
            # Le niveau est gelé car le TP n'est pas plaçable côté HL ; le
            # ré-armer ici relancerait la boucle de re-fill. _tick_level gère
            # le dégel quand szi devient cohérent.

            if not need_rearm:
                continue

            # Fix 29/05 (port V6) : avant de re-poser, scanner le registry
            # pour un grid_pending orphelin matching → adopter au lieu de
            # placer un doublon (cas SOL sell@83.148 posté 2 fois en V6).
            adopted_oid = self._find_orphan_grid_pending(
                g.symbol, lvl.side, lvl.target_px,
            )
            if adopted_oid is not None:
                logger.info(
                    "GRID %s health_check: niveau %s@%.4f déjà présent côté HL oid=%d → adopt (raison init: %s)",
                    g.symbol, lvl.side, lvl.target_px, adopted_oid, reason,
                )
                new_oid = adopted_oid
            else:
                new_oid = self._place_limit(
                    g.symbol, lvl.side, lvl.qty, lvl.target_px, lev,
                    reduce_only=False,
                )
                if new_oid is None:
                    logger.warning(
                        "GRID %s health_check: re-pose %s@%.4f échouée (%s)",
                        g.symbol, lvl.side, lvl.target_px, reason,
                    )
                    continue
                logger.warning(
                    "GRID %s health_check: re-pose %s@%.4f (raison: %s, oid=%d)",
                    g.symbol, lvl.side, lvl.target_px, reason, new_oid,
                )
            lvl.pending_oid = new_oid
            lvl.fill_px = None
            lvl.tp_oid = None
            lvl.tp_target_px = None
            lvl.miss_pending = 0
            lvl.miss_tp = 0
            lvl.frozen_since = None
            lvl.state = "pending"
            repairs += 1

        if repairs:
            logger.info(
                "GRID %s health_check: %d niveau(x) re-posé(s) sur %d total",
                g.symbol, repairs, len(g.levels),
            )

    def cleanup_unknown_grid_orphans(self) -> int:
        """Annule + dé-enregistre les records source=unknown qui ressemblent
        à des reliquats de grille (limit non-RO, non-trigger).

        Heuristique : un ordre absorbé en "unknown" au boot avec :
          - intent == "limit"
          - is_trigger == False
          - reduce_only == False
        est presque sûrement un ancien niveau de grid (le scalp utilise du
        market, les SL/TP recovery sont triggers, les TPs grid sont RO).
        Cancel pour ne pas qu'ils traînent à travers les sessions.

        Appelé uniquement au boot reconcile (pas en runtime, car un ordre
        unknown pourrait être un add absorption transitoire en cours de
        session — au boot c'est statique).
        """
        from memory.order_registry import SOURCE_UNKNOWN, get_order_registry
        reg = get_order_registry()
        targets = [
            r for r in reg.all()
            if r.source == SOURCE_UNKNOWN
            and not r.is_trigger
            and not r.reduce_only
            and str(r.intent).lower() == "limit"
        ]
        cancelled = 0
        for r in targets:
            try:
                self._exchange.cancel_order(str(r.oid))
                cancelled += 1
            except Exception as e:
                logger.warning(
                    "GRID cleanup_unknown cancel oid=%d %s (%s@%.4f): %r",
                    r.oid, r.symbol, r.side, r.price, e,
                )
            finally:
                try:
                    reg.unregister(r.oid)
                except Exception:
                    pass
        if targets:
            logger.info(
                "GRID cleanup_unknown_grid_orphans: %d candidats, %d annulés",
                len(targets), cancelled,
            )
        return cancelled

    def cleanup_dangling_orders(self, symbol: Optional[str] = None) -> int:
        """Annule + dé-enregistre les ordres tagués grid_pending / grid_tp dont
        l'état n'est plus géré par un GridState en mémoire.

        Cas typiques :
          - Restart bot : `_grids` est vide mais le registre persiste sur disque,
            donc les anciens ordres restent vivants sur HL sans superviseur.
          - Réactivation grid : la deactivate précédente a échoué partiellement
            (cancel KO sur 1 OID), un reliquat traîne au tour d'après.

        Appelé depuis activate() (symbol ciblé) et depuis le boot reconciler
        (symbol=None → tous les symbols).

        Returns: nombre d'ordres réellement annulés.
        """
        reg = get_order_registry()
        targets = [
            r for r in reg.all()
            if r.source in (SOURCE_GRID_PENDING, SOURCE_GRID_TP)
            and (symbol is None or r.symbol == str(symbol).upper())
        ]
        # Ne pas toucher aux OIDs encore référencés par un GridState actif
        # (sécurité au cas où cette méthode serait appelée pendant un cycle).
        tracked: Set[int] = set()
        for g in self._grids.values():
            for lvl in g.levels:
                if lvl.pending_oid is not None:
                    tracked.add(int(lvl.pending_oid))
                if lvl.tp_oid is not None:
                    tracked.add(int(lvl.tp_oid))
        cancelled = 0
        for r in targets:
            if r.oid in tracked:
                continue
            try:
                self._exchange.cancel_order(str(r.oid))
                cancelled += 1
            except Exception as e:
                logger.warning(
                    "GRID cleanup_dangling cancel oid=%d %s (%s@%.4f): %r",
                    r.oid, r.symbol, r.side, r.price, e,
                )
            finally:
                try:
                    reg.unregister(r.oid)
                except Exception:
                    pass
        if targets:
            logger.info(
                "GRID cleanup_dangling %s: %d candidats, %d annulés (tracked=%d)",
                symbol or "ALL", len(targets), cancelled, len(tracked),
            )
        return cancelled

    # ─── FSM par niveau ───────────────────────────────────────────────────────

    # Confirmer "filled" demande N occurrences consécutives d'OID manquant.
    # Avec tick=2s, MISS_THRESHOLD=2 = 4s mini avant transition. Évite les
    # faux positifs sur blip de cache HL (cas BCH 24/05 : cache stale 60s →
    # 8 TPs duplicats empilés au même prix).
    _MISS_THRESHOLD = 2

    def _tick_level(
        self, g: GridState, lvl: GridLevel,
        open_oids: Set[int], current_price: float, lev: int,
        position_szi: float = 0.0,
        cache_fresh: bool = True,
    ) -> None:
        if lvl.state == "pending":
            if lvl.pending_oid is not None and lvl.pending_oid not in open_oids:
                # OID manquant : pourrait être un blip cache stale.
                # On ne conclut "filled" qu'après MISS_THRESHOLD ticks consécutifs
                # ET avec un cache frais.
                if not cache_fresh:
                    return  # cache stale → ignore, on retry au prochain tick
                lvl.miss_pending += 1
                if lvl.miss_pending < self._MISS_THRESHOLD:
                    return
                # Confirmé : fill effectif
                try:
                    get_order_registry().unregister(lvl.pending_oid)
                except Exception:
                    pass
                lvl.fill_px = lvl.target_px
                lvl.pending_oid = None
                lvl.miss_pending = 0
                lvl.state = "filled"
                logger.info(
                    "GRID %s level %s@%.4f filled → place TP",
                    g.symbol, lvl.side, lvl.target_px,
                )
                self._place_tp_for_level(g, lvl, lev, position_szi, cache_fresh)
            else:
                lvl.miss_pending = 0  # OID toujours présent : reset compteur

        elif lvl.state == "filled":
            # Place TP si pas encore fait (retry possible)
            self._place_tp_for_level(g, lvl, lev, position_szi, cache_fresh)

        elif lvl.state == "frozen":
            # G2 skip antérieur : le TP reduce_only serait refusé par HL car
            # position du mauvais côté. On retente uniquement quand szi est
            # cohérent. Sinon timeout → done (health_check ré-armera un
            # pending propre, rythme borné GRID_HEALTH_CHECK_SEC).
            if not cache_fresh:
                return
            frozen_timeout = int(getattr(self._cfg, "frozen_timeout_sec", 600))
            tp_side = "sell" if lvl.side == "buy" else "buy"
            EPS = 1e-9
            szi_ok = (
                (tp_side == "sell" and position_szi > EPS) or
                (tp_side == "buy" and position_szi < -EPS)
            )
            if szi_ok:
                logger.info(
                    "GRID %s level %s@%.4f: dégel (szi=%.6f cohérent) → place TP",
                    g.symbol, lvl.side, lvl.target_px, position_szi,
                )
                lvl.frozen_since = None
                self._place_tp_for_level(g, lvl, lev, position_szi, cache_fresh)
            elif lvl.frozen_since is not None and \
                 time.time() - lvl.frozen_since > frozen_timeout:
                logger.warning(
                    "GRID %s level %s@%.4f: frozen >%ds (szi=%.6f toujours mauvais côté) → done",
                    g.symbol, lvl.side, lvl.target_px,
                    int(frozen_timeout), position_szi,
                )
                lvl.state = "done"
                lvl.frozen_since = None

        elif lvl.state == "tp_placed":
            if lvl.tp_oid is not None and lvl.tp_oid not in open_oids:
                # Même protection anti faux-positif que pour le pending.
                if not cache_fresh:
                    return
                lvl.miss_tp += 1
                if lvl.miss_tp < self._MISS_THRESHOLD:
                    return
                # TP rempli confirmé → profit du step
                try:
                    get_order_registry().unregister(lvl.tp_oid)
                except Exception:
                    pass
                pnl_pct = g.spacing / max(lvl.fill_px or g.center, 1e-9)
                g.total_pnl_pct += pnl_pct
                g.trade_count += 1
                logger.info(
                    "GRID %s level %s@%.4f TP #%d hit pnl=%.3f%% cumul=%.3f%%",
                    g.symbol, lvl.side, lvl.target_px,
                    g.trade_count, pnl_pct * 100, g.total_pnl_pct * 100,
                )
                # Recycle le niveau : nouveau pending au même prix.
                lvl.fill_px = None
                lvl.tp_oid = None
                lvl.tp_target_px = None
                lvl.miss_tp = 0
                new_oid = self._place_limit(
                    g.symbol, lvl.side, lvl.qty, lvl.target_px, lev, reduce_only=False,
                )
                if new_oid is None:
                    logger.warning(
                        "GRID %s: re-armement niveau %s@%.4f échoué",
                        g.symbol, lvl.side, lvl.target_px,
                    )
                    lvl.state = "done"
                    return
                lvl.pending_oid = new_oid
                lvl.state = "pending"
            else:
                lvl.miss_tp = 0  # OID TP toujours présent : reset

    def _place_tp_for_level(
        self, g: GridState, lvl: GridLevel, lev: int, position_szi: float = 0.0,
        cache_fresh: bool = True,
    ) -> None:
        """Place le TP reduce_only correspondant au niveau filled.

        G1 : si le TP ne peut être placé, marque le niveau "done" (pas de retry).
        G2 : si la position globale n'a pas de quoi être réduite par ce TP,
             gèle le niveau (state="frozen"). _tick_level retentera quand szi
             redevient cohérent.

        cache_fresh : si False, on ne décide rien (position_szi est un snapshot
        potentiellement périmé). L'appelant retentera au prochain tick.
        Fix 28/05 : sans ça, szi figé par cache stale fait skipper le TP en
        boucle alors que la position réelle évolue.
        """
        # Fix 28/05 : ne pas décider sur snapshot szi périmé.
        if not cache_fresh:
            return

        # Buy filled @ k → TP sell @ k+spacing  (step au-dessus)
        # Sell filled @ k → TP buy @ k-spacing  (step en-dessous)
        if lvl.side == "buy":
            tp_side = "sell"
            tp_target = self._round_px(lvl.target_px + g.spacing, g.symbol)
        else:
            tp_side = "buy"
            tp_target = self._round_px(lvl.target_px - g.spacing, g.symbol)

        if tp_target <= 0:
            logger.warning("GRID %s: tp_target invalide (%.6f), niveau désarmé",
                           g.symbol, tp_target)
            lvl.state = "done"
            return

        # G2 : check de cohérence position globale ↔ TP reduce_only.
        # - TP sell  ↔ on cherche à fermer un long  → position_szi doit être > 0
        # - TP buy   ↔ on cherche à fermer un short → position_szi doit être < 0
        # Si la position n'est pas du bon côté, HL refusera "Reduce only would
        # increase position".
        #
        # Fix 28/05 (port V6) : avant (fix 26/05) on recyclait le pending au même
        # prix → boucle de re-fill quand prix collait au target (cas ADA 28/05
        # 21:57 : 5 fills empilés en 42s sur buy@0.2320 car szi=-384 figé par
        # cache stale). Maintenant on gèle. Le niveau redevient actif soit
        # quand szi est cohérent (dégel dans _tick_level), soit après timeout
        # → done → ré-armement via _ladder_health_check (rythme borné 5 min).
        EPS = 1e-9
        if (tp_side == "sell" and position_szi <= EPS) or \
           (tp_side == "buy"  and position_szi >= -EPS):
            if lvl.state != "frozen":
                logger.warning(
                    "GRID %s level %s@%.4f: TP %s impossible (szi=%.6f) → frozen",
                    g.symbol, lvl.side, lvl.target_px, tp_side, position_szi,
                )
                lvl.frozen_since = time.time()
            lvl.state = "frozen"
            return

        oid = self._place_limit(g.symbol, tp_side, lvl.qty, tp_target, lev, reduce_only=True)
        if oid is None:
            # G1 : pas de retry infini. Le niveau est désarmé, retry au prochain
            # cycle complet (réactivation grid) seulement.
            logger.warning(
                "GRID %s: place_tp %s@%.4f échoué pour niveau %s@%.4f → niveau désarmé",
                g.symbol, tp_side, tp_target, lvl.side, lvl.target_px,
            )
            lvl.state = "done"
            return
        lvl.tp_oid = oid
        lvl.tp_target_px = tp_target
        lvl.state = "tp_placed"
        logger.info(
            "GRID %s level %s@%.4f → TP %s@%.4f oid=%d",
            g.symbol, lvl.side, lvl.target_px, tp_side, tp_target, oid,
        )

    def _close_position_if_open(self, symbol: str) -> None:
        """Si une position existe pour ce symbole, la ferme via market reduce_only.
        Évite les positions zombies après deactivate sur erreur."""
        try:
            us = self._exchange._client.get_user_state()
            for p in us.get("assetPositions", []):
                pos = p.get("position", p)
                if str(pos.get("coin", "")).upper() != symbol.upper():
                    continue
                szi = float(pos.get("szi", 0) or 0)
                if szi == 0:
                    return
                qty = abs(szi)
                close_side = "sell" if szi > 0 else "buy"
                lev_raw = pos.get("leverage", {})
                lev = int(lev_raw.get("value", 3) if isinstance(lev_raw, dict) else (lev_raw or 3))
                req = OrderRequest(
                    symbol=symbol, side=close_side, qty=qty,
                    order_type="market", price=0,
                    leverage=lev, reduce_only=True, client_id=None,
                )
                result = self._exchange.place_order(req)
                logger.warning(
                    "GRID %s position fermée d'urgence (deactivate close_position=True) qty=%.6f side=%s status=%s",
                    symbol, qty, close_side, getattr(result, "status", "?"),
                )
                return
        except Exception as e:
            logger.error("GRID %s _close_position_if_open: %r", symbol, e)

    # ─── Privé ────────────────────────────────────────────────────────────────

    def _get_tick_decimals(self, symbol: str) -> int:
        """Récupère le nombre de décimales prix tolérées par HL pour ce symbole.
        Cache après le 1er fetch. Fallback à 4 si meta indisponible.
        """
        sym = symbol.upper()
        if sym in self._tick_decimals:
            return self._tick_decimals[sym]
        try:
            meta = self._exchange._client.info.meta()
            universe = meta.get("universe", []) if isinstance(meta, dict) else []
            for u in universe:
                if str(u.get("name", "")).upper() == sym:
                    sz_dec = int(u.get("szDecimals", 3) or 3)
                    px_dec = max(0, 6 - sz_dec)
                    self._tick_decimals[sym] = px_dec
                    return px_dec
        except Exception as e:
            logger.warning("GRID %s: meta fetch échoué pour tick_decimals: %r", sym, e)
        fallback = {"BTC": 1, "ETH": 2, "BNB": 3, "SOL": 3, "BCH": 2, "AAVE": 3, "LINK": 4, "SUI": 5}.get(sym, 4)
        self._tick_decimals[sym] = fallback
        return fallback

    def _round_px(self, price: float, symbol: str = "") -> float:
        """Arrondit le prix au tick HL pour ce symbole (sinon HL rejette).
        Le paramètre symbol est optionnel pour compatibilité ; sans lui on
        retombe sur 6 décimales (legacy, BTC/ETH ok mais BNB ko).
        """
        try:
            if symbol:
                return round(float(price), self._get_tick_decimals(symbol))
            return round(float(price), 6)
        except Exception:
            return 0.0

    def _cancel_oid(self, symbol: str, oid: Optional[int]) -> None:
        if oid is None:
            return
        try:
            self._exchange.cancel_order(str(oid))
        except Exception as e:
            logger.warning("GRID %s cancel oid=%d: %r", symbol, oid, e)
        finally:
            try:
                get_order_registry().unregister(oid)
            except Exception:
                pass

    def _place_limit(
        self, symbol: str, side: str, qty: float, price: float,
        leverage: int, reduce_only: bool = False,
    ) -> Optional[int]:
        try:
            req = OrderRequest(
                symbol=symbol,
                side=side,
                qty=qty,
                order_type="limit",
                price=price,
                leverage=leverage,
                reduce_only=reduce_only,
                client_id=None,
            )
            result = self._exchange.place_order(req)
            oid_str = result.order_id
            if oid_str:
                oid_int = int(oid_str)
                try:
                    get_order_registry().register(
                        oid=oid_int,
                        source=SOURCE_GRID_TP if reduce_only else SOURCE_GRID_PENDING,
                        symbol=symbol,
                        intent="tp" if reduce_only else "open",
                        side=side,
                        is_trigger=False,
                        reduce_only=reduce_only,
                        qty=qty,
                        price=price,
                        meta={"leverage": leverage},
                    )
                except Exception as reg_e:
                    logger.warning("GRID %s registry.register %s: %r", symbol, oid_int, reg_e)
                return oid_int
            logger.warning("GRID %s place_limit %s@%.4f: oid vide", symbol, side, price)
            return None
        except Exception as e:
            logger.warning("GRID %s place_limit %s@%.4f: %r", symbol, side, price, e)
            return None


# Alias V6 pour compatibilité éventuelle pendant la transition
GridManager = GridEngine


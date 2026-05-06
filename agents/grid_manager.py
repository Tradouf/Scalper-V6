"""
GridManager — grille symétrique neutre pour marchés en range (Hyperliquid perpetuals).

Logique :
  Activation : place buy@(center - spacing/2) ET sell@(center + spacing/2) simultanément.
  - Si buy se remplit en premier → long ouvert → cancel le sell pending → TP sell reduce_only
  - Si sell se remplit en premier → short ouvert → cancel le buy pending → TP buy reduce_only
  - Quand TP rempli → profit = spacing - fees → nouveau cycle symétrique sur le prix courant

  Désactivation : régime passe à trend OU breakout hors de la fenêtre ±(LEVELS+1)×spacing.
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Set

from exchanges.base import OrderRequest

logger = logging.getLogger("sdm.grid")


@dataclass
class GridState:
    symbol: str
    center: float
    spacing: float
    qty: float
    phase: str = "symmetric"           # symmetric | waiting_sell_tp | waiting_buy_tp
    buy_oid: Optional[int] = None
    sell_oid: Optional[int] = None
    buy_fill_price: Optional[float] = None
    sell_fill_price: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    last_update: float = field(default_factory=time.time)
    trade_count: int = 0
    total_pnl_pct: float = 0.0


class GridManager:
    """Gère les grilles actives. Appelé depuis _grid_loop() dans main_v6."""

    def __init__(self, exchange):
        self._exchange = exchange
        self._grids: Dict[str, GridState] = {}
        self._deactivation_ts: Dict[str, float] = {}

    # ─── API publique ─────────────────────────────────────────────────────────

    def is_active(self, symbol: str) -> bool:
        return symbol in self._grids

    def active_symbols(self) -> list:
        return list(self._grids.keys())

    def can_activate(self, symbol: str) -> bool:
        from config.settings import GRID_COOLDOWN_SEC
        last = self._deactivation_ts.get(symbol, 0.0)
        remaining = GRID_COOLDOWN_SEC - (time.time() - last)
        if remaining > 0:
            logger.debug("GRID %s cooldown actif (%.0fs restantes)", symbol, remaining)
            return False
        return True

    def activate(self, symbol: str, center: float, atr: float) -> bool:
        """Place buy ET sell simultanément autour du center. Retourne True si succès."""
        if self.is_active(symbol):
            return False
        if not self.can_activate(symbol):
            return False

        from config.settings import GRID_ATR_FACTOR, GRID_NOTIONAL, GRID_LEVERAGE

        spacing = atr * GRID_ATR_FACTOR
        if spacing <= 0 or center <= 0:
            logger.warning("GRID %s: paramètres invalides (center=%.4f atr=%.4f)", symbol, center, atr)
            return False

        qty = round(GRID_NOTIONAL / center, 6)
        if qty * center < 10.5:
            logger.warning("GRID %s: notional trop faible (%.2f < $10.5)", symbol, qty * center)
            return False

        buy_price = round(center - spacing / 2, 6)
        sell_price = round(center + spacing / 2, 6)
        lev = int(GRID_LEVERAGE)

        buy_oid = self._place_limit(symbol, "buy", qty, buy_price, lev, reduce_only=False)
        if buy_oid is None:
            return False

        sell_oid = self._place_limit(symbol, "sell", qty, sell_price, lev, reduce_only=False)
        if sell_oid is None:
            self._cancel_oid(symbol, buy_oid)
            return False

        self._grids[symbol] = GridState(
            symbol=symbol,
            center=center,
            spacing=spacing,
            qty=qty,
            phase="symmetric",
            buy_oid=buy_oid,
            sell_oid=sell_oid,
        )
        logger.info(
            "GRID %s ACTIVÉ center=%.4f spacing=%.4f qty=%.6f | buy@%.4f oid=%d | sell@%.4f oid=%d",
            symbol, center, spacing, qty, buy_price, buy_oid, sell_price, sell_oid,
        )
        return True

    def on_tick(self, symbol: str, open_oids: Set[int], current_price: float) -> None:
        """Mise à jour état grille. Appelé toutes les TRAIL_CHECK_SEC secondes."""
        g = self._grids.get(symbol)
        if g is None:
            return

        from config.settings import GRID_LEVELS, GRID_LEVERAGE, GRID_GRACE_SEC

        if time.time() - g.created_at < GRID_GRACE_SEC:
            return

        g.last_update = time.time()
        lev = int(GRID_LEVERAGE)

        # Breakout guard
        breakout_limit = g.spacing * (GRID_LEVELS + 1)
        if abs(current_price - g.center) > breakout_limit:
            logger.info(
                "GRID %s BREAKOUT (price=%.4f center=%.4f ±%.4f) → désactivation",
                symbol, current_price, g.center, breakout_limit,
            )
            self.deactivate(symbol, cancel=True)
            return

        if g.phase == "symmetric":
            buy_filled = g.buy_oid is not None and g.buy_oid not in open_oids
            sell_filled = g.sell_oid is not None and g.sell_oid not in open_oids

            if buy_filled and sell_filled:
                # Les deux remplis quasi-simultanément → net 0, nouveau cycle
                g.trade_count += 1
                logger.info("GRID %s double fill → nouveau cycle", symbol)
                self._reset_symmetric(symbol, g, current_price, lev)

            elif buy_filled:
                # Long ouvert → cancel le sell pending, place TP sell reduce_only
                g.buy_fill_price = current_price
                self._cancel_oid(symbol, g.sell_oid)
                g.sell_oid = None
                tp_price = round(g.center + g.spacing / 2, 6)
                oid = self._place_limit(symbol, "sell", g.qty, tp_price, lev, reduce_only=True)
                if oid:
                    g.buy_oid = None
                    g.sell_oid = oid
                    g.phase = "waiting_sell_tp"
                    logger.info("GRID %s long ouvert → TP sell@%.4f oid=%d", symbol, tp_price, oid)
                else:
                    logger.warning("GRID %s: échec TP sell, désactivation + close position", symbol)
                    self.deactivate(symbol, cancel=False, close_position=True)

            elif sell_filled:
                # Short ouvert → cancel le buy pending, place TP buy reduce_only
                g.sell_fill_price = current_price
                self._cancel_oid(symbol, g.buy_oid)
                g.buy_oid = None
                tp_price = round(g.center - g.spacing / 2, 6)
                oid = self._place_limit(symbol, "buy", g.qty, tp_price, lev, reduce_only=True)
                if oid:
                    g.sell_oid = None
                    g.buy_oid = oid
                    g.phase = "waiting_buy_tp"
                    logger.info("GRID %s short ouvert → TP buy@%.4f oid=%d", symbol, tp_price, oid)
                else:
                    logger.warning("GRID %s: échec TP buy, désactivation + close position", symbol)
                    self.deactivate(symbol, cancel=False, close_position=True)

        elif g.phase == "waiting_sell_tp":
            if g.sell_oid is not None and g.sell_oid not in open_oids:
                buy_px = g.buy_fill_price or (g.center - g.spacing / 2)
                sell_px = g.center + g.spacing / 2
                pnl_pct = (sell_px - buy_px) / buy_px if buy_px > 0 else g.spacing / g.center
                g.total_pnl_pct += pnl_pct
                g.trade_count += 1
                logger.info(
                    "GRID %s long TP #%d pnl=%.3f%% cumul=%.3f%%",
                    symbol, g.trade_count, pnl_pct * 100, g.total_pnl_pct * 100,
                )
                self._reset_symmetric(symbol, g, current_price, lev)

        elif g.phase == "waiting_buy_tp":
            if g.buy_oid is not None and g.buy_oid not in open_oids:
                sell_px = g.sell_fill_price or (g.center + g.spacing / 2)
                buy_px = g.center - g.spacing / 2
                pnl_pct = (sell_px - buy_px) / buy_px if buy_px > 0 else g.spacing / g.center
                g.total_pnl_pct += pnl_pct
                g.trade_count += 1
                logger.info(
                    "GRID %s short TP #%d pnl=%.3f%% cumul=%.3f%%",
                    symbol, g.trade_count, pnl_pct * 100, g.total_pnl_pct * 100,
                )
                self._reset_symmetric(symbol, g, current_price, lev)

    def deactivate(self, symbol: str, cancel: bool = True, close_position: bool = False) -> None:
        """
        close_position=True : ferme aussi la position spot via reduce_only market.
        À utiliser quand la désactivation vient d'une erreur (insufficient margin,
        échec TP, échec reset symétrique). Sinon on laisse une position nue qui
        dérive sans surveillance grid (cf. bug BNB $242 du 2026-05-06).
        """
        g = self._grids.pop(symbol, None)
        if g is None:
            return
        if cancel:
            for oid in (g.buy_oid, g.sell_oid):
                if oid is not None:
                    self._cancel_oid(symbol, oid)
        self._deactivation_ts[symbol] = time.time()
        logger.info(
            "GRID %s désactivé phase=%s trades=%d pnl_cumul=%.3f%%",
            symbol, g.phase, g.trade_count, g.total_pnl_pct * 100,
        )
        if close_position:
            self._close_position_if_open(symbol)

    def _close_position_if_open(self, symbol: str) -> None:
        """Si une position spot existe pour ce symbole, la ferme via market reduce_only.
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

    def deactivate_all(self) -> None:
        for sym in list(self._grids.keys()):
            self.deactivate(sym, cancel=True)

    # ─── Privé ────────────────────────────────────────────────────────────────

    def _reset_symmetric(self, symbol: str, g: GridState, current_price: float, lev: int) -> None:
        """Recentre la grille sur le prix courant et replace les deux ordres."""
        g.center = current_price
        buy_price = round(current_price - g.spacing / 2, 6)
        sell_price = round(current_price + g.spacing / 2, 6)

        buy_oid = self._place_limit(symbol, "buy", g.qty, buy_price, lev, reduce_only=False)
        if buy_oid is None:
            # _reset_symmetric : ne devrait pas y avoir de position résiduelle (TP juste fillé)
            # mais si margin insuffisante d'un autre symbole, mieux vaut vérifier et fermer.
            self.deactivate(symbol, cancel=False, close_position=True)
            return

        sell_oid = self._place_limit(symbol, "sell", g.qty, sell_price, lev, reduce_only=False)
        if sell_oid is None:
            self._cancel_oid(symbol, buy_oid)
            self.deactivate(symbol, cancel=False, close_position=True)
            return

        g.buy_oid = buy_oid
        g.sell_oid = sell_oid
        g.phase = "symmetric"
        g.buy_fill_price = None
        g.sell_fill_price = None
        g.created_at = time.time()  # remet la période de grâce
        logger.info(
            "GRID %s nouveau cycle center=%.4f buy@%.4f sell@%.4f",
            symbol, current_price, buy_price, sell_price,
        )

    def _cancel_oid(self, symbol: str, oid: Optional[int]) -> None:
        if oid is None:
            return
        try:
            self._exchange.cancel_order(str(oid))
        except Exception as e:
            logger.warning("GRID %s cancel oid=%d: %r", symbol, oid, e)

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
                return int(oid_str)
            logger.warning("GRID %s place_limit %s@%.4f: oid vide", symbol, side, price)
            return None
        except Exception as e:
            logger.warning("GRID %s place_limit %s@%.4f: %r", symbol, side, price, e)
            return None

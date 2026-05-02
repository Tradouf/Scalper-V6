"""
GridManager — stratégie grid pour marchés en range (Hyperliquid perpetuals).

Logique : 1 unité par symbole (buy limit + sell TP).
  - Activation : regime.trend == "range" et aucune position scalp ouverte
  - Désactivation : régime passe à trend OU breakout hors de la grille
  - Cycle : buy@(center - spacing/2) → fill → sell@(center + spacing/2) → repeat
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
    phase: str = "waiting_buy"      # waiting_buy | waiting_sell
    buy_oid: Optional[int] = None
    sell_oid: Optional[int] = None
    buy_fill_price: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    last_update: float = field(default_factory=time.time)
    trade_count: int = 0
    total_pnl_pct: float = 0.0


class GridManager:
    """Gère les grilles actives. Appelé depuis _grid_loop() dans main_v6."""

    def __init__(self, exchange):
        self._exchange = exchange
        self._grids: Dict[str, GridState] = {}
        self._deactivation_ts: Dict[str, float] = {}  # cooldown post-désactivation

    # ─── API publique ─────────────────────────────────────────────────────────

    def is_active(self, symbol: str) -> bool:
        return symbol in self._grids

    def active_symbols(self) -> list:
        return list(self._grids.keys())

    def can_activate(self, symbol: str) -> bool:
        """Retourne False si le symbole est en cooldown post-désactivation."""
        from config.settings import GRID_COOLDOWN_SEC
        last = self._deactivation_ts.get(symbol, 0.0)
        remaining = GRID_COOLDOWN_SEC - (time.time() - last)
        if remaining > 0:
            logger.debug("GRID %s cooldown actif (%.0fs restantes)", symbol, remaining)
            return False
        return True

    def activate(self, symbol: str, center: float, atr: float) -> bool:
        """Démarre une grille sur ce symbole. Retourne True si succès."""
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
        oid = self._place_limit(symbol, "buy", qty, buy_price, int(GRID_LEVERAGE))
        if oid is None:
            return False

        self._grids[symbol] = GridState(
            symbol=symbol,
            center=center,
            spacing=spacing,
            qty=qty,
            phase="waiting_buy",
            buy_oid=oid,
        )
        logger.info(
            "GRID %s ACTIVÉ center=%.4f spacing=%.4f qty=%.6f buy@%.4f oid=%d",
            symbol, center, spacing, qty, buy_price, oid,
        )
        return True

    def on_tick(self, symbol: str, open_oids: Set[int], current_price: float) -> None:
        """Mise à jour état grille. Appelé toutes les TRAIL_CHECK_SEC secondes."""
        g = self._grids.get(symbol)
        if g is None:
            return

        from config.settings import GRID_LEVELS, GRID_LEVERAGE

        g.last_update = time.time()

        # Breakout guard : annule si le prix sort de la fenêtre prévue
        breakout_limit = g.spacing * (GRID_LEVELS + 1)
        if abs(current_price - g.center) > breakout_limit:
            logger.info(
                "GRID %s BREAKOUT (price=%.4f center=%.4f ±%.4f) → désactivation",
                symbol, current_price, g.center, breakout_limit,
            )
            self.deactivate(symbol, cancel=True)
            return

        if g.phase == "waiting_buy":
            if g.buy_oid is not None and g.buy_oid not in open_oids:
                # Buy rempli → TP sell
                g.buy_fill_price = current_price
                tp_price = round(g.center + g.spacing / 2, 6)
                oid = self._place_limit(symbol, "sell", g.qty, tp_price, int(GRID_LEVERAGE))
                if oid:
                    g.buy_oid = None
                    g.sell_oid = oid
                    g.phase = "waiting_sell"
                    logger.info("GRID %s buy rempli → TP sell@%.4f oid=%d", symbol, tp_price, oid)
                else:
                    logger.warning("GRID %s: échec TP sell, désactivation", symbol)
                    self.deactivate(symbol, cancel=False)

        elif g.phase == "waiting_sell":
            if g.sell_oid is not None and g.sell_oid not in open_oids:
                # TP rempli → cycle complet, on relance
                sell_price = g.center + g.spacing / 2
                buy_fill = g.buy_fill_price or (g.center - g.spacing / 2)
                pnl_pct = (sell_price - buy_fill) / buy_fill if buy_fill > 0 else 0.0
                g.total_pnl_pct += pnl_pct
                g.trade_count += 1
                logger.info(
                    "GRID %s cycle #%d OK pnl=%.3f%% cumul=%.3f%%",
                    symbol, g.trade_count, pnl_pct * 100, g.total_pnl_pct * 100,
                )
                # Nouveau cycle
                buy_price = round(g.center - g.spacing / 2, 6)
                oid = self._place_limit(symbol, "buy", g.qty, buy_price, int(GRID_LEVERAGE))
                if oid:
                    g.sell_oid = None
                    g.buy_oid = oid
                    g.phase = "waiting_buy"
                else:
                    self.deactivate(symbol, cancel=False)

    def deactivate(self, symbol: str, cancel: bool = True) -> None:
        g = self._grids.pop(symbol, None)
        if g is None:
            return
        if cancel:
            for oid in (g.buy_oid, g.sell_oid):
                if oid is not None:
                    try:
                        self._exchange.cancel_order(str(oid))
                    except Exception as e:
                        logger.warning("GRID %s cancel oid=%d: %r", symbol, oid, e)
        self._deactivation_ts[symbol] = time.time()
        logger.info(
            "GRID %s désactivé phase=%s trades=%d pnl_cumul=%.3f%%",
            symbol, g.phase, g.trade_count, g.total_pnl_pct * 100,
        )

    def deactivate_all(self) -> None:
        for sym in list(self._grids.keys()):
            self.deactivate(sym, cancel=True)

    # ─── Privé ────────────────────────────────────────────────────────────────

    def _place_limit(
        self, symbol: str, side: str, qty: float, price: float, leverage: int
    ) -> Optional[int]:
        try:
            req = OrderRequest(
                symbol=symbol,
                side=side,
                qty=qty,
                order_type="limit",
                price=price,
                leverage=leverage,
                reduce_only=False,
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

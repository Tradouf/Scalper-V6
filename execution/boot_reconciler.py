"""
BootReconciler — synchronise V7 avec l'état HL au démarrage live.

Au cutover V6 → V7, V6 laisse des positions ouvertes + des ordres
(grid, SL/TP, recovery) côté HL et dans memory/order_registry.json.
V7 doit voir tout ça avant de commencer à décider, sinon :
  - Le portfolio démarre vide → reconcile() voudrait rouvrir des positions
    qui existent déjà → doublons.
  - L'order_registry contient des OIDs de session V6 → ghosts si HL les a
    déjà fermés, ou orphans si HL les a encore et V7 ne les tracke pas.

Séquence (idempotente, peut être ré-appelée à tout restart) :
  1. Lit positions HL → portfolio.set_position()
  2. Lit equity HL → portfolio.set_equity()
  3. Lit open_orders HL
  4. Registry.purge_ghosts(live_oids) — vire les OIDs absents de HL
  5. Registry.absorb_orphans(live_orders) — tag UNKNOWN les OIDs vivants HL
     que le registre ne connaît pas (puis grid cleanup_unknown peut les annuler).

Aucun ordre n'est placé/annulé ici. Décisions de cleanup délégué à GridEngine.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from execution.order_registry import get_order_registry

if TYPE_CHECKING:
    from execution.hyperliquid_adapter import HyperliquidReadAdapter
    from execution.portfolio import PortfolioImpl

logger = logging.getLogger("v7.bootreconciler")


class BootReconciler:
    def __init__(
        self,
        read_adapter: "HyperliquidReadAdapter",
        write_adapter,
        portfolio: "PortfolioImpl",
        manual_symbols: list | None = None,
        mr_strategy=None,
    ) -> None:
        self._read = read_adapter
        self._write = write_adapter
        self._portfolio = portfolio
        # 2026-06-07 : positions manuelles (francois) — ne JAMAIS les charger
        # dans le portfolio, sinon le reconcile les solde (target=0).
        self._manual = set(manual_symbols or [])
        # 2026-06-17 : stratégie MR à hydrater au boot. Sans ré-attribution, les
        # positions chargées dans le portfolio sont orphelines (aucune stratégie
        # ne les pilote) → coupées seulement au stop -5 %. On les adopte dans MR
        # (gestion z-revert + SL natif). None en paper / tests → adoption skip.
        self._mr = mr_strategy

    def reconcile(self) -> dict:
        """Retourne un résumé chiffré pour le log boot."""
        out = {
            "positions_loaded": 0,
            "equity": 0.0,
            "orders_live": 0,
            "ghosts_purged": 0,
            "orphans_absorbed": 0,
            "positions_adopted": 0,
            "errors": [],
        }

        # 1. Positions HL → portfolio
        try:
            positions = self._read.get_positions()  # {asset → signed notional}
            skipped_manual = {a: n for a, n in positions.items() if a in self._manual}
            positions = {a: n for a, n in positions.items() if a not in self._manual}
            for asset, notional in positions.items():
                self._portfolio.set_position(asset, float(notional))
            out["positions_loaded"] = len(positions)
            logger.info(
                "BootReconciler: %d positions HL chargées dans portfolio: %s",
                len(positions),
                ", ".join(f"{a}={n:+.2f}$" for a, n in list(positions.items())[:10]),
            )
            if skipped_manual:
                logger.warning(
                    "BootReconciler: %d position(s) MANUELLE(S) ignorée(s) (non gérées par le bot): %s",
                    len(skipped_manual),
                    ", ".join(f"{a}={n:+.2f}$" for a, n in skipped_manual.items()),
                )
        except Exception as e:
            out["errors"].append(f"positions: {e!r}")
            logger.warning("BootReconciler positions error: %r", e)

        # 2. Equity HL
        try:
            equity = self._read.get_equity()
            if equity > 0:
                self._portfolio.set_equity(equity)
                out["equity"] = equity
                logger.info("BootReconciler: equity HL = $%.2f", equity)
        except Exception as e:
            out["errors"].append(f"equity: {e!r}")
            logger.warning("BootReconciler equity error: %r", e)

        # 2b. Ré-attribution des positions à MR (2026-06-17). Chaque position
        # non-manuelle est adoptée par la stratégie MR : maintien + sortie au
        # retour à la moyenne + SL natif. Sans ça elle reste orpheline (ingérable
        # jusqu'au stop -5 %). Décision francois 06-17 : même les orphelines de
        # grille (non reconstructibles en GridState) vont dans MR + SL.
        if self._mr is not None:
            try:
                out["positions_adopted"] = self._adopt_into_mr()
            except Exception as e:
                out["errors"].append(f"adopt: {e!r}")
                logger.warning("BootReconciler adoption MR error: %r", e)

        # 3 + 4 + 5. Open orders → reconcile registry
        try:
            live_orders = self._write.get_open_orders()
            out["orders_live"] = len(live_orders)
            live_oids = set()
            for o in live_orders:
                try:
                    live_oids.add(int(o.get("oid")))
                except Exception:
                    continue

            reg = get_order_registry()
            # 4. Ghosts : OIDs registry absents HL → purge
            ghosts = reg.purge_ghosts(live_oids)
            out["ghosts_purged"] = ghosts
            # 5. Orphans : OIDs HL non registrés → absorb (tag UNKNOWN)
            orphans = reg.absorb_orphans(live_orders)
            out["orphans_absorbed"] = orphans

            logger.info(
                "BootReconciler: %d ordres live HL, %d ghosts purgés, %d orphans absorbés (UNKNOWN)",
                len(live_orders), ghosts, orphans,
            )
        except Exception as e:
            out["errors"].append(f"orders: {e!r}")
            logger.warning("BootReconciler orders error: %r", e)

        return out

    def _adopt_into_mr(self) -> int:
        """Adopte dans MR chaque position HL non-manuelle de la watchlist MR.

        Retourne le nombre adopté. Les positions hors watchlist MR ne sont pas
        adoptables (MR ne les évalue pas) : on les signale comme restant
        orphelines. Extrait pour testabilité (pas d'I/O registre/disque)."""
        detailed = self._read.get_positions_detailed()
        mr_syms = set(getattr(self._mr, "_symbols", []))
        adopted: list[str] = []
        skipped_off_watchlist: list[str] = []
        for coin, d in detailed.items():
            if coin in self._manual:
                continue
            szi = float(d.get("szi", 0) or 0)
            if szi == 0:
                continue
            if coin not in mr_syms:
                skipped_off_watchlist.append(coin)
                continue
            side = "buy" if szi > 0 else "sell"
            if self._mr.adopt_position(
                coin, side, float(d.get("entry_px", 0) or 0), abs(szi),
            ):
                adopted.append(coin)
        if adopted:
            logger.info(
                "BootReconciler: %d position(s) adoptée(s) par MR (gérée + SL): %s",
                len(adopted), ", ".join(adopted),
            )
        if skipped_off_watchlist:
            logger.warning(
                "BootReconciler: %d position(s) hors watchlist MR NON adoptée(s) "
                "(restent orphelines, coupées au stop -5%%): %s",
                len(skipped_off_watchlist), ", ".join(skipped_off_watchlist),
            )
        return len(adopted)

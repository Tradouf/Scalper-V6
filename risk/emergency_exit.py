"""
EmergencyExitManager — filet de sécurité par-position (port V6 + Fix #8).

Mécanisme indépendant du flow normal (signals→target→reconcile). Lit l'état
HL réel à intervalle régulier, force-close toute position dont le ROE
dépasse `emergency_exit_roe_pct` (défaut -2.2%).

Deux branches :
  - **tracée** : position présente dans portfolio V7 (= notional non-zero).
    Le force-close est immédiat (CRITICAL log) — c'est le filet pour les
    positions ouvertes par le bot que les stratégies n'ont pas refermées.
  - **orpheline** : position HL absente du portfolio V7. C'est un fill
    inattendu (Fix 7 résiduel, action manuelle HL, bug). Fix #8 :
    grâce `orphan_grace_sec` avant force-close pour ne pas fermer sur
    snapshot périmé / mèche. 1ʳᵉ obs → WARN + arme timer ; force-close
    seulement après grâce continue.

Mode paper : skip complet (PaperExchange ne synchronise pas avec HL).
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Dict

from execution.types import OrderRequest

if TYPE_CHECKING:
    from core.config import RiskConfig
    from execution.hyperliquid_adapter import HyperliquidReadAdapter
    from execution.portfolio import PortfolioImpl

logger = logging.getLogger("v7.emergency")


class EmergencyExitManager:
    def __init__(
        self,
        cfg: "RiskConfig",
        read_adapter: "HyperliquidReadAdapter",
        write_adapter,
        portfolio: "PortfolioImpl",
        paper_mode: bool = True,
    ) -> None:
        self._cfg = cfg
        self._read = read_adapter
        self._write = write_adapter
        self._portfolio = portfolio
        self._paper = paper_mode
        # Fix #8 : timer de grâce par symbole orphelin.
        self._orphan_emergency_since: Dict[str, float] = {}

    def check_and_exit(self) -> dict:
        """Une passe de vérification. Retourne un résumé chiffré pour le log."""
        out = {
            "checked": 0,
            "tracked_emergency": 0,
            "orphan_grace_armed": 0,
            "orphan_force_closed": 0,
            "errors": [],
        }
        if self._paper or not self._cfg.emergency_exit_enabled:
            return out

        try:
            positions = self._read.get_positions_detailed()
        except Exception as e:
            out["errors"].append(f"read positions: {e!r}")
            logger.warning("EmergencyExit read error: %r", e)
            return out

        out["checked"] = len(positions)
        threshold = -abs(self._cfg.emergency_exit_roe_pct)
        now = time.time()
        portfolio_assets = {a for a, n in self._portfolio.positions.items() if abs(n) > 1e-9}

        # Reset des timers pour positions sorties de zone ou disparues.
        seen_in_zone = set()

        for asset, info in positions.items():
            roe = info["roe"]
            if roe > threshold:
                # Hors zone : reset timer si armé.
                if asset in self._orphan_emergency_since:
                    logger.info(
                        "EmergencyExit %s : ROE %.3f%% hors zone, timer désarmé",
                        asset, roe * 100,
                    )
                    self._orphan_emergency_since.pop(asset, None)
                continue

            # En zone : tracée ou orpheline ?
            is_tracked = asset in portfolio_assets
            seen_in_zone.add(asset)

            if is_tracked:
                # Branche tracée : force-close immédiat (CRITICAL).
                logger.critical(
                    "EMERGENCY EXIT %s ROE=%.3f%% ≤ -%.3f%% — force close "
                    "(entry=%.4f mark=%.4f side=%s lev=%.0fx)",
                    asset, roe * 100, abs(threshold) * 100,
                    info["entry_px"], info["mark_px"], info["side"], info["leverage"],
                )
                ok = self._force_close(asset, info)
                if ok:
                    out["tracked_emergency"] += 1
                continue

            # Branche orpheline : Fix #8 grâce.
            grace = float(self._cfg.orphan_grace_sec)
            armed_since = self._orphan_emergency_since.get(asset)
            if armed_since is None:
                self._orphan_emergency_since[asset] = now
                out["orphan_grace_armed"] += 1
                logger.warning(
                    "EMERGENCY EXIT (orphan) %s ROE=%.3f%% — grâce %.0fs avant force close "
                    "(entry=%.4f mark=%.4f side=%s)",
                    asset, roe * 100, grace,
                    info["entry_px"], info["mark_px"], info["side"],
                )
            elif now - armed_since >= grace:
                logger.critical(
                    "EMERGENCY EXIT (orphan) %s ROE=%.3f%% — grâce %.0fs écoulée → force close",
                    asset, roe * 100, grace,
                )
                ok = self._force_close(asset, info)
                if ok:
                    out["orphan_force_closed"] += 1
                    self._orphan_emergency_since.pop(asset, None)
            else:
                # Encore dans la grâce, on attend.
                pass

        # Garbage collect : positions disparues HL → drop timer.
        for asset in list(self._orphan_emergency_since.keys()):
            if asset not in positions:
                self._orphan_emergency_since.pop(asset, None)

        return out

    def _force_close(self, asset: str, info: dict) -> bool:
        """Émet un market reduce_only pour fermer la position. True si OK."""
        try:
            szi = info["szi"]
            qty = abs(szi)
            side = "sell" if szi > 0 else "buy"
            req = OrderRequest(
                symbol=asset, side=side, qty=qty,
                order_type="market", price=None,
                leverage=int(info["leverage"]) or 1,
                reduce_only=True,
                strategy_id="emergency_exit",
            )
            result = self._write.place_order(req)
            status = getattr(result, "status", "?")
            if status in ("filled", "accepted"):
                logger.warning(
                    "EmergencyExit %s force-close %s qty=%.6f status=%s",
                    asset, side, qty, status,
                )
                return True
            logger.error(
                "EmergencyExit %s force-close ÉCHOUÉ status=%s — position non protégée",
                asset, status,
            )
            return False
        except Exception as e:
            logger.error("EmergencyExit %s force-close exception: %r", asset, e)
            return False

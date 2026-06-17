"""
NativeStopManager — réconcilie des SL natifs (stop-market reduce-only) côté HL
avec les SL souhaités par les stratégies, à chaque tick (2026-06-17).

Pourquoi par tick et non « à l'entrée » : un SL doit aussi protéger les positions
ADOPTÉES au boot (aucun ordre d'entrée dans la session) et survivre aux restarts.
Dériver l'état désiré de la POSITION tenue (MR.desired_stops()) puis réconcilier
les ordres trigger HL est idempotent et couvre entrée + adoption + restart.

Sécurité :
  - n'agit QUE sur les symboles de la watchlist fournie, JAMAIS sur les manuels
    (francois pose ses propres SL sur HYPE/swings — on n'y touche pas) ;
  - ne pose/annule que des triggers reduce_only `tpsl=sl` ;
  - dédoublonne (un seul SL par symbole) ;
  - si le niveau de stop est déjà franchi (mark au-delà), on NE pose pas (HL
    rejette un trigger immédiat) — la sortie gérée / l'emergency s'en charge.

Idempotent : appelable à chaque tick. Tolérances pour éviter le churn (un SL
déjà au bon niveau ± trigger_tol n'est pas reposé).
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

from execution.types import OrderRequest

logger = logging.getLogger("v7.stopmanager")


def _is_sl_order(o: dict) -> bool:
    """True si l'ordre ouvert est un SL natif (trigger reduce-only tpsl=sl)."""
    is_trig = bool(o.get("isTrigger", False))
    ro = bool(o.get("reduceOnly", o.get("reduce_only", False)))
    tpsl = str(o.get("tpsl", "")).lower()
    return is_trig and ro and tpsl == "sl"


class NativeStopManager:
    def __init__(
        self,
        exchange,
        symbols,
        manual_symbols=None,
        price_cb=None,
        trigger_tol_pct: float = 0.002,
        qty_tol_pct: float = 0.15,
    ) -> None:
        self._ex = exchange
        self._syms = set(symbols)
        self._manual = set(manual_symbols or [])
        self._price_cb = price_cb
        self._trigger_tol = float(trigger_tol_pct)
        self._qty_tol = float(qty_tol_pct)

    def reconcile(self, desired: Dict[str, Dict]) -> Dict[str, int]:
        """`desired` = {sym → {stop_px, side(=sens position buy/sell), qty}}.
        Retourne un résumé chiffré {placed, cancelled, kept, skipped, errors}."""
        out = {"placed": 0, "cancelled": 0, "kept": 0, "skipped": 0, "errors": 0}
        # Ne gère que la watchlist, jamais les manuels.
        managed = self._syms - self._manual
        desired = {s: d for s, d in desired.items() if s in managed}

        existing = self._existing_sls(managed)

        for sym in managed:
            want = desired.get(sym)
            cur = existing.get(sym, [])
            try:
                if want is None:
                    # Plus de position tenue → annuler tout SL résiduel.
                    for o in cur:
                        if self._cancel(o):
                            out["cancelled"] += 1
                    continue
                self._reconcile_symbol(sym, want, cur, out)
            except Exception as e:
                out["errors"] += 1
                logger.warning("StopManager %s reconcile error: %r", sym, e)
        return out

    # ─── interne ──────────────────────────────────────────────────────────────

    def _reconcile_symbol(self, sym, want, cur, out) -> None:
        stop_px = float(want["stop_px"])
        qty = float(want["qty"])
        pos_side = str(want["side"]).lower()
        order_side = "sell" if pos_side == "buy" else "buy"  # stop = sens opposé

        # Un SL existant déjà au bon niveau (± tol) et bonne taille (± tol) ?
        match = None
        extras = []
        for o in cur:
            trig = o.get("triggerPx")
            sz = o.get("sz")
            ok = (
                match is None and trig and sz
                and abs(float(trig) - stop_px) <= self._trigger_tol * max(stop_px, 1e-9)
                and abs(float(sz) - qty) <= self._qty_tol * max(qty, 1e-9)
            )
            if ok:
                match = o
            else:
                extras.append(o)

        # Dédoublonnage : on annule tout SL surnuméraire / mal placé.
        for o in extras:
            if self._cancel(o):
                out["cancelled"] += 1

        if match is not None:
            out["kept"] += 1
            return

        # Pas de SL conforme → en poser un (sauf si déjà franchi).
        if self._is_breached(sym, pos_side, stop_px):
            out["skipped"] += 1
            logger.info(
                "StopManager %s : stop %.6g déjà franchi (pos %s) → pas de SL natif "
                "(sortie gérée/emergency prend le relais)", sym, stop_px, pos_side,
            )
            return

        if self._place(sym, order_side, qty, stop_px):
            out["placed"] += 1
        else:
            out["errors"] += 1

    def _existing_sls(self, managed) -> Dict[str, list]:
        """{sym → [orders SL]} restreint à la watchlist gérée."""
        try:
            orders = self._ex.get_open_orders() or []
        except Exception as e:
            logger.warning("StopManager get_open_orders error: %r", e)
            return {}
        res: Dict[str, list] = {}
        for o in orders:
            coin = str(o.get("coin", "")).upper()
            if coin not in managed:
                continue
            if _is_sl_order(o):
                res.setdefault(coin, []).append(o)
        return res

    def _is_breached(self, sym, pos_side, stop_px) -> bool:
        """Le mark a-t-il déjà dépassé le stop ? (long: mark<=stop ; short: mark>=stop)."""
        mark = self._mark(sym)
        if mark is None or mark <= 0:
            return False  # prix inconnu → on tente quand même
        return mark <= stop_px if pos_side == "buy" else mark >= stop_px

    def _mark(self, sym) -> Optional[float]:
        if self._price_cb is not None:
            try:
                p = self._price_cb(sym)
                if p and p > 0:
                    return float(p)
            except Exception:
                pass
        try:
            p = self._ex.get_mark_price(sym)
            if p and p > 0:
                return float(p)
        except Exception:
            pass
        return None

    def _place(self, sym, order_side, qty, trigger_px) -> bool:
        req = OrderRequest(
            symbol=sym, side=order_side, qty=qty, order_type="market",
            reduce_only=True, is_stop=True, trigger_px=float(trigger_px),
            strategy_id="mean_reversion",
        )
        try:
            res = self._ex.place_order(req)
        except Exception as e:
            logger.warning("StopManager place SL %s error: %r", sym, e)
            return False
        status = getattr(res, "status", "?")
        if status in ("accepted", "filled"):
            logger.info("StopManager SL posé %s %s qty=%.6f trigger=%.6g (oid=%s)",
                        sym, order_side, qty, trigger_px, getattr(res, "order_id", "?"))
            return True
        logger.warning("StopManager SL %s rejeté status=%s", sym, status)
        return False

    def _cancel(self, order: dict) -> bool:
        oid = order.get("oid")
        if oid is None:
            return False
        try:
            res = self._ex.cancel_order(str(oid))
        except Exception as e:
            logger.warning("StopManager cancel %s error: %r", oid, e)
            return False
        ok = bool(getattr(res, "success", False))
        if ok:
            logger.info("StopManager SL annulé %s oid=%s (position close/déplacée)",
                        order.get("coin"), oid)
        return ok

"""
ExecutionEngine — réconcilie le portfolio courant vers le portfolio cible.

Algorithme reconcile() :
  - Pour chaque asset présent dans target OU current :
      diff = target.target_notional - current.notional
      si |diff| < rebalance_threshold * max(gross_target, 1) : skip (bande non-trade)
      sinon : génère un OrderImpl market (qty = |diff|/price, side selon signe)
  - L'OrderImpl porte strategy_id = stratégie majoritaire du contributing_strategies.
  - reduce_only = True si on réduit une position (|new| < |current|).

submit() délègue à ExchangeClient.place_order() (paper ou live).
"""
from __future__ import annotations

import dataclasses
import logging
import time
from typing import Dict, Optional

from core.config import ExecutionConfig
from core.types import Fill, TargetPortfolio
from execution.order import OrderImpl
from execution.portfolio import PortfolioImpl
from execution.types import ExchangeClient, OrderRequest

logger = logging.getLogger("v7.execution")

FEE_TAKER_BPS = 4.5
FEE_MAKER_BPS = 1.5


class ExecutionEngine:
    """Implémente le Protocol ExecutionEngine."""

    # Fix #4 (port V6 5316822) : seuil dust. Une position résiduelle dont la
    # valeur en USD est sous ce seuil est traitée comme 0 dans reconcile.
    # Évite le blocage observé V6 (BTC short qty=1e-05 ≈ $0.74, HL rejette
    # le reduce_only sous min ~$10 → grid figée indéfiniment sur ce symbole).
    DUST_NOTIONAL_USD = 2.0

    # HL rejette tout ordre dont le notional est sous ~$10. Un ordre sous ce seuil (ouverture
    # OU réduction) sera TOUJOURS rejeté → le soumettre est futile (la position reste identique)
    # et ne fait que spammer des ERROR + gaspiller des appels API. On le skippe en amont. Cas
    # typique : une position résiduelle de $2-$10 (au-dessus du filtre dust, sous le min HL) qu'on
    # voudrait fermer/réduire — impossible sur HL, donc on la laisse telle quelle.
    MIN_ORDER_NOTIONAL_USD = 10.0

    def __init__(
        self,
        exchange: ExchangeClient,
        cfg: ExecutionConfig,
        prices_callback=None,
        bandit=None,
    ) -> None:
        """`prices_callback(asset) -> price` : utilisé pour convertir notional → qty
        si le prix mark courant n'est pas dans le target. Si None, on tente
        `exchange.get_mark_price(asset)`.

        `bandit` : BanditPolicy optionnelle (exec_bandit_active). None ou
        choix "taker_now" → market historique. Bras limit → ordre limit GTC
        suivi dans `_pending`, résolu par poll_pending() (fill ou timeout →
        cancel + fallback market).
        """
        self._exchange = exchange
        self._cfg = cfg
        self._prices_callback = prices_callback
        self._bandit = bandit
        self._pending: list[dict] = []   # {oid, order, limit_px, deadline, arm}

    # ─── API publique (Protocol) ─────────────────────────────────────────────

    def reconcile(
        self,
        target: TargetPortfolio,
        current: PortfolioImpl,
    ) -> list[OrderImpl]:
        """Calcule les ordres nécessaires pour passer de `current` à `target`."""
        # Bande de non-trade : seuil en USD
        # On utilise rebalance_threshold_pct × max(gross_target, equity_courante).
        # Sur target avec gross=0 mais des positions courantes (signal CLOSE), on
        # utilise l'equity comme référence.
        ref = max(target.gross_exposure, current.equity, 1.0)
        threshold_usd = self._cfg.rebalance_threshold_pct * ref

        orders: list[OrderImpl] = []
        target_by_asset = {p.asset: p for p in target.positions}
        current_assets = set(current.positions.keys())
        all_assets = set(target_by_asset.keys()) | current_assets

        for asset in all_assets:
            current_n = current.positions.get(asset, 0.0)
            # Fix #4 : ignorer une position dust (< DUST_NOTIONAL_USD en valeur
            # absolue notionnelle). HL refuse de la fermer (sous min ordre) →
            # sans ce filtre, reconcile générerait à chaque tick un order qty=1e-5
            # qui sera rejeté, bloquant toute nouvelle activité sur le symbole.
            if 0 < abs(current_n) < self.DUST_NOTIONAL_USD:
                logger.debug(
                    "ExecutionEngine %s : position dust %.4f$ < %.2f$ → traitée comme 0",
                    asset, current_n, self.DUST_NOTIONAL_USD,
                )
                current_n = 0.0
            tp = target_by_asset.get(asset)
            wanted = tp.target_notional if tp else 0.0
            diff = wanted - current_n
            if abs(diff) < threshold_usd:
                # Bande non-trade
                continue

            # Détermine la stratégie attribuée
            strategy_id = None
            if tp and tp.contributing_strategies:
                strategy_id = max(
                    tp.contributing_strategies.items(),
                    key=lambda kv: abs(kv[1]),
                )[0]

            # Récupère le prix mark courant
            price = self._get_price(asset)
            if price is None or price <= 0:
                logger.warning("ExecutionEngine: no price for %s, skip", asset)
                continue

            qty = abs(diff) / price
            if qty <= 0:
                # Cas-limite (rebalance_threshold_pct=0 + dust filtré) : on n'a
                # rien à exécuter mais on est passé sous la bande non-trade.
                continue
            if abs(diff) < self.MIN_ORDER_NOTIONAL_USD:
                # Sous le min HL → rejet garanti. On skippe (la position reste inchangée,
                # comme si l'ordre avait été envoyé puis rejeté, mais sans ERROR ni appel API).
                logger.debug(
                    "ExecutionEngine %s : ordre %.2f$ < min HL %.2f$ → skip (rejet garanti)",
                    asset, abs(diff), self.MIN_ORDER_NOTIONAL_USD,
                )
                continue
            side = "buy" if diff > 0 else "sell"
            # reduce_only : uniquement si on reste du même côté ET on réduit.
            # Pour un flip (long → short ou inverse), RO=False car HL clampe
            # un ordre RO à 0 (ne peut pas inverser). Le reste à exécuter
            # (overshoot) sera capté au reconcile suivant.
            same_side = (
                (current_n > 0 and wanted >= 0) or
                (current_n < 0 and wanted <= 0) or
                (current_n == 0)
            )
            reduce_only = same_side and abs(wanted) < abs(current_n) and current_n != 0

            orders.append(OrderImpl(
                asset=asset,
                side=side,
                qty=qty,
                order_type="market",
                price=None,
                reduce_only=reduce_only,
                strategy_id=strategy_id,
            ))
            logger.debug(
                "ExecutionEngine reconcile %s : current=%.2f wanted=%.2f diff=%.2f → %s qty=%.6f%s",
                asset, current_n, wanted, diff, side, qty,
                " (RO)" if reduce_only else "",
            )

        return orders

    def submit(self, orders: list[OrderImpl], use_bandit: bool = True) -> list[Fill]:
        """Envoie les ordres et retourne les fills correspondants.

        On crée un Fill à partir du résultat de place_order(). Pour un fill
        accepted (market HL est presque toujours filled), on utilise les
        attributs de la requête (price, qty signée par side).

        Si une BanditPolicy est branchée (exec_bandit_active) et qu'elle choisit
        un bras limit : l'ordre part en limit GTC et rejoint `_pending` au lieu
        de produire un Fill immédiat — poll_pending() le résoudra. Les fallbacks
        de poll_pending repassent ici avec use_bandit=False (pas de récursion).
        """
        fills: list[Fill] = []
        for order in orders:
            # ── Bandit : market historique ou limit adaptatif ? ──────────────
            arm, limit_px = "taker_now", None
            if use_bandit and self._bandit is not None and order.order_type == "market":
                arm, limit_px = self._bandit.choose(
                    order.asset, order.side,
                    notional=order.qty * (self._get_price(order.asset) or 0.0),
                    reduce_only=order.reduce_only,
                )
            if arm != "taker_now" and limit_px and limit_px > 0:
                order = dataclasses.replace(order, order_type="limit", price=limit_px)

            req = OrderRequest(
                symbol=order.asset,
                side=order.side,
                qty=order.qty,
                order_type=order.order_type,
                price=order.price,
                reduce_only=order.reduce_only,
                strategy_id=order.strategy_id,
            )
            try:
                result = self._exchange.place_order(req)
            except Exception as e:
                logger.error("ExecutionEngine submit error %s %s: %r", order.asset, order.side, e)
                continue
            if result.status not in ("accepted", "filled"):
                logger.warning("ExecutionEngine order rejected : %s %s %s", order.asset, order.side, result.status)
                continue

            # Limit bandit resté au book (accepted + oid) → pending, pas de Fill.
            if arm != "taker_now" and result.status == "accepted" and result.order_id:
                from execution.bandit_policy import TIMEOUT_S
                self._pending.append({
                    "oid": str(result.order_id), "order": order,
                    "limit_px": float(order.price), "arm": arm,
                    "deadline": time.time() + TIMEOUT_S,
                })
                logger.info("ExecutionEngine bandit %s %s: limit %s @%.6g pending oid=%s",
                            order.asset, order.side, arm, order.price, result.order_id)
                continue

            maker = (order.order_type == "limit" and result.status == "accepted")
            fills.append(self._build_fill(order, result.price, result.order_id,
                                          maker=maker))
        return fills

    def poll_pending(self) -> list[Fill]:
        """Résout les limits bandit en attente. À appeler à chaque tick.

        - oid absent des open orders → considéré fill au prix limit (maker)
        - deadline dépassée → cancel ; si cancel OK → fallback market (taker).
          Si cancel KO (course : peut-être déjà fill), on garde l'entrée — le
          prochain poll tranchera via open orders. Jamais de double exécution.
        """
        if not self._pending:
            return []
        try:
            open_oids = {str(o.get("oid")) for o in self._exchange.get_open_orders()}
        except Exception as e:
            logger.warning("poll_pending: open_orders error %r — report au prochain poll", e)
            return []
        fills: list[Fill] = []
        now = time.time()
        still: list[dict] = []
        for p in self._pending:
            if p["oid"] not in open_oids:
                logger.info("ExecutionEngine bandit %s: limit %s fill @%.6g (oid=%s)",
                            p["order"].asset, p["arm"], p["limit_px"], p["oid"])
                fills.append(self._build_fill(p["order"], p["limit_px"], p["oid"], maker=True))
            elif now >= p["deadline"]:
                try:
                    res = self._exchange.cancel_order(p["oid"])
                    ok = bool(getattr(res, "success", False))
                except Exception as e:
                    logger.warning("poll_pending cancel %s: %r", p["oid"], e)
                    ok = False
                if ok:
                    logger.info("ExecutionEngine bandit %s: timeout %s → fallback market",
                                p["order"].asset, p["arm"])
                    fb = dataclasses.replace(p["order"], order_type="market", price=None)
                    fills.extend(self.submit([fb], use_bandit=False))
                else:
                    still.append(p)   # cancel raté (fill probable) → re-check
            else:
                still.append(p)
        self._pending = still
        return fills

    def _build_fill(self, order: OrderImpl, result_price, oid, maker: bool) -> Fill:
        import datetime as dt
        # Au moment du fill, on n'a pas toujours le price exact côté result :
        # result.price si présent, sinon get_mark_price, sinon le px de l'ordre.
        fill_price = result_price if result_price else self._get_price(order.asset)
        if fill_price is None or fill_price <= 0:
            fill_price = order.price if order.price else 0.0
        notional_signed = order.qty * fill_price * (1.0 if order.side == "buy" else -1.0)
        fee_bps = FEE_MAKER_BPS if maker else FEE_TAKER_BPS
        fee_estimate = abs(notional_signed) * fee_bps / 10_000.0
        # OID synthétique si HL ne retourne pas d'oid resting (cas marketable
        # immediate fill : result.status="filled" + order_id=""). Fill exige
        # un order_id non-vide (cf. core/types.py:Fill.__post_init__).
        oid = str(oid) if oid else (
            f"imm-{order.asset}-{int(dt.datetime.utcnow().timestamp() * 1000)}"
        )
        return Fill(
            order_id=oid,
            asset=order.asset,
            notional=notional_signed,
            price=float(fill_price),
            fee=fee_estimate,
            strategy_id=order.strategy_id,
            timestamp=dt.datetime.utcnow(),
        )

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _get_price(self, asset: str) -> Optional[float]:
        if self._prices_callback is not None:
            try:
                p = self._prices_callback(asset)
                if p and p > 0:
                    return float(p)
            except Exception:
                pass
        try:
            p = self._exchange.get_mark_price(asset)
            if p and p > 0:
                return float(p)
        except Exception:
            pass
        return None

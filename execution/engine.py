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

import logging
from typing import Dict, Optional

from core.config import ExecutionConfig
from core.types import Fill, TargetPortfolio
from execution.order import OrderImpl
from execution.portfolio import PortfolioImpl
from execution.types import ExchangeClient, OrderRequest

logger = logging.getLogger("v7.execution")


class ExecutionEngine:
    """Implémente le Protocol ExecutionEngine."""

    # Fix #4 (port V6 5316822) : seuil dust. Une position résiduelle dont la
    # valeur en USD est sous ce seuil est traitée comme 0 dans reconcile.
    # Évite le blocage observé V6 (BTC short qty=1e-05 ≈ $0.74, HL rejette
    # le reduce_only sous min ~$10 → grid figée indéfiniment sur ce symbole).
    DUST_NOTIONAL_USD = 2.0

    def __init__(
        self,
        exchange: ExchangeClient,
        cfg: ExecutionConfig,
        prices_callback=None,
    ) -> None:
        """`prices_callback(asset) -> price` : utilisé pour convertir notional → qty
        si le prix mark courant n'est pas dans le target. Si None, on tente
        `exchange.get_mark_price(asset)`.
        """
        self._exchange = exchange
        self._cfg = cfg
        self._prices_callback = prices_callback

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

    def submit(self, orders: list[OrderImpl]) -> list[Fill]:
        """Envoie les ordres et retourne les fills correspondants.

        On crée un Fill à partir du résultat de place_order(). Pour un fill
        accepted (market HL est presque toujours filled), on utilise les
        attributs de la requête (price, qty signée par side).
        """
        fills: list[Fill] = []
        import datetime as dt
        for order in orders:
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
            # Construit le Fill — au moment du fill, on n'a pas toujours le price exact
            # côté result. On prend result.price si présent, sinon get_mark_price.
            fill_price = result.price if result.price else self._get_price(order.asset)
            if fill_price is None or fill_price <= 0:
                fill_price = order.price if order.price else 0.0
            notional_signed = order.qty * fill_price * (1.0 if order.side == "buy" else -1.0)
            fee_estimate = abs(notional_signed) * 4.5 / 10_000.0  # taker 0.045%
            # OID synthétique si HL ne retourne pas d'oid resting (cas marketable
            # immediate fill : result.status="filled" + order_id=""). Fill exige
            # un order_id non-vide (cf. core/types.py:Fill.__post_init__).
            oid = str(result.order_id) if result.order_id else (
                f"imm-{order.asset}-{int(dt.datetime.utcnow().timestamp() * 1000)}"
            )
            fills.append(Fill(
                order_id=oid,
                asset=order.asset,
                notional=notional_signed,
                price=float(fill_price),
                fee=fee_estimate,
                strategy_id=order.strategy_id,
                timestamp=dt.datetime.utcnow(),
            ))
        return fills

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

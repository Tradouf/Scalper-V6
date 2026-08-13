"""
Gestion du risque — SPEC §6.

Tout est pur : ces fonctions reçoivent l'équity, l'état des garde-fous et
l'horloge, et rendent des nombres ou des verdicts. La persistance est dans
`state.py`, l'exécution ailleurs.

Le §6.5 est le cœur du module. Le diagnostic de départ (§1) n'est pas « la
stratégie perd », c'est « les frais représentent 64 % des pertes nettes ». Un
filtre d'entrée plus fin n'y répond qu'à moitié : ce qui répond vraiment, c'est
un plafond dur au nombre de trades, des cooldowns qui survivent au restart, un
seuil d'edge minimal exprimé en multiples de frais, et un kill-switch qui coupe
quand le ratio dérape malgré tout.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from confluence.config import RiskConfig
from confluence.state import ClosedTrade, GuardState, hours_since
from confluence.types import Side


@dataclass(frozen=True)
class SizeResult:
    size: float                    # en unités du sous-jacent (BTC)
    notional: float                # en USD
    risk_usd: float                # perte si le stop est touché
    capped_by: Optional[str]       # None | "leverage" | "max_position_usd"

    @property
    def valid(self) -> bool:
        return self.size > 0


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    reason: str
    detail: dict


class RiskManager:
    def __init__(self, cfg: RiskConfig) -> None:
        self.cfg = cfg

    # ── §6.2 Stop initial ───────────────────────────────────────────────────

    def stop_price(self, entry: float, side: Side, atr_1h: float) -> float:
        """`stop = entry ∓ k_stop * ATR_1h(14)`.

        Posé immédiatement au fill, en ordre trigger côté exchange (§6.2). Ce
        point n'est pas cosmétique : le V6 gère son trailing en logiciel seul
        (cf. CLAUDE.md, `_trail_loop`), donc un crash pendant une position
        ouverte la laisse nue. Ici le stop initial vit sur l'exchange.
        """
        if atr_1h <= 0:
            raise ValueError("ATR_1h doit être > 0 pour poser un stop")
        return entry - side.sign * self.cfg.k_stop * atr_1h

    # ── §6.1 Sizing ─────────────────────────────────────────────────────────

    def size(self, equity: float, entry: float, stop: float) -> SizeResult:
        """`size = (equity * risk_pct) / |entry - stop|`, plafonnée par le
        levier et le notionnel max.

        Le sizing part du RISQUE, pas du notionnel : c'est ce qui fait qu'un
        stop large donne mécaniquement une petite position. Les plafonds ne
        sont là que pour empêcher un ATR minuscule de produire un levier
        absurde — et quand ils mordent, on le dit dans `capped_by` plutôt que
        de rogner en silence.
        """
        distance = abs(entry - stop)
        if equity <= 0 or entry <= 0 or distance <= 0:
            return SizeResult(0.0, 0.0, 0.0, None)

        risk_usd = equity * self.cfg.risk_pct
        size = risk_usd / distance
        notional = size * entry
        capped_by = None

        max_notional = min(equity * self.cfg.max_leverage, self.cfg.max_position_usd)
        if notional > max_notional:
            capped_by = ("leverage" if equity * self.cfg.max_leverage <= self.cfg.max_position_usd
                         else "max_position_usd")
            notional = max_notional
            size = notional / entry
            risk_usd = size * distance

        return SizeResult(size=size, notional=notional, risk_usd=risk_usd, capped_by=capped_by)

    # ── §6.5 Filtre d'edge minimal ──────────────────────────────────────────

    def edge_ok(self, entry: float, atr_1h: float) -> Tuple[bool, dict]:
        """`k_edge * ATR_1h ≥ fee_roundtrip * edge_multiple` (§6.5).

        Les deux membres sont en PRIX : le mouvement espéré face au coût
        aller-retour du même notionnel. Exiger 5× les frais paraît brutal ;
        c'est le seul réglage qui attaque directement le diagnostic du §1.
        """
        expected_move = self.cfg.k_edge * atr_1h
        fee_cost = entry * self.cfg.fee_roundtrip
        required = fee_cost * self.cfg.edge_multiple
        detail = {
            "expected_move": expected_move,
            "fee_roundtrip_price": fee_cost,
            "required_move": required,
            "edge_ratio": (expected_move / fee_cost) if fee_cost > 0 else float("inf"),
        }
        return expected_move >= required, detail

    # ── §6.5 Kill-switch frais ──────────────────────────────────────────────

    def fee_ratio(self, history: List[ClosedTrade], now_ms: int) -> Tuple[Optional[float], dict]:
        """`fees_paid / gross_pnl_abs` sur la fenêtre glissante (défaut 30 j).

        `gross_pnl_abs` est la somme des |PnL BRUTS| : c'est la « quantité de
        mouvement » que la stratégie a su capter. Le ratio répond donc à « quelle
        part de ce que je capte part en frais ». Prendre le PnL net au
        dénominateur donnerait un ratio qui s'améliore quand on perd davantage.

        Rend `(None, …)` si la fenêtre est vide : pas de données, pas de verdict.
        """
        cutoff = now_ms - self.cfg.fee_killswitch_days * 86_400_000
        window = [t for t in history if t.closed_ms >= cutoff]
        fees = sum(t.fees for t in window)
        gross_abs = sum(abs(t.gross_pnl) for t in window)
        detail = {"fees_paid": fees, "gross_pnl_abs": gross_abs, "trades": len(window)}
        if not window:
            return None, detail
        if gross_abs <= 0:
            # Des frais payés pour zéro mouvement capté : ratio infini. C'est le
            # pire cas possible, il déclenche.
            return (float("inf") if fees > 0 else 0.0), detail
        return fees / gross_abs, detail

    def killswitch_triggered(self, history: List[ClosedTrade], now_ms: int) -> Tuple[bool, dict]:
        ratio, detail = self.fee_ratio(history, now_ms)
        detail["fee_ratio"] = ratio
        detail["threshold"] = self.cfg.fee_killswitch_ratio
        if ratio is None:
            return False, detail
        return ratio > self.cfg.fee_killswitch_ratio, detail

    # ── §6.5 Garde-fous anti-overtrading ────────────────────────────────────

    def check_guards(self, guards: GuardState, now_ms: int,
                     bar_ts: Optional[int] = None) -> GuardResult:
        """Tous les garde-fous du §6.5, dans l'ordre du moins au plus spécifique.

        `bar_ts` sert l'idempotence (§8) : si une entrée a déjà été prise sur
        cette bougie 15m, rejouer la bougie ne doit pas en produire une seconde.
        """
        guards.roll_day(now_ms)
        detail = {
            "trades_today": guards.trades_today,
            "max_trades_per_day": self.cfg.max_trades_per_day,
            "hours_since_entry": hours_since(now_ms, guards.last_entry_ms),
            "hours_since_loss": hours_since(now_ms, guards.last_loss_ms),
        }

        triggered, ks_detail = self.killswitch_triggered(guards.history, now_ms)
        detail.update(ks_detail)
        if triggered:
            return GuardResult(False, (
                f"kill-switch frais: {ks_detail['fee_ratio']:.1%} des gains bruts "
                f"partent en frais sur {self.cfg.fee_killswitch_days} j "
                f"(> {self.cfg.fee_killswitch_ratio:.0%}) — mode observation"
            ), detail)

        if bar_ts is not None and bar_ts in guards.seen_entry_bars:
            return GuardResult(False, f"entrée déjà prise sur la bougie {bar_ts}", detail)

        if guards.trades_today >= self.cfg.max_trades_per_day:
            return GuardResult(False, (
                f"plafond journalier atteint: {guards.trades_today}"
                f"/{self.cfg.max_trades_per_day} trades"
            ), detail)

        since_loss = detail["hours_since_loss"]
        if since_loss < self.cfg.cooldown_after_loss_h:
            return GuardResult(False, (
                f"cooldown après perte: {since_loss:.1f}h / "
                f"{self.cfg.cooldown_after_loss_h:g}h"
            ), detail)

        since_entry = detail["hours_since_entry"]
        if since_entry < self.cfg.cooldown_after_trade_h:
            return GuardResult(False, (
                f"cooldown entre trades: {since_entry:.1f}h / "
                f"{self.cfg.cooldown_after_trade_h:g}h"
            ), detail)

        return GuardResult(True, "garde-fous OK", detail)

    # ── Mise à jour des compteurs ───────────────────────────────────────────

    def register_entry(self, guards: GuardState, now_ms: int,
                       bar_ts: Optional[int] = None) -> None:
        guards.roll_day(now_ms)
        guards.trades_today += 1
        guards.last_entry_ms = now_ms
        if bar_ts is not None and bar_ts not in guards.seen_entry_bars:
            guards.seen_entry_bars.append(bar_ts)

    def register_exit(self, guards: GuardState, trade: ClosedTrade) -> None:
        guards.history.append(trade)
        if trade.net_pnl < 0:
            # Le cooldown du §6.5 vise « après un stop touché ». On l'applique à
            # toute sortie NETTE perdante : une sortie à -0,1 % dont les frais
            # font la perte doit déclencher la pause autant qu'un stop franc.
            guards.last_loss_ms = trade.closed_ms
        guards.prune(trade.closed_ms, self.cfg.fee_killswitch_days)


__all__ = ["GuardResult", "RiskManager", "SizeResult"]

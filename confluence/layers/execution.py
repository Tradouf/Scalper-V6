"""
Couche 1m — Exécution seule. SPEC §4.4 et §11.

Le 1m n'émet AUCUN signal et ne porte AUCUN indicateur de décision : il place
l'ordre décidé plus haut, et il refuse de le placer si le marché est
momentanément impraticable. C'est tout.

Deux règles non négociables, inscrites dans les types plutôt que dans la
discipline :

* **Maker uniquement.** Le prix coté est systématiquement borné du bon côté du
  carnet, donc un post-only ne peut pas être rejeté pour cause de croisement.
  Il n'existe aucun chemin de code menant à un ordre au marché à l'entrée
  (§11) : en fin de course, `ExecutionPlan` ABANDONNE le signal.
* **Abandon, pas dégradation.** Après `max_requotes` re-cotations infructueuses
  le signal est jeté. Un signal qu'on n'a pas réussi à prendre en maker était
  un signal dont le marché s'éloignait ; le prendre en taker, c'est payer 3×
  les frais pour entrer moins bien — exactement le mécanisme qui a fait 64 %
  des pertes du V7.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

from confluence.config import ExecutionConfig
from confluence.indicators import atr
from confluence.layers.context import Candle, LayerContext
from confluence.types import LayerVerdict, Side, ok, utc, veto


class ExecutionOutcome(Enum):
    PENDING = "pending"
    FILLED = "filled"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class ExecutionAction:
    """Ce que l'appelant doit faire, maintenant. Aucune I/O ici : le plan dit,
    l'exécutant fait."""

    kind: str                      # post | wait | requote | filled | abandon
    price: Optional[float]
    reason: str


class ExecutionLayer:
    """Sanity checks pré-ordre (§4.4). Pur : aucune I/O."""

    name = "1m"

    def __init__(self, cfg: ExecutionConfig) -> None:
        self.cfg = cfg

    def evaluate(self, candles: List[Candle], context: LayerContext) -> LayerVerdict:
        """`candles` = bougies 1m CLÔTURÉES."""
        cfg = self.cfg
        need = max(cfg.anomaly_lookback_bars, 15) + 15
        if len(candles) < need:
            at = utc(int(candles[-1]["ts"])) if candles else context.now
            return veto(f"warmup insuffisant: {len(candles)}/{need} bougies 1m", at)

        at = utc(int(candles[-1]["ts"]))
        bid, ask = context.best_bid, context.best_ask
        base = {"best_bid": bid, "best_ask": ask}

        if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
            return veto("carnet indisponible ou incohérent", at, **base)

        mid = (bid + ask) / 2.0
        spread_bps = (ask - bid) / mid * 10_000.0
        base["spread_bps"] = spread_bps
        if spread_bps >= cfg.max_spread_bps:
            return veto(
                f"spread {spread_bps:.2f} bps ≥ {cfg.max_spread_bps:g} bps", at, **base)

        atr_1m = atr(candles, 14)[-1]
        base["atr_1m"] = atr_1m
        if atr_1m is None or atr_1m <= 0:
            return veto("ATR 1m non calculable", at, **base)

        recent = candles[-cfg.anomaly_lookback_bars:]
        worst = max(float(c["high"]) - float(c["low"]) for c in recent)
        base["max_range_1m"] = worst
        if worst > cfg.anomaly_atr_mult * atr_1m:
            return veto(
                f"bougie 1m anormale dans les {cfg.anomaly_lookback_bars} dernières "
                f"minutes: amplitude {worst:.2f} > {cfg.anomaly_atr_mult:g}×ATR_1m "
                f"({atr_1m:.2f})", at, **base)

        return ok(f"carnet sain (spread {spread_bps:.2f} bps)", at, **base)


@dataclass
class ExecutionPlan:
    """Machine à états d'une entrée maker : cotation, timeout, re-cotations,
    abandon. Pure — le temps et le carnet sont injectés à chaque `step()`, ce
    qui la rend rejouable à l'identique en backtest."""

    side: Side
    entry_zone: Tuple[float, float]
    cfg: ExecutionConfig
    attempts: int = 0
    quoted_price: Optional[float] = None
    quoted_at_ms: int = 0
    status: ExecutionOutcome = ExecutionOutcome.PENDING
    abandon_reason: str = ""
    history: List[Tuple[int, float]] = field(default_factory=list)

    def limit_price(self, best_bid: float, best_ask: float) -> float:
        """Prix limite post-only, garanti non croisant et dans la zone d'entrée.

        Trois bornes successives, dans cet ordre :
          1. le meilleur bid/ask décalé de `tick_offset` ticks vers l'intérieur
             (meilleure place dans la file) ;
          2. un cran en deçà du côté opposé, pour qu'un post-only ne soit
             JAMAIS rejeté pour croisement — c'est la borne qui garantit §11 ;
          3. la zone d'entrée du signal, qu'on ne dépasse pas même si le carnet
             s'est déplacé : au-delà, la thèse du signal ne tient plus.
        """
        tick = self.cfg.tick_size
        offset = self.cfg.tick_offset * tick
        if self.side is Side.LONG:
            price = best_bid + offset
            price = min(price, best_ask - tick)     # jamais croisant
            price = min(price, self.entry_zone[1])  # jamais au-dessus de la zone
        else:
            price = best_ask - offset
            price = max(price, best_bid + tick)
            price = max(price, self.entry_zone[0])
        return round(price / tick) * tick

    def step(self, now_ms: int, best_bid: float, best_ask: float,
             filled: bool = False) -> ExecutionAction:
        if self.status is not ExecutionOutcome.PENDING:
            return ExecutionAction(self.status.value, self.quoted_price, self.abandon_reason)

        if filled:
            self.status = ExecutionOutcome.FILLED
            return ExecutionAction("filled", self.quoted_price, "exécuté en maker")

        if self.quoted_price is None:
            return self._quote(now_ms, best_bid, best_ask, "post", "cotation initiale")

        elapsed_s = (now_ms - self.quoted_at_ms) / 1000.0
        if elapsed_s < self.cfg.fill_timeout_s:
            return ExecutionAction("wait", self.quoted_price,
                                   f"en attente ({elapsed_s:.0f}s / {self.cfg.fill_timeout_s:g}s)")

        # `attempts` compte la cotation initiale ; on autorise `max_requotes`
        # re-cotations EN PLUS, puis on abandonne — jamais de bascule taker.
        if self.attempts > self.cfg.max_requotes:
            self.status = ExecutionOutcome.ABANDONED
            self.abandon_reason = (
                f"non exécuté après {self.cfg.max_requotes} re-cotations "
                f"— signal abandonné (pas de bascule taker, §11)")
            return ExecutionAction("abandon", None, self.abandon_reason)

        return self._quote(now_ms, best_bid, best_ask, "requote",
                           f"re-cotation {self.attempts}/{self.cfg.max_requotes}")

    def _quote(self, now_ms: int, best_bid: float, best_ask: float,
               kind: str, reason: str) -> ExecutionAction:
        self.quoted_price = self.limit_price(best_bid, best_ask)
        self.quoted_at_ms = now_ms
        self.attempts += 1
        self.history.append((now_ms, self.quoted_price))
        return ExecutionAction(kind, self.quoted_price, reason)

    def would_fill(self, low: float, high: float) -> bool:
        """Modèle de fill maker pour le backtest : un ordre limite passif est
        exécuté si le prix VIENT LE CHERCHER (le marché traverse la limite).

        C'est volontairement conservateur — on exige une traversée stricte, pas
        un simple contact —, parce qu'un modèle de fill optimiste est la
        deuxième façon la plus courante de fabriquer un backtest flatteur,
        juste après le repaint.
        """
        if self.quoted_price is None:
            return False
        if self.side is Side.LONG:
            return low < self.quoted_price
        return high > self.quoted_price


__all__ = ["ExecutionAction", "ExecutionLayer", "ExecutionOutcome", "ExecutionPlan"]

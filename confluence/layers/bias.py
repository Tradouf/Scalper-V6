"""
Couche 1d — Biais directionnel. SPEC §4.1.

Rôle : dire dans quel sens on a le DROIT de trader. Cette couche ne déclenche
jamais d'entrée.

    close > EMA_100 ET SMA_50 > SMA_200   -> LONG_ONLY
    close < EMA_100 ET SMA_50 < SMA_200   -> SHORT_ONLY
    sinon                                 -> FLAT

Deux mécanismes s'ajoutent à cette règle nue :

* **Hystérésis** (2 clôtures daily consécutives) : sans elle, un prix qui
  oscille autour de l'EMA_100 fait basculer le biais tous les jours, et un
  biais qui clignote autorise successivement les deux sens — c'est-à-dire
  n'autorise plus rien. C'est de l'over-trading déguisé en filtre.
* **Veto macro** : `risk_level == EXTREME` force FLAT. Le veto porte sur la
  SORTIE, pas sur l'état interne : quand la macro se détend, le biais reprend
  là où il en était plutôt que de devoir se reconstruire en 2 clôtures.
"""

from __future__ import annotations

from dataclasses import replace
from typing import List, Optional, Sequence, Tuple

from confluence.config import BiasConfig
from confluence.indicators import ema, sma
from confluence.layers.context import Candle, LayerContext
from confluence.state import BiasState
from confluence.types import Bias, LayerVerdict, RiskLevel, ok, utc, veto


class BiasLayer:
    """Filtre veto du haut de la cascade. Pur : aucune I/O."""

    name = "1d"

    def __init__(self, cfg: BiasConfig) -> None:
        self.cfg = cfg

    # -- règle nue -----------------------------------------------------------

    def raw_bias(self, closes: Sequence[float]) -> Tuple[Bias, dict]:
        """Biais brut sur la dernière clôture daily, sans hystérésis ni macro.

        Rend aussi les valeurs d'indicateurs, qui partent dans le log : quand
        le bot ne trade pas pendant trois semaines, c'est ce qui permet de
        vérifier que c'est bien parce que SMA_50 < SMA_200 et pas à cause d'un
        bug.
        """
        e = ema(closes, self.cfg.ema)[-1]
        fast = sma(closes, self.cfg.sma_fast)[-1]
        slow = sma(closes, self.cfg.sma_slow)[-1]
        detail = {"close": closes[-1], "ema": e, "sma_fast": fast, "sma_slow": slow}
        if e is None or fast is None or slow is None:
            return Bias.FLAT, detail
        close = closes[-1]
        if close > e and fast > slow:
            return Bias.LONG_ONLY, detail
        if close < e and fast < slow:
            return Bias.SHORT_ONLY, detail
        return Bias.FLAT, detail

    # -- hystérésis ----------------------------------------------------------

    def advance(self, state: BiasState, raw: Bias, bar_ts: int) -> BiasState:
        """Fait avancer l'hystérésis d'exactement une clôture daily.

        Idempotent (§8) : si `bar_ts` a déjà été consommé — ou est antérieur au
        dernier consommé —, l'état est rendu inchangé. Sans ce garde, un
        redémarrage du bot au milieu d'une journée relirait la même bougie et
        confirmerait un basculement en une seule clôture réelle.
        """
        if bar_ts <= state.last_bar_ts:
            return replace(state)

        new = replace(state, last_bar_ts=bar_ts)
        if raw == state.current:
            new.pending, new.pending_count = None, 0
            return new

        if raw == state.pending:
            new.pending_count = state.pending_count + 1
        else:
            new.pending, new.pending_count = raw, 1

        if new.pending_count >= self.cfg.confirm_closes:
            new.current, new.pending, new.pending_count = raw, None, 0
        return new

    # -- contrat §8 ----------------------------------------------------------

    def evaluate(self, candles: List[Candle], context: LayerContext) -> LayerVerdict:
        need = self.cfg.warmup_bars
        if len(candles) < need:
            at = utc(candles[-1]["ts"]) if candles else context.now
            return veto(
                f"warmup insuffisant: {len(candles)}/{need} bougies daily",
                at, bias=Bias.FLAT, bias_state=context.bias_state,
            )

        closes = [float(c["close"]) for c in candles]
        bar_ts = int(candles[-1]["ts"])
        at = utc(bar_ts)

        raw, detail = self.raw_bias(closes)
        state = self.advance(context.bias_state, raw, bar_ts)

        # Le veto macro s'applique à la sortie, pas à l'état interne.
        macro_forced = (context.macro_risk is RiskLevel.EXTREME)
        effective = Bias.FLAT if macro_forced else state.current

        payload = dict(
            detail,
            bias=effective,
            raw_bias=raw,
            confirmed_bias=state.current,
            pending=state.pending,
            pending_count=state.pending_count,
            bias_state=state,
            macro_risk=context.macro_risk,
        )

        if macro_forced:
            return veto("veto macro: risk_level=EXTREME ⇒ FLAT", at, **payload)
        if effective is Bias.FLAT:
            if state.pending is not None:
                reason = (f"biais FLAT (bascule {state.pending.name} en attente, "
                          f"{state.pending_count}/{self.cfg.confirm_closes} clôtures)")
            else:
                reason = "biais FLAT: close et SMA non alignées"
            return veto(reason, at, **payload)

        pend = ""
        if state.pending is not None:
            pend = (f", bascule {state.pending.name} en cours "
                    f"{state.pending_count}/{self.cfg.confirm_closes}")
        return ok(f"biais {effective.name} confirmé{pend}", at, **payload)


def bias_side(bias: Bias) -> Optional[int]:
    """Sens autorisé par un biais, ou None si FLAT."""
    return None if bias is Bias.FLAT else bias.value


__all__ = ["BiasLayer", "bias_side"]

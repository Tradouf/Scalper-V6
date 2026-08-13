"""
Couche 15m — Timing d'entrée. SPEC §4.3.

Cette couche ne décide PAS de la direction : elle la reçoit de la couche 1h et
se contente de dire « c'est le moment » ou « pas encore ». Deux modes, choisis
par le régime 1h et mutuellement exclusifs.

**Mode TREND — pullback-continuation**

1. *Setup* : le prix est revenu toucher l'EMA_20(15m) ou le VWAP de session,
   dans le sens de la tendance 1h.
2. *Trigger* : une bougie 15m clôture en repartant dans le sens de la
   tendance, AVEC expansion du Bollinger Band Width (BBW > sa SMA_20). La
   condition d'expansion est ce qui empêche d'entrer dans une compression :
   entrer dans un resserrement de volatilité, c'est payer les frais pour
   attendre que quelque chose se passe.
3. *Invalidation* : si le repli dépasse l'EMA_50(15m), le setup est annulé —
   ce n'est plus un pullback, c'est un retournement.

**Mode RANGE** — la main passe au MeanReversionAgent, sous la contrainte du
biais 1d.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from confluence.config import TimingConfig
from confluence.indicators import (
    INTERVAL_MS,
    bollinger_width,
    ema,
    session_vwap,
    sma_of_optional,
)
from confluence.layers.context import Candle, LayerContext
from confluence.types import LayerVerdict, Regime, Side, ok, utc, veto

BAR_MS = INTERVAL_MS["15m"]


class TimingLayer:
    name = "15m"

    def __init__(self, cfg: TimingConfig, meanrev=None) -> None:
        self.cfg = cfg
        self.meanrev = meanrev          # MeanReversionAgent | None (injecté)

    # -- contrat §8 ----------------------------------------------------------

    def evaluate(self, candles: List[Candle], context: LayerContext) -> LayerVerdict:
        need = self.cfg.warmup_bars
        if len(candles) < need:
            at = utc(int(candles[-1]["ts"])) if candles else context.now
            return veto(f"warmup insuffisant: {len(candles)}/{need} bougies 15m", at)

        at = utc(int(candles[-1]["ts"]))
        if context.regime is Regime.RANGE:
            return self._evaluate_range(candles, context, at)
        if context.regime is Regime.TREND:
            return self._evaluate_trend(candles, context, at)
        return veto(f"régime {context.regime} sans mode de timing", at)

    # -- mode TREND ----------------------------------------------------------

    def _evaluate_trend(self, candles: List[Candle], context: LayerContext, at) -> LayerVerdict:
        side = context.direction
        if side is None:
            return veto("direction 1h absente", at)

        cfg = self.cfg
        closes = [float(c["close"]) for c in candles]
        ema_pull = ema(closes, cfg.ema_pullback)
        ema_inval = ema(closes, cfg.ema_invalidation)
        vwap = session_vwap(candles)
        bbw = bollinger_width(closes, cfg.bbw_sma, cfg.bbw_std)
        bbw_ref = sma_of_optional(bbw, cfg.bbw_sma)

        i = len(candles) - 1
        bar = candles[i]
        if ema_pull[i] is None or ema_inval[i] is None or bbw[i] is None or bbw_ref[i] is None:
            return veto("indicateurs 15m non amorcés", at)

        base = {
            "mode": "trend",
            "side": side,
            "ema_pullback": ema_pull[i],
            "ema_invalidation": ema_inval[i],
            "vwap": vwap[i],
            "bbw": bbw[i],
            "bbw_sma": bbw_ref[i],
        }

        # 1. Setup : le pullback a-t-il eu lieu dans la fenêtre récente ?
        touch = self._find_pullback(candles, ema_pull, vwap, side, i)
        base["pullback_bar"] = None if touch is None else int(candles[touch]["ts"])
        if touch is None:
            return veto(
                f"pas de pullback vers EMA_{cfg.ema_pullback}/VWAP sur les "
                f"{cfg.setup_lookback_bars} dernières bougies 15m", at, **base)

        # 3. Invalidation : le repli a-t-il dépassé l'EMA_50 depuis le touch ?
        broken = self._invalidated(candles, ema_inval, side, touch, i)
        if broken is not None:
            base["invalidation_bar"] = int(candles[broken]["ts"])
            return veto(
                f"setup annulé: le repli a dépassé l'EMA_{cfg.ema_invalidation} "
                f"à {utc(int(candles[broken]['ts'])).isoformat()}", at, **base)

        # 2. Trigger : reprise dans le sens, avec expansion du BBW.
        if bbw[i] <= bbw_ref[i]:
            return veto(
                f"compression: BBW={bbw[i]:.4f} ≤ SMA_{cfg.bbw_sma}={bbw_ref[i]:.4f} "
                f"— on n'entre pas dans un resserrement", at, **base)

        o, c = float(bar["open"]), float(bar["close"])
        resumed = (c > o and c > ema_pull[i]) if side is Side.LONG else (c < o and c < ema_pull[i])
        base["resumed"] = resumed
        if not resumed:
            return veto(
                f"pas de reprise {side.name} sur la bougie de déclenchement "
                f"(close={c:g}, open={o:g}, EMA_{cfg.ema_pullback}={ema_pull[i]:g})",
                at, **base)

        zone = self.entry_zone(c, side, context.atr_1h)
        base.update(entry_ref=c, entry_zone=zone,
                    expires_at=self.expiry(int(bar["ts"])), bar_ts=int(bar["ts"]))
        return ok(f"trigger {side.name} après pullback, BBW en expansion", at, **base)

    def _find_pullback(self, candles, ema_pull, vwap, side: Side, i: int) -> Optional[int]:
        """Index de la bougie la plus récente ayant touché la zone de pullback.

        La zone est « EMA_20 ou VWAP » : pour un long on retient le PLUS HAUT
        des deux, puisque c'est celui que le prix touche en premier en
        redescendant (et symétriquement pour un short).
        """
        start = max(0, i - self.cfg.setup_lookback_bars + 1)
        for j in range(i, start - 1, -1):
            ref = self._pullback_ref(ema_pull[j], vwap[j], side)
            if ref is None:
                continue
            if side is Side.LONG and float(candles[j]["low"]) <= ref:
                return j
            if side is Side.SHORT and float(candles[j]["high"]) >= ref:
                return j
        return None

    @staticmethod
    def _pullback_ref(ema_value, vwap_value, side: Side) -> Optional[float]:
        refs = [v for v in (ema_value, vwap_value) if v is not None]
        if not refs:
            return None
        return max(refs) if side is Side.LONG else min(refs)

    def _invalidated(self, candles, ema_inval, side: Side, start: int, i: int) -> Optional[int]:
        """Index de la bougie qui a invalidé le setup, ou None.

        `invalidation_use_wick` arbitre entre juger sur la mèche (strict : une
        seule extension tue le setup) ou sur la clôture (défaut : une mèche
        isolée sous l'EMA_50 est du bruit d'exécution, pas un retournement).
        """
        for j in range(start, i + 1):
            level = ema_inval[j]
            if level is None:
                continue
            if self.cfg.invalidation_use_wick:
                price = float(candles[j]["low"]) if side is Side.LONG else float(candles[j]["high"])
            else:
                price = float(candles[j]["close"])
            if (side is Side.LONG and price < level) or (side is Side.SHORT and price > level):
                return j
        return None

    # -- mode RANGE ----------------------------------------------------------

    def _evaluate_range(self, candles: List[Candle], context: LayerContext, at) -> LayerVerdict:
        if self.meanrev is None:
            return veto("régime RANGE mais aucun MeanReversionAgent branché", at, mode="range")

        verdict = self.meanrev.evaluate(context.series("1h"), context)
        data = dict(verdict.data, mode="range")
        if not verdict.passed:
            return veto(f"RANGE: {verdict.reason}", at, **data)

        side = data.get("side")
        entry_ref = float(data.get("entry_ref", candles[-1]["close"]))
        zone = self.entry_zone(entry_ref, side, context.atr_1h)
        data.update(entry_zone=zone, entry_ref=entry_ref,
                    expires_at=self.expiry(int(candles[-1]["ts"])),
                    bar_ts=int(candles[-1]["ts"]))
        return ok(f"RANGE: {verdict.reason}", at, **data)

    # -- utilitaires ---------------------------------------------------------

    def entry_zone(self, ref: float, side: Side, atr_1h: Optional[float]) -> Tuple[float, float]:
        """Zone limite maker autour du prix de référence.

        On place TOUJOURS la limite du bon côté (sous le marché pour un achat) :
        c'est ce qui rend le post-only du §4.4 réalisable. Une zone à cheval sur
        le prix courant produirait des ordres rejetés par le post-only.
        """
        width = (atr_1h or 0.0) * self.cfg.entry_zone_atr_frac
        if side is Side.LONG:
            return (ref - width, ref)
        return (ref, ref + width)

    def expiry(self, bar_ts: int):
        """Le signal meurt `signal_ttl_bars` bougies 15m après la CLÔTURE de sa
        bougie de déclenchement (§4.3, défaut 2)."""
        return utc(bar_ts + BAR_MS + self.cfg.signal_ttl_bars * BAR_MS)


__all__ = ["TimingLayer"]

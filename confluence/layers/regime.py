"""
Couche 1h — Régime et direction. SPEC §4.2.

Rôle : dire si le marché est dans un état exploitable, et dans quel sens.
Cette couche ne déclenche jamais d'entrée non plus.

Quatre filtres, tous à veto :

1. **ADX(14)** — TREND si > 25, RANGE si < 20, et entre les deux une **zone
   morte** où l'on ne trade pas. Cette zone morte est le cœur du dispositif
   anti-frais : c'est là que les indicateurs donnent des signaux qui se
   contredisent une bougie sur deux.
2. **Percentile d'ATR sur 90 jours** — trade autorisé dans [20, 90] seulement.
   Sous le 20e, le mouvement espéré ne couvre pas les frais ; au-dessus du 90e,
   les spreads se dégradent et le stop ATR devient énorme, donc la taille
   minuscule.
3. **Direction** — EMA_21 vs EMA_55, qui doit être COHÉRENTE avec le biais 1d.
4. **Funding** — si le côté qu'on veut prendre paie déjà plus que le seuil
   annualisé, on est du côté surchargé du marché.

En RANGE la direction n'est pas donnée par les EMA : c'est le
MeanReversionAgent qui prend la main (§4.3), sous la contrainte du biais 1d.
"""

from __future__ import annotations

from typing import List, Optional

from confluence.config import RegimeConfig
from confluence.indicators import adx, atr, ema, percentile_rank
from confluence.layers.context import Candle, LayerContext
from confluence.types import Bias, LayerVerdict, Regime, Side, ok, utc, veto

HOURS_PER_YEAR = 24 * 365


class RegimeLayer:
    name = "1h"

    def __init__(self, cfg: RegimeConfig) -> None:
        self.cfg = cfg

    def classify(self, adx_value: Optional[float]) -> Optional[Regime]:
        """ADX → régime. None si l'ADX n'est pas encore amorcé.

        La zone morte [adx_range, adx_trend] rend CHOP, pas « le régime
        précédent » : reconduire l'ancien régime dans la zone morte reviendrait
        à supprimer la zone morte.
        """
        if adx_value is None:
            return None
        if adx_value > self.cfg.adx_trend:
            return Regime.TREND
        if adx_value < self.cfg.adx_range:
            return Regime.RANGE
        return Regime.CHOP

    def funding_ok(self, funding_hourly: Optional[float], side: Side) -> tuple:
        """(ok, annualisé). Positif = les longs paient les shorts.

        On ne bloque que le côté QUI PAIE : un funding très positif est un vent
        contraire pour un long, mais un vent porteur pour un short.
        """
        if funding_hourly is None:
            return False, None
        annual = funding_hourly * HOURS_PER_YEAR
        limit = self.cfg.funding_max_annualized
        paying = annual if side is Side.LONG else -annual
        return paying <= limit, annual

    def evaluate(self, candles: List[Candle], context: LayerContext) -> LayerVerdict:
        need = self.cfg.warmup_bars
        if len(candles) < need:
            at = utc(candles[-1]["ts"]) if candles else context.now
            return veto(f"warmup insuffisant: {len(candles)}/{need} bougies 1h", at,
                        regime=None, direction=None)

        at = utc(int(candles[-1]["ts"]))
        closes = [float(c["close"]) for c in candles]

        adx_series = adx(candles, self.cfg.adx_period)
        atr_series = atr(candles, self.cfg.atr_period)
        adx_value = adx_series[-1]
        atr_value = atr_series[-1]
        fast = ema(closes, self.cfg.ema_fast)[-1]
        slow = ema(closes, self.cfg.ema_slow)[-1]

        base = {"adx": adx_value, "atr_1h": atr_value,
                "ema_fast": fast, "ema_slow": slow}

        regime = self.classify(adx_value)
        if regime is None or atr_value is None or fast is None or slow is None:
            return veto("indicateurs 1h non amorcés", at, regime=None, direction=None, **base)

        base["regime"] = regime

        # 1. Zone morte ADX.
        if regime is Regime.CHOP:
            return veto(
                f"régime CHOP: ADX={adx_value:.1f} dans la zone morte "
                f"[{self.cfg.adx_range:g}, {self.cfg.adx_trend:g}]",
                at, direction=None, **base)

        # 2. Percentile d'ATR — fenêtre glissante de 90 jours.
        window = [v for v in atr_series[-self.cfg.percentile_window_bars:] if v is not None]
        pct = percentile_rank(window, atr_value)
        base["atr_percentile"] = pct
        base["atr_window_bars"] = len(window)
        vol_ok = self.cfg.atr_percentile_min <= pct <= self.cfg.atr_percentile_max
        base["vol_ok"] = vol_ok
        if not vol_ok:
            # Le qualificatif est AVANT le deux-points : la distribution des
            # motifs (§9.3) coupe à la ponctuation, et « trop calme » vs « trop
            # volatil » sont deux diagnostics opposés qu'il ne faut pas fondre
            # dans une seule ligne de rapport.
            side_of = "trop calme" if pct < self.cfg.atr_percentile_min else "trop volatil"
            return veto(
                f"volatilité {side_of}: percentile ATR={pct:.1f} "
                f"hors [{self.cfg.atr_percentile_min:g}, {self.cfg.atr_percentile_max:g}]",
                at, direction=None, **base)

        # 3. Direction, et cohérence avec le biais 1d.
        if regime is Regime.TREND:
            direction = Side.LONG if fast > slow else Side.SHORT
        else:
            # RANGE : la direction vient du biais 1d, le MeanReversionAgent
            # décidera du point d'entrée (§4.3).
            direction = None if context.bias is Bias.FLAT else Side(context.bias.value)
        base["direction"] = direction

        if direction is None:
            return veto("RANGE sans biais directionnel 1d", at, **base)

        if context.bias is not Bias.FLAT and direction.value != context.bias.value:
            return veto(
                f"incohérence: direction 1h={direction.name} vs biais 1d={context.bias.name}",
                at, **base)

        # 4. Funding.
        f_ok, annual = self.funding_ok(context.funding_hourly, direction)
        base["funding_annualized"] = annual
        base["funding_ok"] = f_ok
        if annual is None:
            # Donnée absente ⇒ veto. Le §1 pose que le défaut est l'inaction ;
            # un filtre qu'on ne peut pas évaluer n'est pas un filtre passé.
            return veto("funding indisponible: filtre §4.2 non évaluable", at, **base)
        if not f_ok:
            return veto(
                f"funding défavorable: {annual:+.1%} annualisé côté {direction.name} "
                f"> {self.cfg.funding_max_annualized:.0%}",
                at, **base)

        return ok(
            f"régime {regime.value.upper()} {direction.name} "
            f"(ADX={adx_value:.1f}, ATR p{pct:.0f}, funding {annual:+.1%})",
            at, **base)


__all__ = ["RegimeLayer"]

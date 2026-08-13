"""
MeanReversionAgent — SPEC §2 et §4.3.

Cité par la spec comme existant ; il n'existait pas dans le repo, il est donc
écrit ici, en logique déterministe pure comme le reste du module.

Il n'est consulté que lorsque la couche 1h retourne **RANGE**. En TREND il est
suspendu (§2) — un mean-reverter lâché dans une tendance vend chaque nouveau
plus-haut, c'est le mode d'échec canonique de la famille.

Trois filtres avant tout signal, dans cet ordre :

1. **ADF** — la série doit rejeter la racine unitaire. Un z-score sur une série
   non stationnaire mesure la distance à une moyenne qui dérive : le « retour »
   qu'il annonce n'a aucune raison d'arriver.
2. **Demi-vie** — bornée. Trop courte, le retour est déjà consommé quand
   l'ordre maker se remplit ; trop longue, la position dort pendant des jours
   en payant du funding.
3. **Biais 1d** — en LONG_ONLY, seuls les longs depuis le bas du range sont
   autorisés (§4.3). C'est la contrainte que le ConfluenceAgent impose.

Un ADF ou une demi-vie qu'on ne peut pas calculer valent VETO, jamais
« ignorer le filtre » : le défaut du système est l'inaction (§1).
"""

from __future__ import annotations

from typing import List, Optional

from confluence.config import MeanRevConfig
from confluence.indicators import ADF_CRITICAL, adf_statistic, half_life, zscore
from confluence.layers.context import Candle, LayerContext
from confluence.types import Bias, LayerVerdict, Side, ok, utc, veto


class MeanReversionAgent:
    name = "meanrev"

    def __init__(self, cfg: MeanRevConfig) -> None:
        self.cfg = cfg

    def evaluate(self, candles: List[Candle], context: LayerContext) -> LayerVerdict:
        """`candles` = bougies 1h CLÔTURÉES. Pur, aucune I/O."""
        cfg = self.cfg
        need = cfg.zscore_period + cfg.adf_lags + 5
        if not cfg.enabled:
            at = utc(int(candles[-1]["ts"])) if candles else context.now
            return veto("MeanReversionAgent désactivé (meanrev.enabled=false)", at)
        if len(candles) < need:
            at = utc(int(candles[-1]["ts"])) if candles else context.now
            return veto(f"warmup insuffisant: {len(candles)}/{need} bougies 1h", at)

        at = utc(int(candles[-1]["ts"]))
        closes = [float(c["close"]) for c in candles]
        window = closes[-cfg.zscore_period:]

        z = zscore(closes, cfg.zscore_period)[-1]
        stat = adf_statistic(window, cfg.adf_lags)
        hl = half_life(window)
        critical = ADF_CRITICAL[cfg.adf_alpha]
        base = {"zscore": z, "adf_stat": stat, "adf_critical": critical, "half_life": hl}

        if z is None:
            return veto("z-score indéfini (écart-type nul sur la fenêtre)", at, **base)
        if stat is None:
            return veto("ADF non calculable sur la fenêtre", at, **base)
        if stat >= critical:
            return veto(
                f"série non stationnaire: ADF={stat:.2f} ≥ {critical:.2f} "
                f"(α={cfg.adf_alpha:g}) — le retour à la moyenne n'est pas établi",
                at, **base)
        if hl is None:
            return veto("demi-vie non calculable (pas de retour à la moyenne)", at, **base)
        if not (cfg.half_life_min_bars <= hl <= cfg.half_life_max_bars):
            return veto(
                f"demi-vie {hl:.1f} barres hors "
                f"[{cfg.half_life_min_bars:g}, {cfg.half_life_max_bars:g}]",
                at, **base)

        side = self._side_from_z(z)
        base["side"] = side
        if side is None:
            return veto(
                f"z-score {z:+.2f} dans la zone neutre (|z| < {cfg.entry_z:g})", at, **base)

        if context.bias is Bias.FLAT:
            return veto("biais 1d FLAT: aucun sens autorisé en RANGE", at, **base)
        if side.value != context.bias.value:
            return veto(
                f"sens {side.name} interdit par le biais 1d {context.bias.name}",
                at, **base)

        base["entry_ref"] = closes[-1]
        return ok(
            f"mean-reversion {side.name}: z={z:+.2f}, demi-vie {hl:.1f}b, ADF={stat:.2f}",
            at, **base)

    def _side_from_z(self, z: float) -> Optional[Side]:
        """Bas du range ⇒ LONG, haut du range ⇒ SHORT."""
        if z <= -self.cfg.entry_z:
            return Side.LONG
        if z >= self.cfg.entry_z:
            return Side.SHORT
        return None

    def should_exit(self, z: Optional[float], side: Side) -> bool:
        """Sortie mean-reversion : retour du z-score dans la zone neutre.

        Exposé pour le backtest et le live, mais la sortie NORMALE reste le
        TrailingStopAgent (§6.3) — celle-ci ne fait que fermer plus tôt quand
        la thèse est réalisée.
        """
        if z is None:
            return False
        return z >= -self.cfg.exit_z if side is Side.LONG else z <= self.cfg.exit_z


__all__ = ["MeanReversionAgent"]

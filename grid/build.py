"""
Construction de la grille — SPEC §3, et conditions d'activation §2.

Trois décisions structurent ce fichier, toutes tirées du §0 :

**Le plancher de frais est infranchissable (§3.2).** L'espacement est le
`max` entre une part d'ATR et `grid_edge_multiple × roundtrip_maker_bps`. Le
second terme ne dépend pas du marché : quelle que soit la volatilité, un cycle
de niveau rapporte au moins 5× son coût de frais. C'est la traduction directe
du diagnostic frais du projet, et c'est ce qui empêche la grille de devenir
« une machine à payer l'exchange ».

**Le sizing se déduit de la perte, pas l'inverse (§3.3).** On ne choisit pas une
taille puis on regarde le risque ; on fixe la perte maximale tolérable au point
de flatten, et la taille en découle. Une grille dimensionnée par le notionnel
disponible finit toujours par rencontrer la cassure qui la ruine.

**La grille est statique (§3.1).** Aucun réancrage automatique. Un réancrage
serait une grille qui suit le prix — c'est-à-dire une position à moyenne mobile
sans stop, exactement ce que le §6.1 interdit.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from grid.config import GridConfig
from grid.types import ActivationVerdict, GridLevel, Side

logger = logging.getLogger("sdm.grid.build")

Candle = Dict[str, float]


# ── §3.1 Détection du range ──────────────────────────────────────────────────

@dataclass(frozen=True)
class RangeSpec:
    lower: float
    upper: float
    center: float
    width: float

    @property
    def half_width(self) -> float:
        return self.width / 2.0


def detect_range(candles_15m: Sequence[Candle], lookback: int,
                 tick_size: float) -> Optional[RangeSpec]:
    """Bornes = percentiles 5/95 des clôtures 15m, arrondies VERS L'INTÉRIEUR.

    L'arrondi vers l'intérieur est délibéré : il rétrécit légèrement le range,
    donc déclenche la cassure un peu plus tôt. Sur une stratégie dont la perte
    maximale survient à la cassure, l'erreur doit pencher du côté prudent.

    Le centre est la MÉDIANE, pas le VWAP : le volume ne doit jamais entrer dans
    la décision (§3.1, aligné sur la spec ConfluenceAgent §3).
    """
    if len(candles_15m) < lookback:
        return None
    window = [float(c["close"]) for c in candles_15m[-lookback:]]
    ordered = sorted(window)
    n = len(ordered)

    lower_raw = ordered[max(0, int(0.05 * n) - 1)]
    upper_raw = ordered[min(n - 1, int(0.95 * n))]
    center = ordered[n // 2] if n % 2 else 0.5 * (ordered[n // 2 - 1] + ordered[n // 2])

    lower = math.ceil(lower_raw / tick_size) * tick_size     # vers l'intérieur
    upper = math.floor(upper_raw / tick_size) * tick_size
    if upper <= lower:
        return None
    return RangeSpec(lower=lower, upper=upper, center=center, width=upper - lower)


# ── §3.2 Espacement ──────────────────────────────────────────────────────────

def step_price(cfg: GridConfig, atr_1h: float, mid_price: float) -> float:
    """Espacement en PRIX, plancher de frais compris.

    ```
    step_bps = max(k_step * ATR_1h / mid * 10_000,
                   grid_edge_multiple * roundtrip_maker_bps)
    ```
    """
    if mid_price <= 0:
        raise ValueError("mid_price doit être > 0")
    atr_bps = cfg.build.k_step * atr_1h / mid_price * 10_000.0
    floor_bps = cfg.build.grid_edge_multiple * cfg.build.roundtrip_maker_bps
    bps = max(atr_bps, floor_bps)
    step = bps / 10_000.0 * mid_price
    # Un pas plus petit qu'un tick n'a pas de sens sur un carnet.
    return max(step, cfg.build.tick_size)


def fee_floor_bps(cfg: GridConfig) -> float:
    return cfg.build.grid_edge_multiple * cfg.build.roundtrip_maker_bps


# ── §3.3 Niveaux et sizing ───────────────────────────────────────────────────

@dataclass(frozen=True)
class GridPlan:
    levels: List[GridLevel]
    lower: float
    upper: float
    center: float
    step: float
    size_per_level: float
    atr_1h: float
    max_net_exposure_usd: float
    projected_worst_loss: float

    @property
    def n_levels(self) -> int:
        return len(self.levels)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "lower": self.lower, "upper": self.upper, "center": self.center,
            "step": round(self.step, 4), "levels": self.n_levels,
            "size_per_level": round(self.size_per_level, 8),
            "max_net_exposure_usd": round(self.max_net_exposure_usd, 2),
            "projected_worst_loss": round(self.projected_worst_loss, 2),
        }


def traversing_loss(size_per_level: float, prices: Sequence[float],
                    flatten_price: float, side: Side) -> float:
    """Perte mark-to-market si le prix traverse toute une demi-grille et sort.

    Chaque niveau touché ouvre `size_per_level` au prix du niveau ; au point de
    flatten, l'ensemble est débouclé. Pour un effondrement (côté BUY), chaque
    achat perd `prix_niveau − flatten`.

    C'est la contrainte de dimensionnement PRINCIPALE du §3.3 : c'est elle qui
    fixe la taille, et non le notionnel disponible.
    """
    sign = 1.0 if side is Side.BUY else -1.0
    return sum(max(0.0, sign * (p - flatten_price)) * size_per_level for p in prices)


def build_grid(cfg: GridConfig, rng: RangeSpec, atr_1h: float, atr_15m: float,
               equity: float) -> Optional[GridPlan]:
    """Construit la grille, ou rend None si elle ne se déploie pas.

    Rendre `None` est un résultat normal et fréquent : un range trop étroit pour
    porter `min_levels` niveaux au pas minimal ne paie pas, et le §2 l'exclut
    explicitement.
    """
    build = cfg.build
    if equity <= 0 or atr_1h <= 0:
        return None

    step = step_price(cfg, atr_1h, rng.center)
    n_total = int(rng.width // step)
    if n_total < cfg.activation.min_levels:
        logger.debug("grille non déployée: %d niveaux < min %d",
                     n_total, cfg.activation.min_levels)
        return None
    n_total = min(n_total, build.max_levels)

    # Répartition symétrique autour du centre : autant de BUY sous le centre que
    # de SELL au-dessus.
    per_side = n_total // 2
    if per_side < 1:
        return None

    buy_prices = [rng.center - step * (i + 1) for i in range(per_side)]
    sell_prices = [rng.center + step * (i + 1) for i in range(per_side)]
    buy_prices = [p for p in buy_prices if p >= rng.lower]
    sell_prices = [p for p in sell_prices if p <= rng.upper]
    if min(len(buy_prices), len(sell_prices)) < 1:
        return None
    if len(buy_prices) + len(sell_prices) < cfg.activation.min_levels:
        return None

    # §3.3 — sizing dérivé de la contrainte de perte traversante. Le point de
    # flatten est celui du §6.1 : borne ∓ k_breakout × ATR_15m.
    flatten_down = rng.lower - cfg.exits.k_breakout_atr15m * atr_15m
    flatten_up = rng.upper + cfg.exits.k_breakout_atr15m * atr_15m
    loss_down_unit = traversing_loss(1.0, buy_prices, flatten_down, Side.BUY)
    loss_up_unit = traversing_loss(1.0, sell_prices, flatten_up, Side.SELL)
    worst_unit = max(loss_down_unit, loss_up_unit)
    if worst_unit <= 0:
        return None

    budget = equity * build.max_grid_loss_pct
    size_per_level = budget / worst_unit
    if size_per_level <= 0:
        return None

    levels: List[GridLevel] = []
    for i, price in enumerate(sorted(buy_prices, reverse=True)):
        levels.append(GridLevel(price=_round_tick(price, build.tick_size), side=Side.BUY,
                                size=size_per_level,
                                paired_price=_round_tick(price + step, build.tick_size),
                                index=len(levels)))
    for i, price in enumerate(sorted(sell_prices)):
        levels.append(GridLevel(price=_round_tick(price, build.tick_size), side=Side.SELL,
                                size=size_per_level,
                                paired_price=_round_tick(price - step, build.tick_size),
                                index=len(levels)))

    # §4 — plafond d'inventaire net : valeur de `max_net_exposure_frac` des
    # niveaux d'un côté.
    one_side_notional = size_per_level * sum(buy_prices)
    max_net = build.max_net_exposure_frac * one_side_notional

    return GridPlan(
        levels=levels, lower=rng.lower, upper=rng.upper, center=rng.center,
        step=step, size_per_level=size_per_level, atr_1h=atr_1h,
        max_net_exposure_usd=max_net,
        projected_worst_loss=worst_unit * size_per_level,
    )


def _round_tick(price: float, tick: float) -> float:
    return round(price / tick) * tick


# ── §2 Conditions d'activation ───────────────────────────────────────────────

def check_activation(cfg: GridConfig, *, adx: Optional[float],
                     adx_bars_below: int, atr_percentile: Optional[float],
                     range_spec: Optional[RangeSpec], atr_1h: float,
                     funding_annualized: Optional[float],
                     fee_killswitch_active: bool, observation_mode: bool,
                     macro_extreme: bool, cooldown_remaining_h: float,
                     ) -> ActivationVerdict:
    """Toutes les conditions du §2, évaluées ensemble.

    Elles lisent les DONNÉES calculées par le `RegimeLayer` du ConfluenceAgent
    (`adx`, `atr_percentile`), pas son verdict : ce dernier veto en dehors de
    [20, 90] de percentile et quand le biais 1d est FLAT, or la grille exige
    [15, 60] et se moque du biais — un range sans direction est même son terrain
    idéal. Réutiliser le calcul sans réutiliser le jugement.
    """
    act = cfg.activation
    data: Dict[str, Any] = {
        "adx": adx, "adx_bars_below": adx_bars_below,
        "atr_percentile": atr_percentile, "funding_annualized": funding_annualized,
        "cooldown_remaining_h": round(cooldown_remaining_h, 2),
    }

    if macro_extreme:
        return ActivationVerdict(False, "veto macro: risk_level=EXTREME (§1)", data)
    if fee_killswitch_active:
        return ActivationVerdict(False, "kill-switch frais actif (partagé compte, §6.3)", data)
    if observation_mode:
        return ActivationVerdict(False, "mode observation actif (§12.4)", data)
    if cooldown_remaining_h > 0:
        return ActivationVerdict(
            False, f"cooldown post-cassure: {cooldown_remaining_h:.1f}h restantes (§6.1)", data)

    if adx is None or atr_percentile is None:
        return ActivationVerdict(False, "indicateurs de régime non disponibles", data)
    if adx >= act.adx_max:
        return ActivationVerdict(False, f"ADX={adx:.1f} ≥ {act.adx_max:g} — pas un range", data)
    if adx_bars_below < act.confirm_bars_1h:
        return ActivationVerdict(
            False, f"RANGE non confirmé: {adx_bars_below}/{act.confirm_bars_1h} bougies 1h",
            data)

    lo, hi = act.atr_percentile_range
    if not lo <= atr_percentile <= hi:
        side = "trop calme" if atr_percentile < lo else "trop volatil"
        return ActivationVerdict(
            False, f"volatilité {side}: percentile ATR={atr_percentile:.1f} hors [{lo:g}, {hi:g}]",
            data)

    if funding_annualized is None:
        return ActivationVerdict(False, "funding indisponible: filtre §2 non évaluable", data)
    if abs(funding_annualized) >= act.funding_max_annualized:
        return ActivationVerdict(
            False, f"funding {funding_annualized:+.1%} au-delà de "
                   f"±{act.funding_max_annualized:.0%}", data)

    if range_spec is None:
        return ActivationVerdict(False, "range non détectable (historique 15m insuffisant)", data)
    data["range_width"] = range_spec.width
    data["range_atr_multiple"] = range_spec.width / atr_1h if atr_1h > 0 else 0.0
    if atr_1h <= 0 or range_spec.width < act.min_range_atr * atr_1h:
        return ActivationVerdict(
            False, f"range trop étroit: {data['range_atr_multiple']:.1f}× ATR "
                   f"< {act.min_range_atr:g}×", data)

    return ActivationVerdict(True, "conditions §2 réunies", data)


__all__ = ["GridPlan", "RangeSpec", "build_grid", "check_activation", "detect_range",
           "fee_floor_bps", "step_price", "traversing_loss"]

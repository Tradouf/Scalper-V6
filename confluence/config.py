"""
Chargement et validation de `config/confluence.yaml` — SPEC §7.

Le fichier YAML est la seule source de vérité ; ce module le transforme en
dataclasses figées et refuse de démarrer sur une configuration incohérente.

Pourquoi valider aussi durement : un `adx_range` supérieur à `adx_trend` ne
lève aucune exception à l'exécution, il fabrique juste une zone morte inversée
où le bot trade en permanence. Ce genre de faute passe inaperçue pendant tout
un backtest et n'apparaît qu'en live. Mieux vaut refuser de charger.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "confluence.yaml"

# Marge de bougies au-delà du warmup strict, ajoutée à la fenêtre passée aux
# couches. Elle est la même en live et en backtest, et c'est TOUT ce qui compte :
# une EMA amorcée sur une fenêtre glissante dépend de la longueur de cette
# fenêtre. Tant que les deux chemins passent exactement `window_bars` bougies,
# ils calculent exactement le même nombre. Si cette constante changeait entre un
# backtest validé et le live, les deux ne mesureraient plus la même stratégie.
WINDOW_SLACK = 200


class ConfigError(ValueError):
    """Configuration incohérente : on ne démarre pas."""


# ── Sections ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BiasConfig:
    ema: int = 100
    sma_fast: int = 50
    sma_slow: int = 200
    confirm_closes: int = 2

    def validate(self) -> None:
        if self.sma_fast >= self.sma_slow:
            raise ConfigError("bias_1d: sma_fast doit être < sma_slow")
        if min(self.ema, self.sma_fast, self.sma_slow) < 2:
            raise ConfigError("bias_1d: périodes < 2")
        if self.confirm_closes < 1:
            raise ConfigError("bias_1d: confirm_closes doit être ≥ 1")

    @property
    def warmup_bars(self) -> int:
        return max(self.ema, self.sma_slow) + self.confirm_closes

    @property
    def window_bars(self) -> int:
        return self.warmup_bars + WINDOW_SLACK


@dataclass(frozen=True)
class RegimeConfig:
    adx_period: int = 14
    adx_trend: float = 25.0
    adx_range: float = 20.0
    atr_period: int = 14
    atr_percentile_min: float = 20.0
    atr_percentile_max: float = 90.0
    atr_percentile_days: int = 90
    funding_max_annualized: float = 0.30
    ema_fast: int = 21
    ema_slow: int = 55

    def validate(self) -> None:
        if self.adx_range >= self.adx_trend:
            raise ConfigError(
                "regime_1h: adx_range doit être < adx_trend "
                "(sinon la zone morte §4.2 est inversée et n'exclut plus rien)"
            )
        if not 0 <= self.atr_percentile_min < self.atr_percentile_max <= 100:
            raise ConfigError("regime_1h: percentiles ATR incohérents (0 ≤ min < max ≤ 100)")
        if self.ema_fast >= self.ema_slow:
            raise ConfigError("regime_1h: ema_fast doit être < ema_slow")
        if self.funding_max_annualized <= 0:
            raise ConfigError("regime_1h: funding_max_annualized doit être > 0")
        if self.atr_percentile_days < 1:
            raise ConfigError("regime_1h: atr_percentile_days doit être ≥ 1")

    @property
    def percentile_window_bars(self) -> int:
        return self.atr_percentile_days * 24

    @property
    def warmup_bars(self) -> int:
        # Le percentile a besoin d'une fenêtre pleine d'ATR, or l'ATR lui-même
        # coûte `atr_period` barres d'amorçage : les deux s'ADDITIONNENT. Les
        # traiter comme un max donnerait un percentile calculé sur une fenêtre
        # partielle, donc un filtre de volatilité qui ne filtre pas.
        return max(self.percentile_window_bars + self.atr_period,
                   self.ema_slow, 2 * self.adx_period + 2)

    @property
    def window_bars(self) -> int:
        return self.warmup_bars + WINDOW_SLACK


@dataclass(frozen=True)
class TimingConfig:
    ema_pullback: int = 20
    ema_invalidation: int = 50
    bbw_sma: int = 20
    bbw_std: float = 2.0
    signal_ttl_bars: int = 2
    setup_lookback_bars: int = 8
    invalidation_use_wick: bool = False
    entry_zone_atr_frac: float = 0.25

    def validate(self) -> None:
        if self.ema_pullback >= self.ema_invalidation:
            raise ConfigError(
                "timing_15m: ema_pullback doit être < ema_invalidation "
                "(l'invalidation doit être PLUS profonde que la zone de pullback)"
            )
        if self.signal_ttl_bars < 1:
            raise ConfigError("timing_15m: signal_ttl_bars doit être ≥ 1")
        if self.setup_lookback_bars < 1:
            raise ConfigError("timing_15m: setup_lookback_bars doit être ≥ 1")
        if self.entry_zone_atr_frac < 0:
            raise ConfigError("timing_15m: entry_zone_atr_frac doit être ≥ 0")

    @property
    def warmup_bars(self) -> int:
        return max(self.ema_invalidation, self.bbw_sma * 2) + self.setup_lookback_bars

    @property
    def window_bars(self) -> int:
        return self.warmup_bars + WINDOW_SLACK


@dataclass(frozen=True)
class ExecutionConfig:
    post_only: bool = True
    fill_timeout_s: float = 90.0
    max_requotes: int = 3
    tick_offset: int = 1
    tick_size: float = 1.0
    max_spread_bps: float = 3.0
    anomaly_atr_mult: float = 4.0
    anomaly_lookback_bars: int = 5

    def validate(self) -> None:
        if not self.post_only:
            raise ConfigError(
                "execution_1m: post_only=false est interdit par §11 "
                "(« pas de bascule taker automatique à l'entrée »)"
            )
        if self.fill_timeout_s <= 0:
            raise ConfigError("execution_1m: fill_timeout_s doit être > 0")
        if self.max_requotes < 0:
            raise ConfigError("execution_1m: max_requotes doit être ≥ 0")
        if self.tick_size <= 0:
            raise ConfigError("execution_1m: tick_size doit être > 0")


@dataclass(frozen=True)
class RiskConfig:
    risk_pct: float = 0.005
    k_stop: float = 1.5
    max_leverage: float = 3.0
    max_position_usd: float = 50_000.0
    max_trades_per_day: int = 3
    cooldown_after_loss_h: float = 4.0
    cooldown_after_trade_h: float = 1.0
    edge_multiple: float = 5.0
    k_edge: float = 1.0
    fee_killswitch_ratio: float = 0.25
    fee_killswitch_days: int = 30
    fee_maker: float = 0.00015
    fee_taker: float = 0.00045
    close_timeout_s: float = 120.0

    def validate(self) -> None:
        if not 0 < self.risk_pct < 1:
            raise ConfigError("risk: risk_pct doit être dans ]0, 1[")
        if self.k_stop <= 0:
            raise ConfigError("risk: k_stop doit être > 0")
        if self.max_leverage <= 0:
            raise ConfigError("risk: max_leverage doit être > 0")
        if self.max_trades_per_day < 1:
            raise ConfigError("risk: max_trades_per_day doit être ≥ 1")
        if self.edge_multiple <= 0 or self.k_edge <= 0:
            raise ConfigError("risk: edge_multiple et k_edge doivent être > 0")
        if min(self.fee_maker, self.fee_taker) < 0:
            raise ConfigError("risk: frais négatifs")
        if not 0 < self.fee_killswitch_ratio <= 1:
            raise ConfigError("risk: fee_killswitch_ratio doit être dans ]0, 1]")

    @property
    def fee_roundtrip(self) -> float:
        """Frais aller-retour en fraction du notionnel : entrée maker (§4.4
        l'impose) + sortie taker (le trailing sort au marché, §6.3). C'est
        l'estimation conservatrice utilisée par le filtre d'edge §6.5."""
        return self.fee_maker + self.fee_taker


@dataclass(frozen=True)
class MacroConfig:
    enabled: bool = True
    provider: str = "none"
    file_path: str = ""
    max_age_h: float = 8.0
    poll_interval_s: float = 14400.0

    def validate(self) -> None:
        if self.provider not in ("none", "file"):
            raise ConfigError(f"macro: provider inconnu {self.provider!r} (none|file)")
        if self.provider == "file" and not self.file_path:
            raise ConfigError("macro: provider=file exige file_path")


@dataclass(frozen=True)
class TrailingConfig:
    atr_period: int = 14
    k_trail: float = 2.5
    activate_at_r: float = 1.0
    breakeven_at_r: float = 1.0
    k_trail_tight: float = 1.5
    tighten_at_r: float = 2.0

    def validate(self) -> None:
        if self.k_trail <= 0 or self.k_trail_tight <= 0:
            raise ConfigError("trailing: k_trail doit être > 0")
        if self.k_trail_tight > self.k_trail:
            raise ConfigError("trailing: k_trail_tight doit être ≤ k_trail (c'est un resserrement)")
        if self.tighten_at_r < self.activate_at_r:
            raise ConfigError("trailing: tighten_at_r doit être ≥ activate_at_r")


@dataclass(frozen=True)
class MeanRevConfig:
    enabled: bool = True
    zscore_period: int = 48
    entry_z: float = 2.0
    exit_z: float = 0.5
    adf_lags: int = 1
    adf_alpha: float = 0.05
    half_life_min_bars: float = 2.0
    half_life_max_bars: float = 48.0

    def validate(self) -> None:
        if self.entry_z <= self.exit_z:
            raise ConfigError("meanrev: entry_z doit être > exit_z")
        if self.zscore_period < 5:
            raise ConfigError("meanrev: zscore_period doit être ≥ 5")
        if self.adf_alpha not in (0.01, 0.05, 0.10):
            raise ConfigError("meanrev: adf_alpha doit valoir 0.01, 0.05 ou 0.10 (table figée)")
        if self.half_life_min_bars >= self.half_life_max_bars:
            raise ConfigError("meanrev: half_life_min_bars doit être < half_life_max_bars")


@dataclass(frozen=True)
class WalkForwardConfig:
    is_months: int = 12
    oos_months: int = 3
    step_months: int = 3


@dataclass(frozen=True)
class AcceptanceConfig:
    min_profit_factor_oos: float = 1.3
    max_fee_ratio: float = 0.15
    min_trades_total: int = 100
    max_trades_per_day_avg: float = 3.0
    max_oos_dd_vs_is_multiple: float = 2.0


@dataclass(frozen=True)
class SensitivityConfig:
    params: tuple = ("regime_1h.adx_trend", "risk.k_stop", "risk.edge_multiple")
    deltas: tuple = (-0.2, 0.2)


@dataclass(frozen=True)
class PlaceboConfig:
    n_draws: int = 30
    alpha: float = 0.05


@dataclass(frozen=True)
class BacktestConfig:
    history_days: int = 1100
    slippage_bps_market: float = 2.0
    walkforward: WalkForwardConfig = None            # type: ignore[assignment]
    acceptance: AcceptanceConfig = None              # type: ignore[assignment]
    sensitivity: SensitivityConfig = None            # type: ignore[assignment]
    placebo: PlaceboConfig = None                    # type: ignore[assignment]

    def validate(self) -> None:
        if self.walkforward.oos_months < 1 or self.walkforward.is_months < 1:
            raise ConfigError("backtest.walkforward: fenêtres IS/OOS doivent être ≥ 1 mois")
        if self.slippage_bps_market < 0:
            raise ConfigError("backtest: slippage_bps_market doit être ≥ 0")


@dataclass(frozen=True)
class RegimeConditionerConfig:
    vol_percentile_low: float = 30.0
    vol_percentile_high: float = 70.0

    def validate(self) -> None:
        if self.vol_percentile_low >= self.vol_percentile_high:
            raise ConfigError(
                "adaptive.regime_conditioner: vol_percentile_low doit être < high "
                "(sinon l'interpolation §12.3 s'inverse et durcit le risque quand "
                "le marché se calme)")


@dataclass(frozen=True)
class AdaptiveWalkForwardConfig:
    schedule_cron: str = "0 3 1 * *"
    max_param_drift: float = 0.40
    fail_cycles_to_observation: int = 3

    def validate(self) -> None:
        if not 0 < self.max_param_drift <= 5:
            raise ConfigError("adaptive.walk_forward: max_param_drift doit être dans ]0, 5]")
        if self.fail_cycles_to_observation < 1:
            raise ConfigError("adaptive.walk_forward: fail_cycles_to_observation ≥ 1")


@dataclass(frozen=True)
class PostureSelectorConfig:
    enabled: bool = True
    shadow_days: int = 45
    backend: str = "localai"
    endpoint: str = "http://queen.local:8080/v1"
    model: str = ""
    min_confidence: float = 0.6
    aggressive_confirm_days: int = 3

    def validate(self) -> None:
        if self.backend not in ("localai", "anthropic"):
            raise ConfigError(
                f"adaptive.posture_selector: backend inconnu {self.backend!r} "
                f"(localai|anthropic)")
        if not 0 <= self.min_confidence <= 1:
            raise ConfigError("adaptive.posture_selector: min_confidence hors [0, 1]")
        if self.aggressive_confirm_days < 1:
            raise ConfigError(
                "adaptive.posture_selector: aggressive_confirm_days ≥ 1 — le ratchet "
                "asymétrique du §12.5 n'a pas de sens sans au moins une confirmation")
        if self.shadow_days < 0:
            raise ConfigError("adaptive.posture_selector: shadow_days ≥ 0")


@dataclass(frozen=True)
class AdaptiveConfig:
    registry_path: str = "state/param_registry/"
    regime_conditioner: RegimeConditionerConfig = None    # type: ignore[assignment]
    walk_forward: AdaptiveWalkForwardConfig = None        # type: ignore[assignment]
    posture_selector: PostureSelectorConfig = None        # type: ignore[assignment]

    def validate(self) -> None:
        self.regime_conditioner.validate()
        self.walk_forward.validate()
        self.posture_selector.validate()


@dataclass(frozen=True)
class ConfluenceConfig:
    symbol: str = "BTC"
    bias_1d: BiasConfig = None                       # type: ignore[assignment]
    regime_1h: RegimeConfig = None                   # type: ignore[assignment]
    timing_15m: TimingConfig = None                  # type: ignore[assignment]
    execution_1m: ExecutionConfig = None             # type: ignore[assignment]
    risk: RiskConfig = None                          # type: ignore[assignment]
    macro: MacroConfig = None                        # type: ignore[assignment]
    trailing: TrailingConfig = None                  # type: ignore[assignment]
    meanrev: MeanRevConfig = None                    # type: ignore[assignment]
    backtest: BacktestConfig = None                  # type: ignore[assignment]
    adaptive: AdaptiveConfig = None                  # type: ignore[assignment]

    def validate(self) -> None:
        if not self.symbol:
            raise ConfigError("symbol manquant")
        for f in fields(self):
            section = getattr(self, f.name)
            if is_dataclass(section) and hasattr(section, "validate"):
                section.validate()

    def replace_path(self, dotted: str, value: Any) -> "ConfluenceConfig":
        """Copie de la config avec un paramètre pointé remplacé
        (« regime_1h.adx_trend »). Sert à l'analyse de sensibilité §9.5 : elle
        doit pouvoir faire varier un paramètre sans réécrire le YAML, et sans
        muter la config partagée."""
        raw = self.to_dict()
        node = raw
        parts = dotted.split(".")
        for p in parts[:-1]:
            if p not in node:
                raise ConfigError(f"chemin inconnu : {dotted}")
            node = node[p]
        if parts[-1] not in node:
            raise ConfigError(f"chemin inconnu : {dotted}")
        node[parts[-1]] = value
        return from_dict(raw)

    def get_path(self, dotted: str) -> Any:
        node: Any = self
        for p in dotted.split("."):
            node = getattr(node, p)
        return node

    def to_dict(self) -> Dict[str, Any]:
        return _as_dict(self)


# ── Chargement ───────────────────────────────────────────────────────────────

_SECTIONS = {
    "bias_1d": BiasConfig,
    "regime_1h": RegimeConfig,
    "timing_15m": TimingConfig,
    "execution_1m": ExecutionConfig,
    "risk": RiskConfig,
    "macro": MacroConfig,
    "trailing": TrailingConfig,
    "meanrev": MeanRevConfig,
}


def from_dict(raw: Dict[str, Any],
              adaptive: Optional[Dict[str, Any]] = None) -> ConfluenceConfig:
    """Construit la config depuis un dict (contenu de la clé `confluence:`).

    Toute clé inconnue est une ERREUR, pas un avertissement : un `k_stopp: 1.5`
    silencieusement ignoré ferait tourner le backtest sur le défaut tout en
    laissant croire qu'on a testé autre chose.

    `adaptive` correspond au bloc §12.7, que la spec présente comme une racine
    YAML distincte de `confluence:`. On le charge séparément mais on le range
    dans le même objet de config : deux fichiers de vérité pour un seul bot,
    c'est une divergence qui finit toujours par arriver.
    """
    raw = copy.deepcopy(raw or {})
    kwargs: Dict[str, Any] = {"symbol": raw.pop("symbol", "BTC")}

    for key, cls in _SECTIONS.items():
        kwargs[key] = _build(cls, raw.pop(key, {}) or {}, key)

    bt = raw.pop("backtest", {}) or {}
    kwargs["backtest"] = BacktestConfig(
        history_days=int(bt.pop("history_days", 1100)),
        slippage_bps_market=float(bt.pop("slippage_bps_market", 2.0)),
        walkforward=_build(WalkForwardConfig, bt.pop("walkforward", {}) or {},
                           "backtest.walkforward"),
        acceptance=_build(AcceptanceConfig, bt.pop("acceptance", {}) or {},
                          "backtest.acceptance"),
        sensitivity=_build(SensitivityConfig, bt.pop("sensitivity", {}) or {},
                           "backtest.sensitivity", tuples=("params", "deltas")),
        placebo=_build(PlaceboConfig, bt.pop("placebo", {}) or {}, "backtest.placebo"),
    )
    if bt:
        raise ConfigError(f"backtest: clés inconnues {sorted(bt)}")

    # Le bloc §12.7 peut arriver en racine YAML séparée (`adaptive:`) ou être
    # imbriqué sous `confluence:` ; les deux placements sont acceptés, mais pas
    # les deux à la fois — il faudrait alors deviner lequel fait autorité.
    nested = raw.pop("adaptive", None)
    if nested is not None and adaptive is not None:
        raise ConfigError(
            "`adaptive:` défini à la fois en racine et sous `confluence:` — "
            "n'en garder qu'un")
    if raw:
        raise ConfigError(f"confluence: clés inconnues {sorted(raw)}")

    ad = copy.deepcopy(adaptive if adaptive is not None else (nested or {}))
    kwargs["adaptive"] = AdaptiveConfig(
        registry_path=str(ad.pop("registry_path", "state/param_registry/")),
        regime_conditioner=_build(RegimeConditionerConfig,
                                  ad.pop("regime_conditioner", {}) or {},
                                  "adaptive.regime_conditioner"),
        walk_forward=_build(AdaptiveWalkForwardConfig, ad.pop("walk_forward", {}) or {},
                            "adaptive.walk_forward"),
        posture_selector=_build(PostureSelectorConfig, ad.pop("posture_selector", {}) or {},
                                "adaptive.posture_selector"),
    )
    if ad:
        raise ConfigError(f"adaptive: clés inconnues {sorted(ad)}")

    cfg = ConfluenceConfig(**kwargs)
    cfg.validate()
    return cfg


def load(path: Optional[Path] = None) -> ConfluenceConfig:
    """Charge le YAML. `path=None` ⇒ config/confluence.yaml du repo."""
    import yaml

    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        raise ConfigError(f"configuration introuvable : {p}")
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if "confluence" not in doc:
        raise ConfigError(f"{p} : clé racine `confluence:` manquante")
    return from_dict(doc["confluence"], adaptive=doc.get("adaptive"))


def _build(cls, values: Dict[str, Any], label: str, tuples: tuple = ()):
    known = {f.name: f for f in fields(cls)}
    unknown = set(values) - set(known)
    if unknown:
        raise ConfigError(f"{label}: clés inconnues {sorted(unknown)}")
    coerced: Dict[str, Any] = {}
    for name, value in values.items():
        if name in tuples:
            coerced[name] = tuple(value)
            continue
        target = known[name].type
        try:
            if target in (int, "int"):
                coerced[name] = int(value)
            elif target in (float, "float"):
                coerced[name] = float(value)
            elif target in (bool, "bool"):
                coerced[name] = bool(value)
            else:
                coerced[name] = value
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{label}.{name}: valeur invalide {value!r} ({exc})") from exc
    return cls(**coerced)


def _as_dict(obj: Any) -> Any:
    if is_dataclass(obj):
        return {f.name: _as_dict(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, tuple):
        return list(obj)
    if isinstance(obj, list):
        return [_as_dict(x) for x in obj]
    return obj


__all__ = [
    "AcceptanceConfig", "AdaptiveConfig", "AdaptiveWalkForwardConfig",
    "BacktestConfig", "BiasConfig", "ConfigError", "ConfluenceConfig",
    "ExecutionConfig", "MacroConfig", "MeanRevConfig", "PlaceboConfig",
    "PostureSelectorConfig", "RegimeConditionerConfig", "RegimeConfig",
    "RiskConfig", "SensitivityConfig", "TimingConfig", "TrailingConfig",
    "WalkForwardConfig", "WINDOW_SLACK", "from_dict", "load",
]

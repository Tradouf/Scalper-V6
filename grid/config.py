"""
Configuration du GridAgent — SPEC §11, validée au chargement.

Deux validations sortent de l'ordinaire et méritent d'être signalées :

* **Rejet de toute progression de taille (§10, §12)** — le chargeur refuse
  explicitement les clés qui ouvriraient la porte à une martingale
  (`size_multiplier`, `martingale`, `size_progression`…). Le §10 les met « hors
  périmètre » ; ici elles font échouer le démarrage. Une grille qui double sous
  le centre finit toujours par rencontrer le mouvement qui la ruine, et
  l'interdiction n'a de valeur que si elle est mécanique.
* **Plancher de frais non contournable** — `grid_edge_multiple` doit rester ≥ 1.
  En dessous, l'espacement pourrait descendre sous le coût aller-retour et la
  grille deviendrait une machine à payer l'exchange (§0, point 2).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "grid.yaml"

# Clés dont la seule présence trahit une tentative de progression de taille.
# Le §10 les interdit ; on les refuse au chargement plutôt qu'à l'exécution.
FORBIDDEN_KEYS = (
    "size_multiplier", "martingale", "size_progression", "geometric_sizing",
    "size_growth", "double_down", "averaging_down",
)


class GridConfigError(ValueError):
    """Configuration incohérente ou interdite : on ne démarre pas."""


@dataclass(frozen=True)
class ActivationConfig:
    confirm_bars_1h: int = 3
    adx_max: float = 20.0
    atr_percentile_range: Tuple[float, float] = (15.0, 60.0)
    min_range_atr: float = 4.0
    min_levels: int = 6
    funding_max_annualized: float = 0.30

    def validate(self) -> None:
        lo, hi = self.atr_percentile_range
        if not 0 <= lo < hi <= 100:
            raise GridConfigError("activation: atr_percentile_range incohérent")
        if self.confirm_bars_1h < 1:
            raise GridConfigError("activation: confirm_bars_1h ≥ 1")
        if self.min_levels < 2:
            raise GridConfigError(
                "activation: min_levels ≥ 2 — une grille à un niveau n'est pas une grille")
        if self.min_range_atr <= 0:
            raise GridConfigError("activation: min_range_atr > 0")


@dataclass(frozen=True)
class BuildConfig:
    range_lookback_bars_15m: int = 384
    k_step: float = 0.35
    grid_edge_multiple: float = 5.0
    max_levels: int = 20
    max_grid_loss_pct: float = 0.015
    max_net_exposure_frac: float = 0.60
    maker_fee: float = 0.00015
    taker_fee: float = 0.00045
    tick_size: float = 1.0

    def validate(self) -> None:
        if self.k_step <= 0:
            raise GridConfigError("build: k_step > 0")
        if self.grid_edge_multiple < 1:
            raise GridConfigError(
                "build: grid_edge_multiple ≥ 1 — en dessous, l'espacement peut passer "
                "sous le coût aller-retour et la grille paie l'exchange pour tourner (§0)")
        if self.max_levels < 2:
            raise GridConfigError("build: max_levels ≥ 2")
        if not 0 < self.max_grid_loss_pct < 1:
            raise GridConfigError("build: max_grid_loss_pct dans ]0, 1[")
        if not 0 < self.max_net_exposure_frac <= 1:
            raise GridConfigError("build: max_net_exposure_frac dans ]0, 1]")
        if min(self.maker_fee, self.taker_fee) < 0:
            raise GridConfigError("build: frais négatifs")
        if self.tick_size <= 0:
            raise GridConfigError("build: tick_size > 0")

    @property
    def roundtrip_maker_bps(self) -> float:
        """Coût aller-retour d'un cycle de niveau, en points de base (§3.2).

        Deux fills maker par cycle : celui qui ouvre, celui qui verrouille.
        """
        return 2.0 * self.maker_fee * 10_000.0


@dataclass(frozen=True)
class FundingSkewConfig:
    enabled: bool = False
    threshold_annualized: float = 0.15
    skew_ticks: int = 0

    def validate(self) -> None:
        if self.enabled and self.skew_ticks <= 0:
            raise GridConfigError(
                "funding_skew: activé mais skew_ticks = 0 — sans décalage, le skew "
                "n'a aucun effet et masquerait une erreur de configuration")


@dataclass(frozen=True)
class ExecutionConfig:
    post_only: bool = True
    manage_tick_s: float = 30.0
    requote_attempts: int = 3

    def validate(self) -> None:
        if not self.post_only:
            raise GridConfigError(
                "execution: post_only=false est interdit par §10 — le taker n'est "
                "autorisé qu'au flatten d'urgence (§6.1/6.2), jamais à l'entrée")
        if self.requote_attempts < 0:
            raise GridConfigError("execution: requote_attempts ≥ 0")


@dataclass(frozen=True)
class ExitsConfig:
    k_breakout_atr15m: float = 0.5
    flatten_timeout_s: float = 60.0
    breakout_cooldown_h: float = 12.0
    breakout_handoff: bool = True
    handoff_stop_k_atr: float = 1.0
    handoff_max_position_usd: float = 50_000.0

    def validate(self) -> None:
        if self.k_breakout_atr15m <= 0:
            raise GridConfigError(
                "exits: k_breakout_atr15m > 0 — un seuil nul déclencherait la cassure "
                "au moindre contact de borne")
        if self.breakout_cooldown_h < 0:
            raise GridConfigError("exits: breakout_cooldown_h ≥ 0")
        if self.handoff_stop_k_atr <= 0:
            raise GridConfigError("exits: handoff_stop_k_atr > 0")


@dataclass(frozen=True)
class AcceptanceConfig:
    min_profit_factor_oos: float = 1.2
    max_session_loss_multiple: float = 1.1
    max_fee_ratio: float = 0.20
    min_sessions: int = 30


@dataclass(frozen=True)
class PlaceboConfig:
    n_draws: int = 40
    alpha: float = 0.025

    def validate(self) -> None:
        # Contrainte dure de placebo_gate : p_min = 1/(n+1). En dessous de 1/α
        # tirages, le gate NE PEUT PAS passer, quelle que soit la stratégie.
        if 1.0 / (self.n_draws + 1) >= self.alpha:
            raise GridConfigError(
                f"placebo: n_draws={self.n_draws} trop faible pour α={self.alpha} "
                f"(p minimale atteignable = {1.0/(self.n_draws+1):.4f}) — "
                f"il en faut au moins {int(1.0/self.alpha)}")


@dataclass(frozen=True)
class SensitivityConfig:
    params: tuple = ("build.k_step", "build.grid_edge_multiple",
                     "exits.k_breakout_atr15m")
    deltas: tuple = (-0.2, 0.2)


@dataclass(frozen=True)
class BacktestConfig:
    history_days: int = 1100
    slippage_bps_market: float = 2.0
    placebo: PlaceboConfig = None            # type: ignore[assignment]
    sensitivity: SensitivityConfig = None    # type: ignore[assignment]

    def validate(self) -> None:
        if self.slippage_bps_market < 0:
            raise GridConfigError("backtest: slippage_bps_market ≥ 0")
        self.placebo.validate()


@dataclass(frozen=True)
class GridConfig:
    symbol: str = "BTC"
    activation: ActivationConfig = None       # type: ignore[assignment]
    build: BuildConfig = None                 # type: ignore[assignment]
    funding_skew: FundingSkewConfig = None    # type: ignore[assignment]
    execution: ExecutionConfig = None         # type: ignore[assignment]
    exits: ExitsConfig = None                 # type: ignore[assignment]
    acceptance: AcceptanceConfig = None       # type: ignore[assignment]
    backtest: BacktestConfig = None           # type: ignore[assignment]

    def validate(self) -> None:
        if not self.symbol:
            raise GridConfigError("symbol manquant")
        for f in fields(self):
            section = getattr(self, f.name)
            if is_dataclass(section) and hasattr(section, "validate"):
                section.validate()

    def replace_path(self, dotted: str, value: Any) -> "GridConfig":
        raw = self.to_dict()
        node = raw
        parts = dotted.split(".")
        for p in parts[:-1]:
            if p not in node:
                raise GridConfigError(f"chemin inconnu : {dotted}")
            node = node[p]
        if parts[-1] not in node:
            raise GridConfigError(f"chemin inconnu : {dotted}")
        node[parts[-1]] = value
        return from_dict(raw)

    def get_path(self, dotted: str) -> Any:
        node: Any = self
        for p in dotted.split("."):
            node = getattr(node, p)
        return node

    def to_dict(self) -> Dict[str, Any]:
        return _as_dict(self)


_SECTIONS = {
    "activation": ActivationConfig,
    "build": BuildConfig,
    "funding_skew": FundingSkewConfig,
    "execution": ExecutionConfig,
    "exits": ExitsConfig,
    "acceptance": AcceptanceConfig,
}


def _reject_size_progression(raw: Any, path: str = "grid") -> None:
    """Refuse toute clé de progression de taille, à n'importe quelle profondeur.

    §10 : « Aucune martingale ni progression de taille : taille constante par
    niveau. Toute variante "on double sous le centre" est refusée par
    construction. » Refuser au chargement plutôt qu'à l'exécution garantit qu'on
    ne découvre pas l'interdiction après trois heures de backtest.
    """
    if isinstance(raw, dict):
        for key, value in raw.items():
            lowered = str(key).lower()
            if any(bad in lowered for bad in FORBIDDEN_KEYS):
                raise GridConfigError(
                    f"{path}.{key}: progression de taille interdite par §10 — "
                    f"la taille par niveau est constante, sans exception")
            _reject_size_progression(value, f"{path}.{key}")
    elif isinstance(raw, list):
        for i, value in enumerate(raw):
            _reject_size_progression(value, f"{path}[{i}]")


def from_dict(raw: Dict[str, Any]) -> GridConfig:
    raw = copy.deepcopy(raw or {})
    _reject_size_progression(raw)

    kwargs: Dict[str, Any] = {"symbol": raw.pop("symbol", "BTC")}
    for key, cls in _SECTIONS.items():
        kwargs[key] = _build(cls, raw.pop(key, {}) or {}, key)

    bt = raw.pop("backtest", {}) or {}
    kwargs["backtest"] = BacktestConfig(
        history_days=int(bt.pop("history_days", 1100)),
        slippage_bps_market=float(bt.pop("slippage_bps_market", 2.0)),
        placebo=_build(PlaceboConfig, bt.pop("placebo", {}) or {}, "backtest.placebo"),
        sensitivity=_build(SensitivityConfig, bt.pop("sensitivity", {}) or {},
                           "backtest.sensitivity", tuples=("params", "deltas")),
    )
    if bt:
        raise GridConfigError(f"backtest: clés inconnues {sorted(bt)}")
    if raw:
        raise GridConfigError(f"grid: clés inconnues {sorted(raw)}")

    cfg = GridConfig(**kwargs)
    cfg.validate()
    return cfg


def load(path: Optional[Path] = None) -> GridConfig:
    import yaml

    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        raise GridConfigError(f"configuration introuvable : {p}")
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if "grid" not in doc:
        raise GridConfigError(f"{p} : clé racine `grid:` manquante")
    return from_dict(doc["grid"])


def _build(cls, values: Dict[str, Any], label: str, tuples: tuple = ()):
    known = {f.name: f for f in fields(cls)}
    unknown = set(values) - set(known)
    if unknown:
        raise GridConfigError(f"{label}: clés inconnues {sorted(unknown)}")
    coerced: Dict[str, Any] = {}
    for name, value in values.items():
        if name in tuples or name == "atr_percentile_range":
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
            raise GridConfigError(f"{label}.{name}: valeur invalide {value!r}") from exc
    return cls(**coerced)


def _as_dict(obj: Any) -> Any:
    if is_dataclass(obj):
        return {f.name: _as_dict(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, tuple):
        return list(obj)
    if isinstance(obj, list):
        return [_as_dict(x) for x in obj]
    return obj


__all__ = ["AcceptanceConfig", "ActivationConfig", "BacktestConfig", "BuildConfig",
           "ExecutionConfig", "ExitsConfig", "FORBIDDEN_KEYS", "FundingSkewConfig",
           "GridConfig", "GridConfigError", "PlaceboConfig", "SensitivityConfig",
           "from_dict", "load"]

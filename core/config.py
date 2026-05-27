"""
Configuration V7 typée avec pydantic.

Charge :
  - `config/allocation.yaml` : matrice B, bornes mult, vol target, seuils
  - paramètres de stratégies (grid, MR, momentum)
  - paramètres de risque (caps, kill-switch DD)
  - paramètres d'exécution (paper mode, seuil rebalance)

Tout est validé au chargement (typed, bornes, complétude matrice B).

Usage :
    from core.config import load_config
    cfg = load_config()  # charge depuis config/allocation.yaml par défaut
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from core.types import Regime


# ─── Modèles ─────────────────────────────────────────────────────────────────


class RegimeConfig(BaseModel):
    """Détecteur de régime."""

    window_bars: int = Field(100, ge=20, le=500, description="Fenêtre features (bars)")
    min_dwell_bars: int = Field(8, ge=1, le=100, description="Hystérésis : temps de séjour min sur un label dominant")
    timeframe: str = Field("1h", description="Timeframe candle")
    high_vol_atr_percentile: float = Field(0.85, ge=0.5, le=1.0, description="Percentile ATR pour HIGH_VOL")


class AllocationConfig(BaseModel):
    """Allocateur."""

    # Matrice base B[régime][stratégie]
    base_weights: dict[Regime, dict[str, float]]

    # Multiplicateur de performance
    mult_min: float = Field(0.3, ge=0.0, le=1.0)
    mult_max: float = Field(1.5, ge=1.0, le=3.0)
    perf_halflife_days: float = Field(45.0, ge=7.0, le=180.0, description="Demi-vie EMA score de perf")

    # Vol-targeting
    vol_target: float = Field(0.10, ge=0.01, le=1.0, description="Vol annualisée cible par stratégie")

    @field_validator("base_weights")
    @classmethod
    def _check_all_regimes(cls, v: dict[Regime, dict[str, float]]) -> dict[Regime, dict[str, float]]:
        missing = set(Regime) - set(v.keys())
        if missing:
            raise ValueError(f"base_weights : régimes manquants {missing}")
        # Tous les régimes doivent avoir les mêmes stratégies
        strategies = {tuple(sorted(d.keys())) for d in v.values()}
        if len(strategies) > 1:
            raise ValueError(f"base_weights : ensembles de stratégies incohérents entre régimes : {strategies}")
        # Bornes [0, 5] sur les poids
        for r, d in v.items():
            for s, w in d.items():
                if not (0.0 <= w <= 5.0):
                    raise ValueError(f"base_weights[{r}][{s}]={w} hors [0,5]")
        return v


class RiskConfig(BaseModel):
    """Risk manager."""

    max_gross_exposure_pct: float = Field(2.0, ge=0.1, le=10.0, description="Gross exposure max en × equity")
    max_per_asset_pct: float = Field(0.4, ge=0.05, le=2.0, description="Notional max par asset en × equity")
    kill_switch_dd_pct: float = Field(0.10, ge=0.02, le=0.50, description="Drawdown qui kill tout")
    daily_loss_limit_pct: float = Field(0.03, ge=0.005, le=0.10)


class ExecutionConfig(BaseModel):
    """Engine d'exécution."""

    paper_mode: bool = Field(True, description="True = paper trading, False = live")
    rebalance_threshold_pct: float = Field(0.005, ge=0.0, le=0.05, description="Bande non-trade : |target-current| < seuil × gross → skip")
    dashboard_port: int = Field(8082, ge=1024, le=65535)


class GridStrategyConfig(BaseModel):
    enabled: bool = True
    atr_factor: float = Field(0.5, ge=0.1, le=2.0)
    levels: int = Field(5, ge=2, le=10)
    notional_per_level_usdc: float = Field(15.0, ge=5.0, le=500.0)
    drift_k: float = Field(3.0, ge=1.0, le=5.0)
    drift_window_sec: int = Field(3600, ge=300, le=86400)
    health_check_sec: int = Field(300, ge=60, le=3600)
    activation_threshold_usdc: float = Field(20.0, ge=5.0, le=500.0, description="Budget min pour activer le grid")


class MeanReversionStrategyConfig(BaseModel):
    enabled: bool = True
    interval: str = "1h"
    window: int = Field(50, ge=20, le=200)
    entry_z: float = Field(2.0, ge=1.0, le=4.0)
    exit_z: float = Field(0.4, ge=0.0, le=1.0)
    hl_min: float = Field(5.0, ge=1.0, le=20.0)
    hl_max: float = Field(48.0, ge=20.0, le=200.0)
    cooldown_sec: int = Field(1800, ge=60, le=86400)
    notional_usdc: float = Field(30.0, ge=5.0, le=500.0)
    sl_z: float = Field(3.5, ge=2.0, le=6.0)
    min_sl_buffer_std: float = Field(1.5, ge=0.5, le=3.0)


class MomentumStrategyConfig(BaseModel):
    enabled: bool = True
    interval: str = "1h"
    lookback_bars: int = Field(48, ge=12, le=200, description="Bars pour calcul slope")
    entry_zscore: float = Field(1.5, ge=0.5, le=4.0, description="Z-score slope min pour entrer")
    notional_usdc: float = Field(30.0, ge=5.0, le=500.0)


class StrategiesConfig(BaseModel):
    grid: GridStrategyConfig = Field(default_factory=GridStrategyConfig)
    mean_reversion: MeanReversionStrategyConfig = Field(default_factory=MeanReversionStrategyConfig)
    momentum: MomentumStrategyConfig = Field(default_factory=MomentumStrategyConfig)


class PathsConfig(BaseModel):
    """Chemins absolus à override en test."""

    data_historical: Path = Path("data/historical")
    memory: Path = Path("memory")
    logs: Path = Path("logs")


class V7Config(BaseModel):
    """Configuration globale V7."""

    regime: RegimeConfig = Field(default_factory=RegimeConfig)
    allocation: AllocationConfig
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    strategies: StrategiesConfig = Field(default_factory=StrategiesConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)

    # Symboles tradés (watchlist)
    symbols: list[str] = Field(default_factory=lambda: ["BTC", "ETH", "SOL", "BNB", "AAVE", "LINK", "SUI", "DOGE"])

    @model_validator(mode="after")
    def _check_strategies_in_matrix(self) -> "V7Config":
        # Toutes les stratégies citées dans la matrice B doivent correspondre
        # à un strategy_id valide (présent dans strategies config).
        enabled = set()
        if self.strategies.grid.enabled:
            enabled.add("grid")
        if self.strategies.mean_reversion.enabled:
            enabled.add("mean_reversion")
        if self.strategies.momentum.enabled:
            enabled.add("momentum")
        matrix_strats = set()
        for d in self.allocation.base_weights.values():
            matrix_strats.update(d.keys())
        unknown = matrix_strats - enabled - {"breakout", "pairs", "funding_carry"}  # noms futurs autorisés
        if unknown and not enabled.issubset(matrix_strats):
            # Une stratégie activée n'a pas de poids dans la matrice
            missing = enabled - matrix_strats
            if missing:
                raise ValueError(f"Stratégies activées sans poids dans base_weights : {missing}")
        return self


# ─── Chargement ──────────────────────────────────────────────────────────────


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ALLOCATION_PATH = REPO_ROOT / "config" / "allocation.yaml"


def load_config(allocation_path: Optional[Path] = None) -> V7Config:
    """Charge V7Config depuis YAML. Lève si invalide."""
    path = allocation_path or DEFAULT_ALLOCATION_PATH
    if not path.exists():
        raise FileNotFoundError(f"Config V7 introuvable : {path}")
    raw = yaml.safe_load(path.read_text())
    # Conversion clés Regime str → enum dans base_weights
    if "allocation" in raw and "base_weights" in raw["allocation"]:
        raw["allocation"]["base_weights"] = {
            Regime(k): v for k, v in raw["allocation"]["base_weights"].items()
        }
    return V7Config(**raw)

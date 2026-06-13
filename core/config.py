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
    # EmergencyExit (port V6 + Fix #8)
    emergency_exit_enabled: bool = Field(True, description="Active le mécanisme EMERGENCY EXIT (paper l'ignore de toute façon)")
    emergency_exit_roe_pct: float = Field(0.022, ge=0.005, le=0.10, description="Force-close si ROE ≤ -seuil (positions tracées ET orphelines)")
    orphan_grace_sec: float = Field(6.0, ge=1.0, le=60.0, description="Grâce avant force-close d'une orpheline (Fix #8)")
    # 2026-06-07 — francois prend des positions MANUELLES sur le même compte
    # (boucle HYPE 10x, swings BTC). Le bot ne doit JAMAIS les toucher :
    # ni orphan force-close (emergency), ni chargement au boot (sinon le
    # reconcile les solderait, target=0). Les positions STRATÉGIE sur ces
    # symboles restent protégées (branche tracée de l'emergency inchangée).
    manual_symbols: list[str] = Field(default_factory=list, description="Symboles à positions manuelles : exclus de l'orphan force-close et du BootReconciler")
    # Fix 9 PROTOTYPE — trail régime-gaté (port V6, flag OFF par défaut).
    # Tant que regime_gated_trail=False, comportement strictement inchangé.
    # Validation requise : rejouer le backtest sur 1m loggé par data/ohlc_1m/.
    regime_gated_trail: bool = Field(False, description="PROTOTYPE Fix 9 : activer le trail régime-gaté")
    regime_trend_slope_min: float = Field(0.003, ge=0.0, le=0.05, description="|pente| min pour qualifier régime 'trend' à l'entrée")
    regime_slope_bars: int = Field(12, ge=4, le=48, description="N bougies pré-entrée pour le calcul de pente")
    regime_trend_sl_dist_pct: float = Field(0.010, ge=0.002, le=0.05, description="Distance ratchet (trend) — non utilisée si flag OFF")
    # Fix 10 PROTOTYPE — haut levier exempt + cap (port V6, flag OFF par défaut).
    high_lev_emergency_exempt: bool = Field(False, description="PROTOTYPE Fix 10 Knob A : exempte les positions >= high_lev_threshold du seuil ROE serré")
    high_lev_threshold: int = Field(6, ge=2, le=20, description="Levier au-dessus duquel Knob A applique la distance prix au lieu du ROE")
    high_lev_emergency_price_pct: float = Field(0.006, ge=0.001, le=0.05, description="Distance prix (laisse respirer) si Knob A actif")
    high_lev_emergency_roe_cap: float = Field(0.08, ge=0.02, le=0.30, description="Cap ROE max (limite la perte) si Knob A actif")
    leverage_cap_enabled: bool = Field(False, description="PROTOTYPE Fix 10 Knob B : cap de levier préventif à l'entrée")
    global_leverage_cap: int = Field(5, ge=1, le=20, description="Cap levier global si Knob B actif")


class ExecutionConfig(BaseModel):
    """Engine d'exécution."""

    paper_mode: bool = Field(True, description="True = paper trading, False = live")
    rebalance_threshold_pct: float = Field(0.005, ge=0.0, le=0.05, description="Bande non-trade : |target-current| < seuil × gross → skip")
    dashboard_port: int = Field(8082, ge=1024, le=65535)
    # Bandit d'exécution (2026-06-06) : OFF = market systématique (comportement
    # historique). ON = la politique apprise par exec_bandit_shadow.py choisit
    # market vs limit GTC (timeout 30s → fallback market). Critère d'activation :
    # ≥500 fills shadow ET économie prequential ≥1 bps/ordre (cf. --report).
    exec_bandit_active: bool = Field(False, description="Bandit exécution : limit adaptatif appris (False = market historique)")


class GridStrategyConfig(BaseModel):
    enabled: bool = True
    atr_factor: float = Field(0.5, ge=0.1, le=2.0)
    levels: int = Field(5, ge=2, le=10)
    notional_per_level_usdc: float = Field(15.0, ge=5.0, le=500.0)
    drift_k: float = Field(3.0, ge=1.0, le=5.0)
    # 2026-06-01 : 3600→900. La grille fade une tendance pendant tout ce délai
    # avant de se désactiver (cas BNB short dans un rallye +5%). 15 min de dérive
    # SOUTENUE (le timer se reset si le prix revient en zone) = signal de trend.
    drift_window_sec: int = Field(900, ge=300, le=86400)
    health_check_sec: int = Field(300, ge=15, le=3600)  # min abaissé pour renouvellement rapide en high_vol
    # 2026-06-07 — range partagé priorité MR : fraction d'equity allouée à la
    # grille en RANGE (hors allocateur, qui reste 100% MR pour préserver la
    # taille des entrées MR). En high_vol, le budget vient du poids allocateur.
    range_budget_frac: float = Field(0.5, ge=0.0, le=1.0)
    # 2026-06-07 — biais directionnel : momentum 24h > seuil → grille long-only
    # (buy ladder seul, sells = TP), < -seuil → short-only, sinon symétrique.
    bias_momentum_pct: float = Field(0.01, ge=0.0, le=0.2)
    activation_threshold_usdc: float = Field(20.0, ge=5.0, le=500.0, description="Budget min pour activer le grid")
    # Garde bas-prix : spacing minimum exprimé en ticks HL. Empêche que plusieurs
    # niveaux ne s'arrondissent au même prix sur les actifs à petit prix (DOGE
    # ~$0.10 : spacing ATR 0.0003 < tick 0.001 → collisions/doublons/manquants).
    min_spacing_ticks: int = Field(2, ge=1, le=10, description="Spacing grid ≥ N ticks HL")
    # Boucle grille dédiée (port V6) : la FSM grille (pose TP, dégel, drift) tourne
    # dans un thread rapide séparé du tick principal 30s. Sinon, entre deux ticks
    # lents (jusqu'à 157s observé), buy ET sell se remplissent → net szi=0 → TP
    # reduce-only impossible → gel massif (~900 niveaux abandonnés observés).
    fast_loop_enabled: bool = Field(True, description="Thread grille dédié (cadence rapide)")
    fast_loop_sec: int = Field(3, ge=1, le=30, description="Cadence du thread grille (s)")
    # Mode high_vol (2026-06-02) : la grille devient la stratégie active en high_vol
    # avec des pas resserrés pour récolter l'oscillation. atr_factor réduit.
    high_vol_atr_factor: float = Field(0.25, ge=0.1, le=1.0, description="atr_factor en high_vol (pas serrés)")
    # Plancher de pas anti-frais : spacing ≥ min_spacing_pct × prix → garantit que
    # chaque round-trip (step) couvre les frais aller-retour + une marge.
    # Round-trip HL ~0.03% (maker) à 0.09% (taker) ; 0.10% laisse de la marge.
    min_spacing_pct: float = Field(0.001, ge=0.0003, le=0.02, description="Pas min en % du prix (couvre frais+marge)")
    # Enveloppe de sécurité high_vol : si la vol réalisée dépasse N× sa médiane
    # historique, la grille se ferait rincer → flat + pause (hystérésis : reprise
    # sous 0.8×N). Protège contre la vol "ingérable".
    high_vol_safety_mult: float = Field(2.5, ge=1.5, le=6.0, description="Coupe la grille si vol > N× médiane")
    # Frozen guard (fix V6 28/05 ported into V7) : timeout avant de basculer
    # un niveau frozen en done quand szi reste du mauvais côté.
    frozen_timeout_sec: int = Field(600, ge=60, le=3600, description="Timeout level frozen → done")


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


class SupertrendStrategyConfig(BaseModel):
    """Supertrend classique ATR-based, trend-following avec stop trailing intégré."""
    enabled: bool = True
    interval: str = "1h"
    period: int = Field(10, ge=5, le=30, description="Période ATR")
    multiplier: float = Field(3.0, ge=1.0, le=6.0, description="ATR multiplier")
    # Sizing dynamique : risque fixe par trade (% equity) ÷ distance au stop
    risk_per_trade_pct: float = Field(0.01, ge=0.001, le=0.05, description="Risque max par trade en % equity")
    notional_max_usdc: float = Field(500.0, ge=10.0, le=2000.0, description="Cap notional absolu")
    notional_min_usdc: float = Field(10.0, ge=5.0, le=100.0, description="Plancher notional")
    cooldown_sec: int = Field(900, ge=60, le=86400, description="Cooldown post-flip (15min par défaut)")


class StrategiesConfig(BaseModel):
    grid: GridStrategyConfig = Field(default_factory=GridStrategyConfig)
    mean_reversion: MeanReversionStrategyConfig = Field(default_factory=MeanReversionStrategyConfig)
    momentum: MomentumStrategyConfig = Field(default_factory=MomentumStrategyConfig)
    supertrend: SupertrendStrategyConfig = Field(default_factory=SupertrendStrategyConfig)


class PathsConfig(BaseModel):
    """Chemins absolus à override en test."""

    data_historical: Path = Path("data/historical")
    memory: Path = Path("memory")
    logs: Path = Path("logs")


class GovernorConfig(BaseModel):
    """Gouverneur de risque LLM (2026-06-13). Adapte emergency/taille au régime.
    Bornes dures appliquées en code (governor/risk_governor.py), pas ici."""

    enabled: bool = Field(False, description="Active le gouverneur LLM de risque")
    interval_sec: int = Field(900, ge=60, le=7200, description="Cadence tactique (défaut 15min)")
    llm_endpoint: str = Field("http://localhost:8080", description="Endpoint OpenAI-compatible (LocalAI)")
    llm_model: str = Field("qwen3.5-9b", description="Modèle LLM tactique (risque fin)")
    # Étage stratège (Opus, cadence lente) : pose l'enveloppe que le tactique
    # respecte. Coût négligeable (~5s/appel via claude -p).
    strategist_enabled: bool = Field(False, description="Active l'étage stratège Opus")
    strategist_interval_sec: int = Field(3600, ge=300, le=86400, description="Cadence stratège (défaut 1h)")
    strategist_model: str = Field("opus", description="Modèle stratège (via claude -p)")
    strategist_budget_usd: float = Field(0.5, ge=0.05, le=5.0, description="Budget max par appel stratège")


class V7Config(BaseModel):
    """Configuration globale V7."""

    regime: RegimeConfig = Field(default_factory=RegimeConfig)
    allocation: AllocationConfig
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    strategies: StrategiesConfig = Field(default_factory=StrategiesConfig)
    governor: GovernorConfig = Field(default_factory=GovernorConfig)
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
        if self.strategies.supertrend.enabled:
            enabled.add("supertrend")
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

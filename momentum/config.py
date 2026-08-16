"""
Configuration du MomentumAgent — SPEC §11, validée au chargement.

**Fichier autonome (§9.3).** Ce module ne lit ni `confluence.yaml` ni
`grid.yaml`. Les deux candidats précédents ont été rejetés et leur étage de
détection de régime est explicitement hors dépendances : rattacher le hash gelé
du candidat n°3 à leurs fichiers le lierait à des paramètres sans influence sur
lui.

**L'anti-conditionnement du §8 est appliqué ici**, au chargement. Le signal —
`lookback_d`, `skip_d`, `n_legs`, l'univers, la fréquence — est hors de portée
de tout conditionnement adaptatif. Seul `gross_exposure_frac` est réductible par
une posture défensive. Une config qui prétendrait conditionner le signal fait
échouer le démarrage plutôt que de dériver en silence.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "momentum.yaml"

# Seul paramètre qu'une posture peut toucher (§8).
CONDITIONABLE = ("portfolio.gross_exposure_frac",)

# Tout ce qui définit le SIGNAL et l'univers : hors de portée, sans exception.
SIGNAL_FROZEN = (
    "signal.", "universe.", "portfolio.n_legs", "rebalance.every_d",
    "portfolio.hysteresis_rank",
)


class MomentumConfigError(ValueError):
    """Configuration incohérente ou interdite : on ne démarre pas."""


class SignalConditioningError(MomentumConfigError):
    """Tentative de conditionner le signal — interdit par le §8."""


def assert_no_signal_conditioning(paths) -> None:
    """§8 : le signal est hors de portée de tout conditionnement.

    Même principe que `assert_no_grid_feedback()` du candidat n°2, et même
    raison de fond : un signal dont les paramètres bougent selon l'état du
    marché n'est plus le signal qu'on a gelé, et le hash cesse de décrire ce qui
    tourne. Ici la contrainte est même plus stricte — il n'y a aucune boucle à
    craindre, simplement l'exigence que l'hypothèse testée reste celle qui a été
    enregistrée au registre.
    """
    bad = [p for p in paths if p not in CONDITIONABLE]
    if bad:
        raise SignalConditioningError(
            f"conditionnement interdit sur {sorted(bad)} : le §8 ne permet de "
            f"réduire que {CONDITIONABLE}. Le signal, l'univers et la fréquence "
            f"sont gelés — les conditionner reviendrait à tester une autre "
            f"hypothèse que celle enregistrée au registre")


@dataclass(frozen=True)
class UniverseConfig:
    basket_size: int = 10
    liquidity_lookback_d: int = 30
    max_gap_bars: int = 12
    exclusions: tuple = ("stables", "rebasing")

    def validate(self) -> None:
        if self.basket_size < 4:
            raise MomentumConfigError(
                "universe: basket_size ≥ 4 — en dessous, un long/short à 3 jambes "
                "n'a plus de sélection cross-sectionnelle, c'est tout l'univers")
        if self.liquidity_lookback_d < 1:
            raise MomentumConfigError("universe: liquidity_lookback_d ≥ 1")
        if self.max_gap_bars < 0:
            raise MomentumConfigError("universe: max_gap_bars ≥ 0")


@dataclass(frozen=True)
class SignalConfig:
    lookback_d: int = 21
    skip_d: int = 2

    def validate(self) -> None:
        if self.lookback_d < 2:
            raise MomentumConfigError("signal: lookback_d ≥ 2")
        if self.skip_d < 0:
            raise MomentumConfigError("signal: skip_d ≥ 0")
        if self.skip_d >= self.lookback_d:
            raise MomentumConfigError(
                "signal: skip_d doit être < lookback_d — sinon la fenêtre de "
                "mesure est vide et le score n'existe pas")

    @property
    def total_days(self) -> int:
        """Jours d'historique nécessaires pour un score : lookback + skip."""
        return self.lookback_d + self.skip_d


@dataclass(frozen=True)
class PortfolioConfig:
    n_legs: int = 3
    gross_exposure_frac: float = 1.0
    max_weight_per_asset: float = 0.20
    hysteresis_rank: int = 2

    def validate(self) -> None:
        if self.n_legs < 1:
            raise MomentumConfigError("portfolio: n_legs ≥ 1")
        if not 0 < self.gross_exposure_frac <= 3:
            raise MomentumConfigError("portfolio: gross_exposure_frac dans ]0, 3]")
        if not 0 < self.max_weight_per_asset <= 1:
            raise MomentumConfigError("portfolio: max_weight_per_asset dans ]0, 1]")
        if self.hysteresis_rank < 0:
            raise MomentumConfigError("portfolio: hysteresis_rank ≥ 0")


@dataclass(frozen=True)
class RebalanceConfig:
    every_d: int = 2
    hour_utc: int = 8
    exec_timeout_min: int = 30
    requote_s: int = 60

    def validate(self) -> None:
        if self.every_d < 1:
            raise MomentumConfigError("rebalance: every_d ≥ 1")
        if not 0 <= self.hour_utc <= 23:
            raise MomentumConfigError("rebalance: hour_utc dans [0, 23]")


@dataclass(frozen=True)
class RiskConfig:
    max_drawdown_pct: float = 0.40
    max_leverage: float = 1.5

    def validate(self) -> None:
        if not 0 < self.max_drawdown_pct < 1:
            raise MomentumConfigError("risk: max_drawdown_pct dans ]0, 1[")
        if self.max_leverage <= 0:
            raise MomentumConfigError("risk: max_leverage > 0")


@dataclass(frozen=True)
class FeesConfig:
    maker_bps: float = 1.5
    taker_bps: float = 4.5

    def validate(self) -> None:
        if min(self.maker_bps, self.taker_bps) < 0:
            raise MomentumConfigError("fees: frais négatifs")

    @property
    def maker(self) -> float:
        return self.maker_bps / 10_000.0

    @property
    def taker(self) -> float:
        return self.taker_bps / 10_000.0


@dataclass(frozen=True)
class DataConfig:
    market: str = "binance_perp_usdm"
    signal_timeframe: str = "1d"
    exec_timeframe: str = "1h"
    funding_source: str = "binance_perp"

    def validate(self) -> None:
        if self.market != "binance_perp_usdm":
            raise MomentumConfigError(
                f"data: marché {self.market!r} non géré — le §9.1 amendé impose les "
                f"perps USD-M (un univers spot rendrait la jambe short "
                f"inexécutable historiquement)")


@dataclass(frozen=True)
class PlaceboConfig:
    n_draws: int = 60
    alpha: float = 0.0167
    method: str = "persistent_score_permutation"

    def validate(self) -> None:
        if self.method != "persistent_score_permutation":
            raise MomentumConfigError(
                f"placebo: méthode {self.method!r} — le §9.2 amendé impose la "
                f"permutation PERSISTANTE. Une permutation par date détruirait la "
                f"persistance du classement, l'hystérésis ne retiendrait rien, et "
                f"le placebo paierait un multiple des frais du réel")
        if 1.0 / (self.n_draws + 1) >= self.alpha:
            raise MomentumConfigError(
                f"placebo: n_draws={self.n_draws} trop faible pour α={self.alpha} "
                f"(p minimale atteignable = {1.0/(self.n_draws+1):.4f}) — "
                f"il en faut au moins {int(1.0/self.alpha)}")


@dataclass(frozen=True)
class AcceptanceConfig:
    min_profit_factor: float = 1.2
    max_drawdown_pct: float = 0.45
    max_fee_ratio: float = 0.20
    min_rebalances: int = 100


@dataclass(frozen=True)
class SensitivityConfig:
    params: tuple = ("signal.lookback_d", "signal.skip_d",
                     "portfolio.n_legs", "rebalance.every_d")
    deltas: tuple = (-0.2, 0.2)


@dataclass(frozen=True)
class WindowConfig:
    label: str
    days: int
    end_ms: Optional[int] = None


@dataclass(frozen=True)
class BacktestConfig:
    initial_equity: float = 10_000.0
    windows: tuple = ()
    placebo: PlaceboConfig = None            # type: ignore[assignment]
    sensitivity: SensitivityConfig = None    # type: ignore[assignment]
    acceptance: AcceptanceConfig = None      # type: ignore[assignment]

    def validate(self) -> None:
        if len(self.windows) < 2:
            raise MomentumConfigError(
                "backtest: le §9.3 exige DEUX fenêtres (récente et 2021-2023) — "
                "une stratégie validée sur un seul régime ne l'est pas")
        self.placebo.validate()


@dataclass(frozen=True)
class MomentumConfig:
    universe: UniverseConfig = None        # type: ignore[assignment]
    signal: SignalConfig = None            # type: ignore[assignment]
    portfolio: PortfolioConfig = None      # type: ignore[assignment]
    rebalance: RebalanceConfig = None      # type: ignore[assignment]
    risk: RiskConfig = None                # type: ignore[assignment]
    fees: FeesConfig = None                # type: ignore[assignment]
    data: DataConfig = None                # type: ignore[assignment]
    backtest: BacktestConfig = None        # type: ignore[assignment]

    def validate(self) -> None:
        for f in fields(self):
            section = getattr(self, f.name)
            if is_dataclass(section) and hasattr(section, "validate"):
                section.validate()
        if self.portfolio.n_legs * 2 > self.universe.basket_size:
            raise MomentumConfigError(
                f"portfolio: {self.portfolio.n_legs} jambes de chaque côté exigent "
                f"un panier ≥ {self.portfolio.n_legs * 2}, or basket_size="
                f"{self.universe.basket_size} — les jambes se chevaucheraient")

    def replace_path(self, dotted: str, value: Any) -> "MomentumConfig":
        raw = self.to_dict()
        node, parts = raw, dotted.split(".")
        for p in parts[:-1]:
            if p not in node:
                raise MomentumConfigError(f"chemin inconnu : {dotted}")
            node = node[p]
        if parts[-1] not in node:
            raise MomentumConfigError(f"chemin inconnu : {dotted}")
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
    "universe": UniverseConfig, "signal": SignalConfig,
    "portfolio": PortfolioConfig, "rebalance": RebalanceConfig,
    "risk": RiskConfig, "fees": FeesConfig, "data": DataConfig,
}


def from_dict(raw: Dict[str, Any]) -> MomentumConfig:
    raw = copy.deepcopy(raw or {})
    kwargs: Dict[str, Any] = {}
    for key, cls in _SECTIONS.items():
        kwargs[key] = _build(cls, raw.pop(key, {}) or {}, key,
                             tuples=("exclusions",))

    bt = raw.pop("backtest", {}) or {}
    windows = tuple(WindowConfig(label=w["label"], days=int(w["days"]),
                                 end_ms=w.get("end_ms"))
                    for w in (bt.pop("windows", []) or []))
    kwargs["backtest"] = BacktestConfig(
        initial_equity=float(bt.pop("initial_equity", 10_000.0)),
        windows=windows,
        placebo=_build(PlaceboConfig, bt.pop("placebo", {}) or {}, "backtest.placebo"),
        sensitivity=_build(SensitivityConfig, bt.pop("sensitivity", {}) or {},
                           "backtest.sensitivity", tuples=("params", "deltas")),
        acceptance=_build(AcceptanceConfig, bt.pop("acceptance", {}) or {},
                          "backtest.acceptance"),
    )
    if bt:
        raise MomentumConfigError(f"backtest: clés inconnues {sorted(bt)}")
    if raw:
        raise MomentumConfigError(f"momentum: clés inconnues {sorted(raw)}")

    cfg = MomentumConfig(**kwargs)
    cfg.validate()
    return cfg


def load(path: Optional[Path] = None) -> MomentumConfig:
    import yaml

    p = Path(path) if path else DEFAULT_PATH
    if not p.exists():
        raise MomentumConfigError(f"configuration introuvable : {p}")
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if "momentum" not in doc:
        raise MomentumConfigError(f"{p} : clé racine `momentum:` manquante")
    return from_dict(doc["momentum"])


def _build(cls, values: Dict[str, Any], label: str, tuples: tuple = ()):
    known = {f.name: f for f in fields(cls)}
    unknown = set(values) - set(known)
    if unknown:
        raise MomentumConfigError(f"{label}: clés inconnues {sorted(unknown)}")
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
            raise MomentumConfigError(f"{label}.{name}: valeur invalide {value!r}") from exc
    return cls(**coerced)


def _as_dict(obj: Any) -> Any:
    if is_dataclass(obj):
        return {f.name: _as_dict(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, tuple):
        return [_as_dict(x) for x in obj]
    if isinstance(obj, list):
        return [_as_dict(x) for x in obj]
    return obj


__all__ = ["AcceptanceConfig", "BacktestConfig", "CONDITIONABLE", "DataConfig",
           "FeesConfig", "MomentumConfig", "MomentumConfigError", "PlaceboConfig",
           "PortfolioConfig", "RebalanceConfig", "RiskConfig", "SIGNAL_FROZEN",
           "SensitivityConfig", "SignalConditioningError", "SignalConfig",
           "UniverseConfig", "WindowConfig", "assert_no_signal_conditioning",
           "from_dict", "load"]

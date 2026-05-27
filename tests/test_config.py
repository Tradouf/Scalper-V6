"""Test du chargement et de la validation de la configuration V7."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.config import (
    DEFAULT_ALLOCATION_PATH,
    REPO_ROOT,
    V7Config,
    load_config,
)
from core.types import Regime


def test_default_config_loads():
    """config/allocation.yaml doit être chargeable sans erreur."""
    assert DEFAULT_ALLOCATION_PATH.exists(), f"Manquant : {DEFAULT_ALLOCATION_PATH}"
    cfg = load_config()
    assert isinstance(cfg, V7Config)


def test_matrix_b_has_all_regimes():
    cfg = load_config()
    for r in Regime:
        assert r in cfg.allocation.base_weights, f"Régime {r} manquant dans base_weights"


def test_enabled_strategies_have_weights():
    """Chaque stratégie activée doit avoir un poids dans la matrice."""
    cfg = load_config()
    matrix_strats = set()
    for d in cfg.allocation.base_weights.values():
        matrix_strats.update(d.keys())
    if cfg.strategies.grid.enabled:
        assert "grid" in matrix_strats
    if cfg.strategies.mean_reversion.enabled:
        assert "mean_reversion" in matrix_strats
    if cfg.strategies.momentum.enabled:
        assert "momentum" in matrix_strats


def test_bounds(tmp_path: Path):
    """Bornes sur multipliers : mult_min < mult_max."""
    cfg = load_config()
    assert cfg.allocation.mult_min < cfg.allocation.mult_max


def test_invalid_proba_sum_rejected(tmp_path: Path):
    """Une matrice B avec un régime manquant doit être rejetée."""
    invalid = {
        "symbols": ["BTC"],
        "allocation": {
            "base_weights": {
                "range": {"grid": 1.0},  # seul régime
            },
            "mult_min": 0.3,
            "mult_max": 1.5,
            "perf_halflife_days": 30,
            "vol_target": 0.1,
        },
    }
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(invalid))
    with pytest.raises(Exception):  # ValueError ou ValidationError
        load_config(path)


def test_negative_weight_rejected(tmp_path: Path):
    """Un poids négatif doit être rejeté."""
    invalid = {
        "symbols": ["BTC"],
        "allocation": {
            "base_weights": {
                "trend_up": {"grid": -1.0, "mean_reversion": 0.2, "momentum": 1.0},
                "trend_down": {"grid": 0.1, "mean_reversion": 0.2, "momentum": 1.0},
                "range": {"grid": 1.0, "mean_reversion": 1.0, "momentum": 0.1},
                "high_vol": {"grid": 0.2, "mean_reversion": 0.3, "momentum": 0.3},
            },
            "mult_min": 0.3,
            "mult_max": 1.5,
            "perf_halflife_days": 30,
            "vol_target": 0.1,
        },
    }
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(invalid))
    with pytest.raises(Exception):
        load_config(path)


def test_symbols_present():
    cfg = load_config()
    assert len(cfg.symbols) > 0
    assert "BTC" in cfg.symbols


def test_paths_resolve():
    cfg = load_config()
    # data/historical existe (créé en P-1)
    assert (REPO_ROOT / cfg.paths.data_historical).exists()

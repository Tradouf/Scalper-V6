"""
Configuration SimpleBot — tout est surchargeable par variable d'environnement.
Indépendant de config/settings.py (la V6 et SimpleBot ne partagent rien).
"""

from __future__ import annotations

import os
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent / "state"

BEST_PARAMS_FILE = STATE_DIR / "best_params.json"
OPTIMIZER_HISTORY_FILE = STATE_DIR / "optimizer_history.jsonl"
LIVE_STATE_FILE = STATE_DIR / "live_state.json"


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ── Univers ──────────────────────────────────────────────────────────────────
SYMBOLS = [s.strip() for s in _env_str("SIMPLEBOT_SYMBOLS", "BTC,ETH,SOL").split(",") if s.strip()]
INTERVAL = _env_str("SIMPLEBOT_INTERVAL", "15m")

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}[INTERVAL]

# ── Backtest / optimisation ──────────────────────────────────────────────────
BACKTEST_DAYS = _env_int("SIMPLEBOT_BACKTEST_DAYS", 21)
OPTIMIZE_INTERVAL_SEC = _env_int("SIMPLEBOT_OPTIMIZE_INTERVAL_SEC", 6 * 3600)

# Split walk-forward : la grille est classée sur le train, le set retenu doit
# confirmer sur la fenêtre de validation (les VALIDATION_RATIO derniers %).
VALIDATION_RATIO = _env_float("SIMPLEBOT_VALIDATION_RATIO", 0.30)
TOP_K_VALIDATION = _env_int("SIMPLEBOT_TOP_K_VALIDATION", 10)
MIN_TRAIN_TRADES = _env_int("SIMPLEBOT_MIN_TRAIN_TRADES", 5)
MIN_VALID_TRADES = _env_int("SIMPLEBOT_MIN_VALID_TRADES", 2)
MIN_VALID_PF = _env_float("SIMPLEBOT_MIN_VALID_PF", 1.2)

# Coûts par side (fill taker + glissement) appliqués au backtest ET au sizing.
FEE_PCT = _env_float("SIMPLEBOT_FEE_PCT", 0.00045)
SLIPPAGE_PCT = _env_float("SIMPLEBOT_SLIPPAGE_PCT", 0.0003)

# ── Live ─────────────────────────────────────────────────────────────────────
# DRY-RUN PAR DÉFAUT : le live réel exige SIMPLEBOT_DRY_RUN=0 explicitement.
DRY_RUN = _env_str("SIMPLEBOT_DRY_RUN", "1") not in ("0", "false", "False")

LEVERAGE = _env_int("SIMPLEBOT_LEVERAGE", 3)
MARGIN_PCT = _env_float("SIMPLEBOT_MARGIN_PCT", 0.05)   # marge par trade en % de l'account value
MIN_NOTIONAL_USD = 11.0                                  # minimum HL = $10, marge d'arrondi
MAX_OPEN_POSITIONS = _env_int("SIMPLEBOT_MAX_OPEN_POSITIONS", 3)
LOOP_SEC = _env_int("SIMPLEBOT_LOOP_SEC", 30)

# Second wallet — NE PAS réutiliser le wallet de la V6.
ENV_PRIVATE_KEY = "HL2_PRIVATE_KEY"
ENV_ACCOUNT_ADDRESS = "HL2_ACCOUNT_ADDRESS"

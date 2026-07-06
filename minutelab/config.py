"""
Configuration MinuteLab. Tout est surchargeable par variable d'environnement
MINUTELAB_*.
"""

from __future__ import annotations

import os


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


SYMBOL = os.environ.get("MINUTELAB_SYMBOL", "BTC")

# --- Coûts (identiques aux hypothèses SimpleBot : taker + slippage par côté) ---
FEE_PCT = _env_float("MINUTELAB_FEE_PCT", 0.00045)
SLIPPAGE_PCT = _env_float("MINUTELAB_SLIPPAGE_PCT", 0.0003)

# --- Fenêtres de sélection ---
LOOKBACK_MIN = _env_int("MINUTELAB_LOOKBACK_MIN", 60)    # fenêtre de backtest
RECENT_MIN = _env_int("MINUTELAB_RECENT_MIN", 20)        # sous-fenêtre qui doit être gagnante
WARMUP_HOURS = _env_float("MINUTELAB_WARMUP_HOURS", 6.0) # préfixe pour chauffer les indicateurs
MIN_TRADES = _env_int("MINUTELAB_MIN_TRADES", 2)         # trades minimum sur la fenêtre

# --- Rythme de réévaluation (adaptatif) ---
RESELECT_START_MIN = _env_int("MINUTELAB_RESELECT_START_MIN", 15)
RESELECT_MIN_MIN = _env_int("MINUTELAB_RESELECT_MIN_MIN", 5)
RESELECT_MAX_MIN = _env_int("MINUTELAB_RESELECT_MAX_MIN", 30)

# --- Sortie : le gain croise sous sa moyenne, échantillonné toutes les 5 s ---
EXIT_SAMPLE_SEC = _env_float("MINUTELAB_EXIT_SAMPLE_SEC", 5.0)
EXIT_MA_SAMPLES = _env_int("MINUTELAB_EXIT_MA_SAMPLES", 12)   # 12 × 5 s = 1 min
EXIT_WARMUP_SAMPLES = _env_int("MINUTELAB_EXIT_WARMUP_SAMPLES", 3)
# Le croisement PnL/MA ne coupe que si le gain couvre déjà les frais aller-
# retour (sinon on tient : stop dur et durée max restent les garde-fous).
EXIT_REQUIRE_NET_GAIN = _env_int("MINUTELAB_EXIT_REQUIRE_NET_GAIN", 1)

# --- Garde-fous de position (le PnL/MA reste la sortie principale) ---
HARD_SL_PCT = _env_float("MINUTELAB_HARD_SL_PCT", 0.004)  # -0.4 % prix
MAX_HOLD_MIN = _env_int("MINUTELAB_MAX_HOLD_MIN", 30)

# --- État / journaux ---
STATE_DIR = os.environ.get(
    "MINUTELAB_STATE_DIR",
    os.path.join(os.path.dirname(__file__), "state"),
)

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
ROUND_TRIP_COST = 2.0 * (FEE_PCT + SLIPPAGE_PCT)

# --- Mode de qualification ---
# pulse : fenêtre unique ou récente dominante + seuils d'edge (défaut)
# dual  : gagnant sur lookback ET recent (ancien 60/20)
# legacy: pnl_pct > 0 et pnl_recent_pct > 0 sans seuil d'edge
QUAL_MODE = os.environ.get("MINUTELAB_QUAL_MODE", "pulse")

# --- Fenêtres de sélection ---
LOOKBACK_MIN = _env_int("MINUTELAB_LOOKBACK_MIN", 20)    # fenêtre de backtest
RECENT_MIN = _env_int("MINUTELAB_RECENT_MIN", 20)        # sous-fenêtre récente
WARMUP_HOURS = _env_float("MINUTELAB_WARMUP_HOURS", 6.0) # préfixe pour chauffer les indicateurs
MIN_TRADES = _env_int("MINUTELAB_MIN_TRADES", 2)         # trades minimum sur la fenêtre

# Seuils d'edge (% prix, hors levier) — liés au coût aller-retour
MIN_PNL_RECENT_PCT = _env_float(
    "MINUTELAB_MIN_PNL_RECENT_PCT", ROUND_TRIP_COST * 0.3)
MIN_PNL_FULL_PCT = _env_float(
    "MINUTELAB_MIN_PNL_FULL_PCT", ROUND_TRIP_COST * 0.5)
MIN_SCORE_PCT = _env_float(
    "MINUTELAB_MIN_SCORE_PCT", ROUND_TRIP_COST * 0.4)
MIN_PROFIT_FACTOR = _env_float("MINUTELAB_MIN_PROFIT_FACTOR", 1.15)

# --- Hystérésis champion ---
CHAMPION_MIN_TENURE_MIN = _env_int("MINUTELAB_CHAMPION_MIN_TENURE_MIN", 10)
CHAMPION_SCORE_MARGIN_PCT = _env_float(
    "MINUTELAB_CHAMPION_SCORE_MARGIN_PCT", 0.0003)
CHAMPION_GRACE_SCANS = _env_int("MINUTELAB_CHAMPION_GRACE_SCANS", 2)
CHAMPION_DEMOTE_PNL_PCT = _env_float(
    "MINUTELAB_CHAMPION_DEMOTE_PNL_PCT", -0.002)

# --- Rythme de réévaluation (adaptatif) ---
RESELECT_START_MIN = _env_int("MINUTELAB_RESELECT_START_MIN", 15)
RESELECT_MIN_MIN = _env_int("MINUTELAB_RESELECT_MIN_MIN", 10)
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

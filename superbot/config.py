"""
Configuration SuperBot — tout est surchargeable par variable d'environnement
SUPERBOT_* (voir SPEC.md §10). Indépendant de simplebot/config.py et de
config/settings.py : les trois bots ne partagent AUCUN état.

Wallet : HL3_PRIVATE_KEY / HL3_ACCOUNT_ADDRESS — troisième wallet, le live
(Phase 2) refuse de démarrer s'il est identique à HL_* (V6) ou HL2_* (SimpleBot).
"""

from __future__ import annotations

import os
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent / "state"
HMM_DIR = STATE_DIR / "hmm"

BEST_PARAMS_FILE = STATE_DIR / "best_params.json"
OPTIMIZER_HISTORY_FILE = STATE_DIR / "optimizer_history.jsonl"
LIVE_STATE_FILE = STATE_DIR / "live_state.json"
REGIME_MARKET_FILE = STATE_DIR / "regime_market.json"
REGIME_SYMBOLS_FILE = STATE_DIR / "regime_symbols.json"


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


def _env_bool(name: str, default: str) -> bool:
    return _env_str(name, default) not in ("0", "false", "False")


def _env_csv(name: str, default: str = "") -> list:
    raw = os.environ.get(name, default)
    if not raw.strip():
        return []
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


# ── Wallet (3ᵉ wallet — jamais HL_* ni HL2_*) ────────────────────────────────
ENV_PRIVATE_KEY = "HL3_PRIVATE_KEY"
ENV_ACCOUNT_ADDRESS = "HL3_ACCOUNT_ADDRESS"
FORBIDDEN_WALLET_ENVS = (
    ("HL_PRIVATE_KEY", "HL_ACCOUNT_ADDRESS"),      # V6
    ("HL2_PRIVATE_KEY", "HL2_ACCOUNT_ADDRESS"),    # SimpleBot
)

# ── Mode ─────────────────────────────────────────────────────────────────────
# DRY-RUN PAR DÉFAUT : le live réel exigera SUPERBOT_DRY_RUN=0 explicitement.
DRY_RUN = _env_bool("SUPERBOT_DRY_RUN", "1")

# ── Univers ──────────────────────────────────────────────────────────────────
# "ALL" → top-N perps HL par volume notionnel 24h (résolution via simplebot.data,
# fallback socle liquide si réseau HS). Liste CSV sinon.
_SYMBOLS_FALLBACK = ["BTC", "ETH", "SOL"]
MAX_SYMBOLS = _env_int("SUPERBOT_MAX_SYMBOLS", 40)


def _resolve_symbols(raw: str) -> list:
    if raw.strip().upper() != "ALL":
        return [s.strip() for s in raw.split(",") if s.strip()]
    try:
        from simplebot.data import fetch_perp_universe
        names = fetch_perp_universe(top_n=MAX_SYMBOLS or None)
        if names:
            return names
    except Exception as e:
        import logging
        logging.getLogger("sdm.superbot.config").warning(
            "Univers ALL indisponible (%r) → fallback %s", e, _SYMBOLS_FALLBACK
        )
    return list(_SYMBOLS_FALLBACK)


SYMBOLS = _resolve_symbols(_env_str("SUPERBOT_SYMBOLS", "ALL"))

# ── Timeframes (SPEC §3B : jamais < 15m, 4h réservé au momentum) ─────────────
EMA_TIMEFRAMES = ["15m", "1h"]
INTERVAL_MS = {
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
}
# candleSnapshot plafonne à ~5000 bougies/requête → jours effectifs par TF.
# (15m : 5000 bougies ≈ 52 j — on borne à 50 pour rester sous le cap sans paginer.)
MAX_FETCH_DAYS = {"15m": 50, "1h": 200, "4h": 800}

# ── Backtest / walk-forward (SPEC §3B et §7) ─────────────────────────────────
BACKTEST_DAYS = _env_int("SUPERBOT_BACKTEST_DAYS", 60)
OPTIMIZE_INTERVAL_SEC = _env_int("SUPERBOT_OPTIMIZE_INTERVAL_SEC", 14_400)  # 4h

VALIDATION_RATIO = _env_float("SUPERBOT_VALIDATION_RATIO", 0.30)
TOP_K_VALIDATION = _env_int("SUPERBOT_TOP_K_VALIDATION", 10)
# Gates train (SPEC §7) : n>=5, PF>=1.0, PnL>0
MIN_TRAIN_TRADES = _env_int("SUPERBOT_MIN_TRAIN_TRADES", 5)
MIN_TRAIN_PF = _env_float("SUPERBOT_MIN_TRAIN_PF", 1.0)
# Gates valid (SPEC §3B/§7) : n>=5, PF>=1.2, PnL>0 — filtre BINAIRE, le premier
# set du classement train qui confirme gagne. JAMAIS de sélection sur PnL valid.
MIN_VALID_TRADES = _env_int("SUPERBOT_MIN_VALID_TRADES", 5)
MIN_VALID_PF = _env_float("SUPERBOT_MIN_VALID_PF", 1.2)

# Filtre directionnel FIXE (SPEC §1 : +23% d'edge à params figés, overfit si
# mis dans la grille — leçon R&D simplebot 07/2026). Hors grille, non optimisé.
TREND_EMA_FIXED = 200

# ── Frais / exécution backtest (SPEC §7) ─────────────────────────────────────
FEE_PCT = _env_float("SUPERBOT_FEE_PCT", 0.00045)          # taker
FEE_MAKER_PCT = _env_float("SUPERBOT_FEE_MAKER_PCT", 0.00015)
SLIPPAGE_PCT = _env_float("SUPERBOT_SLIPPAGE_PCT", 0.0003)
ENTRY_MODE = _env_str("SUPERBOT_ENTRY_MODE", "maker")       # maker-first partout

# ── Filtre qualité symboles (SPEC §3B) ───────────────────────────────────────
SYMBOL_ALLOWLIST = _env_csv("SUPERBOT_SYMBOL_ALLOWLIST")
SYMBOL_BLOCKLIST = _env_csv("SUPERBOT_SYMBOL_BLOCKLIST")
MAX_ACTIVE_SYMBOLS = _env_int("SUPERBOT_MAX_ACTIVE_SYMBOLS", 12)
QUALITY_MIN_VALID_PF = _env_float("SUPERBOT_QUALITY_MIN_VALID_PF", 1.30)
QUALITY_MIN_VALID_PNL_PCT = _env_float("SUPERBOT_QUALITY_MIN_VALID_PNL_PCT", 0.015)
QUALITY_MIN_VALID_WINRATE = _env_float("SUPERBOT_QUALITY_MIN_VALID_WINRATE", 0.38)
QUALITY_MIN_TRAIN_PF = _env_float("SUPERBOT_QUALITY_MIN_TRAIN_PF", 1.03)

# ── Allocations sleeves (SPEC §3 — doivent sommer à 1.0) ─────────────────────
MOMENTUM_ALLOC = _env_float("SUPERBOT_MOMENTUM_ALLOC", 0.35)
EMA_ALLOC = _env_float("SUPERBOT_EMA_ALLOC", 0.45)
BREAKOUT_ALLOC = _env_float("SUPERBOT_BREAKOUT_ALLOC", 0.20)

# ── Risque (SPEC §5 — consommé en Phase 2) ───────────────────────────────────
LOOP_SEC = _env_int("SUPERBOT_LOOP_SEC", 30)  # période de la boucle live
LEVERAGE = _env_int("SUPERBOT_LEVERAGE", 3)
MIN_NOTIONAL_USD = 11.0                       # minimum HL = $10, marge d'arrondi
MARGIN_PCT = _env_float("SUPERBOT_MARGIN_PCT", 0.04)
MARGIN_PCT_MAX = _env_float("SUPERBOT_MARGIN_PCT_MAX", 0.07)
DAILY_LOSS_LIMIT_PCT = _env_float("SUPERBOT_DAILY_LOSS_LIMIT_PCT", 0.03)
PORTFOLIO_DD_LIMIT = _env_float("SUPERBOT_PORTFOLIO_DD_LIMIT", 0.08)
KILL_CONFIRMATIONS = _env_int("SUPERBOT_KILL_CONFIRMATIONS", 2)
MAX_OPEN_TOTAL = _env_int("SUPERBOT_MAX_OPEN_TOTAL", 10)
MAX_OPEN_PER_SLEEVE = {"momentum": 6, "adaptive_ema": 5, "breakout": 3}
MAX_SAME_DIRECTION = _env_int("SUPERBOT_MAX_SAME_DIRECTION", 6)
FLIP_COOLDOWN_BARS = _env_int("SUPERBOT_FLIP_COOLDOWN_BARS", 2)
EXEC_MAKER_FIRST = _env_bool("SUPERBOT_EXEC_MAKER_FIRST", "1")

# ── Sleeve A — Momentum 4h (SPEC §3A — params FIGÉS, jamais optimisés) ───────
MOMENTUM_ROC_BARS = 12            # ROC sur 48h (12 bougies 4h)
MOMENTUM_THR = 0.02               # seuil ±2 %
MOMENTUM_SL_ATR = 2.0             # SL natif 2×ATR(14)
MOMENTUM_TIME_EXIT_BARS = 72      # 12 jours
# Filtres live obligatoires (SPEC §3A) : on ne paie pas la foule.
MOMENTUM_FUNDING_GATE = _env_float("SUPERBOT_MOMENTUM_FUNDING_GATE", 0.0001)  # ±0.01%/h
MAX_SPREAD_PCT = _env_float("SUPERBOT_MAX_SPREAD_PCT", 0.0015)                # 0.15 %

# ── HMM (SPEC §4 — consommé en Phase 2) ──────────────────────────────────────
HMM_MARKET_STATES = _env_int("SUPERBOT_HMM_MARKET_STATES", 4)
HMM_SYMBOL_STATES = _env_int("SUPERBOT_HMM_SYMBOL_STATES", 3)
HMM_MARKET_MIN_CONF = _env_float("SUPERBOT_HMM_MARKET_MIN_CONF", 0.55)
HMM_SYMBOL_MIN_CONF = _env_float("SUPERBOT_HMM_SYMBOL_MIN_CONF", 0.42)
HMM_CHOPPY_MIN_CONF = _env_float("SUPERBOT_HMM_CHOPPY_MIN_CONF", 0.42)
HMM_CHOPPY_SIZE_MULT = _env_float("SUPERBOT_HMM_CHOPPY_SIZE_MULT", 0.35)
HMM_MARKET_DAYS = _env_int("SUPERBOT_HMM_MARKET_DAYS", 180)
HMM_SYMBOL_DAYS = _env_int("SUPERBOT_HMM_SYMBOL_DAYS", 90)
HMM_TRANSITION_FREEZE = _env_float("SUPERBOT_HMM_TRANSITION_FREEZE", 0.58)

# ── Débit API (anti-429 — mêmes leçons que simplebot) ────────────────────────
FETCH_THROTTLE_SEC = _env_float("SUPERBOT_FETCH_THROTTLE_SEC", 0.35)

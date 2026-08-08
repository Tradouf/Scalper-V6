"""Configuration LLMBot — wallet HL3, env LLMBOT_*."""

from __future__ import annotations

import os
from pathlib import Path


def _resolve_state_dir(env_key: str, default: Path) -> Path:
    """STATE_DIR surchargeable (A/B paper isolé via LLMBOT_STATE_DIR)."""
    raw = (os.environ.get(env_key) or "").strip()
    if raw:
        p = Path(raw).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p
    default.mkdir(parents=True, exist_ok=True)
    return default


STATE_DIR = _resolve_state_dir(
    "LLMBOT_STATE_DIR",
    Path(__file__).resolve().parent / "state",
)
LIVE_STATE_FILE = STATE_DIR / "live_state.json"
DECISIONS_FILE = STATE_DIR / "decisions.jsonl"
PAPER_START_EQUITY = float(os.environ.get("LLMBOT_PAPER_START_EQUITY", "200") or 200)

LOCALAI_BASE_URL = os.environ.get("LOCALAI_BASE_URL", "http://localhost:8080/v1")
MODEL_TRADER = os.environ.get("LLMBOT_MODEL_TRADER", "qwen2.5-7b-trader")
MODEL_MACRO = os.environ.get("LLMBOT_MODEL_MACRO", "qwen3.5-9b")

DRY_RUN = os.environ.get("LLMBOT_DRY_RUN", "1") not in ("0", "false", "False")
LOOP_SEC = int(os.environ.get("LLMBOT_LOOP_SEC", "60"))
NEWS_REFRESH_SEC = int(os.environ.get("LLMBOT_NEWS_REFRESH_SEC", "900"))
MAX_LLM_TRADES_PER_CYCLE = int(os.environ.get("LLMBOT_MAX_LLM_PER_CYCLE", "3"))
MIN_QUANT_SCORE = float(os.environ.get("LLMBOT_MIN_QUANT_SCORE", "65"))
MIN_LLM_CONFIDENCE = float(os.environ.get("LLMBOT_MIN_LLM_CONF", "0.65"))

INTERVAL = os.environ.get("LLMBOT_INTERVAL", "15m")
INTERVAL_MS = {"15m": 900_000, "1h": 3_600_000}[INTERVAL]
CANDLES_LOOKBACK = int(os.environ.get("LLMBOT_CANDLES", "120"))

SYMBOLS_RAW = os.environ.get(
    "LLMBOT_SYMBOLS",
    "BTC,ETH,SOL,XPL,HYPE,LINK,AVAX,DOGE,XRP,AAVE,UNI,NEAR",
)
SYMBOLS = [s.strip() for s in SYMBOLS_RAW.split(",") if s.strip()]

LEVERAGE = int(os.environ.get("LLMBOT_LEVERAGE", "3"))
MARGIN_PCT = float(os.environ.get("LLMBOT_MARGIN_PCT", "0.05"))
MAX_OPEN_POSITIONS = int(os.environ.get("LLMBOT_MAX_OPEN", "4"))
TP_ROE_PCT = float(os.environ.get("LLMBOT_TP_ROE_PCT", "0.03"))
SL_ROE_PCT = float(os.environ.get("LLMBOT_SL_ROE_PCT", "0.015"))

FEE_PCT = float(os.environ.get("LLMBOT_FEE_PCT", "0.00045"))
SLIPPAGE_PCT = float(os.environ.get("LLMBOT_SLIPPAGE_PCT", "0.0003"))
EXEC_MAKER_FIRST = os.environ.get("LLMBOT_EXEC_MAKER_FIRST", "1") not in ("0", "false", "False")

KILL_LOSS_PCT = float(os.environ.get("LLMBOT_KILL_LOSS_PCT", "0.05"))
KILL_WINDOW_SEC = int(os.environ.get("LLMBOT_KILL_WINDOW_SEC", "86400"))
KILL_PAUSE_SEC = int(os.environ.get("LLMBOT_KILL_PAUSE_SEC", "86400"))

FREEZE_CONSEC_LOSSES = int(os.environ.get("LLMBOT_FREEZE_CONSEC_LOSSES", "2"))
FREEZE_SEC = int(os.environ.get("LLMBOT_FREEZE_SEC", "3600"))

ENV_PRIVATE_KEY = "HL3_PRIVATE_KEY"
ENV_ACCOUNT_ADDRESS = "HL3_ACCOUNT_ADDRESS"
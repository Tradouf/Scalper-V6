# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**SalleDesMarches** is a Python algorithmic trading bot for high-frequency scalping on the [Hyperliquid](https://hyperliquid.xyz) derivatives exchange. It uses a multi-agent system where each agent calls a locally-hosted LLM (via LocalAI) to make trading decisions. The current production version is V6 (`main_v6.py`).

## Setup & Run Commands

```bash
# Install dependencies in virtualenv
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure secrets
cp .env.example .env
# Edit .env: HL_PRIVATE_KEY, HL_ACCOUNT_ADDRESS, HL_NETWORK, WHALES_API_KEY, LOCALAI_BASE_URL

# Run live (preferred)
bash start_sdm.sh

# Run directly
python main_v6.py

# Backtests
python backtest/run_backtest_llm.py   # LLM-driven
python backtest/run_backtest.py        # Deterministic

# Unit tests
python -m pytest tests/test_core.py -v

# Post-trade analysis
python analyze_trades.py
```

## Architecture

### 30-Second Cycle (`main_v6.py` → `SalleDesMarchesV6`)

Every 30 seconds, the main loop:
1. Refreshes news (AgentNews via RSS) and whale signals (AgentWhales via API) every 15–20 min
2. Calls AgentOrchestrator to determine global market regime (trend + volatility)
3. Checks daily loss limit hard stop (-3% → exit all positions)
4. For each symbol in the watchlist (4 per cycle):
   - AgentTechnical computes indicators (RSI, MACD, Bollinger, ATR)
   - AgentOrderbook analyzes L2 book imbalance (no LLM)
   - `scalp_filter()` gates entry: ATR ≥ 0.6%, S&R distance ≥ 1.2%, spread < 0.1%
   - AgentBull and AgentBear each make LLM arguments
   - `compute_consensus()` merges signals deterministically
   - AgentScalper makes the final LLM decision (ENTER / MANAGE / EXIT)
   - AgentLearner adapts TP/SL multipliers from trade history
   - AgentTrader executes the order on Hyperliquid

Two independent threads run in parallel:
- `_hl_sync_loop()`: syncs Hyperliquid account state every 2s
- `_trail_loop()`: software trailing stop checks every 2s (no exchange-level SL)

### Multi-Agent System (`agents/`)

All agents extend `BaseAgent` (`agents/base_agent.py`), which provides:
- `_llm()`: serialized LLM calls via a global semaphore (one call at a time across all agents)
- `_parse_json()`: robust JSON extraction from LLM responses
- `_send_message()`: inter-agent messaging via shared memory

LLM endpoint: `http://localhost:8080/v1/chat/completions` (LocalAI). Two model tiers:
- Orchestrator: `qwen3.5-9b` (heavier reasoning)
- All other agents: `qwen2.5-7b-trader`

Key agents and their roles:

| Agent | LLM | Purpose |
|---|---|---|
| `agent_orchestrator.py` | Yes | Global market regime (bull/bear/range, volatility) |
| `agent_technical.py` | Yes | Technical indicator analysis |
| `agent_bull.py` / `agent_bear.py` | Yes | Debate: bullish vs bearish arguments |
| `agent_scalper.py` | Yes | Final trade decision (ENTER/MANAGE/EXIT) |
| `agent_news_v2.py` | Yes | RSS sentiment (CoinDesk, Cointelegraph, etc.) |
| `agent_whales.py` | Yes | On-chain whale movement analysis |
| `agent_orderbook.py` | No | L2 book imbalance |
| `agent_learner.py` | No | TP/SL profile adaptation from history |
| `agent_trader.py` | No | Order execution |
| `feature_engine.py` | No | Advanced feature computation |
| `regime_engine.py` | No | Local regime detection |

### Shared Memory (`memory/shared_memory.py`)

Thread-safe JSON store (file-backed, auto-saved after every write). Top-level keys:
- `market_analysis`: per-symbol technical + sentiment data
- `debate`: per-symbol bull/bear arguments and decisions
- `positions`: open positions with entry, SL, TP, PnL
- `risk_status`: drawdown, daily PnL, alerts
- `agent_signals`: signal log
- `trade_history`: completed trades
- `regime`: current global market regime
- `scalper_profiles`: per-symbol, per-regime TP/SL ATR multipliers (learned)

### Exchange Layer (`exchanges/hyperliquid.py`, `hyperliquid_client.py`)

`hyperliquid.py` wraps `hyperliquid_client.py` (WebSocket + REST). The client maintains a local cache synced every 2s by `_hl_sync_loop`. Key methods: `place_order()`, `place_tpsl_native()`, `get_positions()`, `get_ohlcv()`.

## Key Configuration (`config/settings.py`)

All tunable parameters live here. Critical values:

| Parameter | Default | Meaning |
|---|---|---|
| `SIMULATION_MODE` | `False` | Live trading by default |
| `SCAN_INTERVAL_SEC` | `30` | Main cycle period |
| `MAX_OPEN_POSITIONS` | `6` | Position cap |
| `DEFAULT_LEVERAGE` | `3` | Default leverage |
| `DAILY_LOSS_LIMIT_PCT` | `0.03` | Hard stop at -3% daily |
| `SCALP_TP_PNL_PCT` | `0.03` | Take-profit at +3% ROE |
| `SCALP_SL_PNL_PCT` | `0.015` | Stop-loss at -1.5% ROE |
| `SYMBOLS_PER_CYCLE` | `4` | Symbols processed per 30s |

Watchlist: BTC, ETH, SOL (scalp mode).

## Important Behaviors

- **Trailing stop is software-only**: `_trail_loop()` in `main_v6.py` manages trailing, no exchange SL order is placed. A crash during an open position leaves it unprotected.
- **LLM calls are serialized**: The global `_LLM_SEMAPHORE` in `base_agent.py` enforces one LLM call at a time. Adding concurrent LLM calls requires careful semaphore rethinking.
- **Freeze system**: AgentScalper tracks consecutive losses per symbol. After 2+ losses, it freezes that symbol for 1–4 hours.
- **Shared memory grows**: `memory/shared_memory.json` is append-heavy (838 KB+ in production). Read it with care; it is not pruned automatically.
- **`.env` is excluded from git**: Never commit secrets. `HL_NETWORK=mainnet` means real money.

## Logs

File: `logs/sdm.log` (rotating, 10 MB × 5). Logger names follow `sdm.<component>` (e.g., `sdm.scalper`, `sdm.main`). Set log level in `config/settings.py`.

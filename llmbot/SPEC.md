# LLMBot — Bot profitable centré LLM

> Remplace l'approche SuperBot 100% déterministe. Le LLM **décide les entrées**,
> mais seulement sur des setups **pré-filtrés** par le quant scanner.

## Pourquoi la V6 échouait (et comment LLMBot corrige)

| Problème V6 | Solution LLMBot |
|---|---|
| 6-8 appels LLM / symbole / 30s | **Max 3 appels LLM / cycle** (60s) |
| Consensus bloqué en range | Filtre quant score ≥ 65 avant tout LLM |
| Bull + Bear + Technical + Scalper | **1 agent trader** avec JSON structuré |
| Pas de backtest reproductible | Quant scanner backtestable ; LLM = couche décision |
| Frais taker | Maker-first (`simplebot/execution.py`) |
| Trail logiciel (crash = nu) | **TP/SL natifs exchange** |

## Architecture

```
Cycle 60s
  │
  ├─ News (1× LLM / 15 min) ──► veto macro block_longs/block_shorts
  │
  ├─ Quant Scanner (0 LLM) ──► score 0-100 par symbole
  │     RSI, MACD, EMA, ATR, S/R, orderbook imbalance
  │
  ├─ Top 3 setups (score ≥ 65)
  │     └─ Agent Trader (1 LLM / setup) ──► ENTER_LONG | ENTER_SHORT | WAIT
  │
  └─ Exécution maker-first + TP/SL natifs
```

## Fichiers

```
llmbot/
  config.py          # LLMBOT_*, wallet HL3
  llm.py             # client LocalAI
  indicators.py      # technique pure Python
  quant_scanner.py   # filtre pré-LLM
  news.py            # RSS + LLM macro
  agent_trader.py    # décision entrée
  live.py            # boucle principale
  run.py             # entrée
  state/             # live_state.json, decisions.jsonl
```

## Lancement

```bash
# Tests
python -m pytest tests/test_llmbot.py -v

# Paper (dry-run, LocalAI optionnel)
python -m llmbot.run

# Live (HL3 + LocalAI requis)
LLMBOT_DRY_RUN=0 python -m llmbot.run
# ou
bash start_llmbot.sh --live
```

## .env

```bash
HL3_PRIVATE_KEY=0x...
HL3_ACCOUNT_ADDRESS=0x...
LOCALAI_BASE_URL=http://localhost:8080/v1
LLMBOT_DRY_RUN=1
LLMBOT_MIN_QUANT_SCORE=65
LLMBOT_MIN_LLM_CONF=0.65
LLMBOT_MAX_LLM_PER_CYCLE=3
LLMBOT_SYMBOLS=BTC,ETH,SOL,XPL,...
```

## Modèles LocalAI

- `qwen2.5-7b-trader` — décisions trade (agent_trader)
- `qwen3.5-9b` — macro news (news.py)

## Métriques cibles

- ≤ 3 appels LLM / minute en conditions normales
- WR ≥ 40% paper 14j
- PF ≥ 1.3 paper 14j
- Si LLM down → bot continue en WAIT (pas de trade aveugle)
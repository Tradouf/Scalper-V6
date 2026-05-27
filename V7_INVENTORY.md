# Inventaire V6 → V7 — modules à porter, archiver, ré-écrire

Audit du code V6.2 à `~/SalleDesMarches_fixed/` (`a74ec2a`). Total ~6800 lignes Python.

## Classification

### 🟢 PORTER tel quel (ou wrapper léger)

| Module V6 | Lignes | Destination V7 | Notes |
|---|---:|---|---|
| `agents/grid_manager.py` | 723 | `strategies/grid.py` (wrap) | Garde la FSM complète + drift guard + health check + tick decimals fix. Wrap pour implémenter `StrategyAgent`. |
| `agents/agent_mean_reversion.py` | 212 | `strategies/mean_reversion.py` (wrap) | Garde l'algo z-score + half-life. Wrap pour `StrategyAgent`. |
| `utils/stats.py` | 83 | `utils/stats.py` | zscore, half_life, rolling_mean_std — utilitaire générique. |
| `memory/order_registry.py` | 280 | `execution/order_registry.py` | Réutilise pour Fill.strategy_id attribution. À étendre. |
| `hyperliquid_client.py` | 1594 | `execution/hyperliquid_client.py` | Couche basse HL — wallet, signing, place_order, get_open_orders, modify_stop_trigger_order. Garde tel quel, ajouter méthodes nécessaires. |
| `exchanges/base.py` | (~50) | `execution/types.py` | OrderRequest, CancelResult, OrderResult — base contractuelle. |
| `exchanges/hyperliquid.py` | (~340) | `execution/hyperliquid_adapter.py` | Wrapper de hyperliquid_client. |
| `scripts/orderflow_collector.py` | 217 | `data/orderflow_collector.py` | Continue de collecter, sortie alimentera les features. |
| `scripts/audit.sh` + `audit_metrics.sh` + `audit_prompt.md` | ~150 | `scripts/` (adapter prompt V7) | Garde la mécanique cron 6h, adapter le prompt pour reporter régime+poids+attribution. |
| `scripts/bot.sh` | 130 | `scripts/bot.sh` (adapter) | Adapter pour `main.py` V7. |

### 🟡 RÉ-ÉCRIRE / TRANSFORMER

| Module V6 | Action V7 | Notes |
|---|---|---|
| `agents/regime_engine.py` (673) | Ré-écrire en `regime/detector.py` + `regime/features.py` | Garder l'idée (features → décision) mais : sortie **probabiliste softmax** + test no-leak + hystérésis explicite. Reuse partiel possible pour les features. |
| `agents/risk_manager.py` (234) | Devient base de `risk/manager.py` | Caps existants restent. Ajouter projection sur `TargetPortfolio` + kill-switch DD. |
| `main_v6.py` (3505) | Découper et migrer | Le monolithe explose en :<br>• `main.py` (boucle clock-driven, < 200 lignes)<br>• `risk/manager.py` (trail loop + emergency exit, ~400 lignes)<br>• `execution/engine.py` (reconcile + place orders, ~300 lignes)<br>• `data/feed.py` (HL sync loop, ~200 lignes) |
| `dashboard.py` (718) | `monitoring/dashboard.py` (étendre) | Garde la base FastAPI. Ajoute panneaux : régime probabiliste, poids stratégies, attribution PnL. Port 8082 pendant dev, 8081 après cutover. |
| `agents/feature_engine.py` (741) | Partiellement réutilisable dans `regime/features.py` | Beaucoup de features non utiles. Garder ATR, slope, vol, autocorr. |
| `agents/multi_tf.py` (376) | Garde quelques fonctions dans `utils/multi_tf.py` | strate gate H1/M15/M1 → pourrait servir de feature secondaire ou de filtre dans Momentum/Breakout. |

### 🔴 ARCHIVER dans `legacy/v6/` (ne pas porter)

| Module V6 | Raison |
|---|---|
| `agents/base_agent.py` | Infrastructure LLM (semaphore, _llm, _parse_json). Plus utilisé en V7. |
| `agents/agent_orchestrator.py` | Décision LLM régime — remplacé par detector déterministe. |
| `agents/agent_scalper.py` | Pipeline LLM scalp — désactivé en V6.2, NET all-time -$32. |
| `agents/agent_technical.py` | Analyse LLM features — remplacé par features déterministes dans regime/. |
| `agents/agent_news_v2.py` | RSS + LLM sentiment — pas dans le MVP V7. |
| `agents/agent_whales.py` | API whales + LLM — pas dans le MVP V7. |
| `agents/agent_orderbook.py` | Niveau pré-LLM — non utilisé. |
| `agents/agent_learner.py` | Profile TP/SL ATR-based — remplacé par vol-targeting allocateur. |
| `agents/agent_trader.py` (684) | Couche exec LLM-couplée — remplacé par execution/engine.py. |
| `agents/agent_memory.py` | Mémoire LLM partagée. |
| `agents/agent_risk.py`, `agents/agent_risk_entry.py` | Logique risk LLM, ré-écrite proprement. |
| `agents/agent_symbol_selector.py` | Watchlist top-30 LLM — peut être dur-codée ou re-fait simple. |
| `agents/coder.py` | Outil dev LLM. |
| `agents/market_scanner.py` | Pré-pipeline LLM. |
| `agents/scalp_memory.py` | État pipeline scalp. |
| `agents/strategy_momentum.py`, `strategy_trend.py`, `strategy_optimizer.py` | Ébauches non utilisées en V6.2 — on ré-écrit Momentum from scratch en V7. |
| `agents/xgb_gate.py` | XGB Gate désactivé V6.2. Réintégrable plus tard comme feature dans Risk Manager (post-MVP). |
| `main_v6.py` | Remplacé par main.py V7. |
| `analyze_trades.py`, `analyze_trades_v2.py` | Outils ponctuels — refait via `backtest/metrics.py`. |
| `dashboard_api.py`, `serve_dashboard.py`, `serve_dashboard_simple.py` | Vestiges anciens — un seul `dashboard.py` survit. |
| `config_hyperliquid.py`, `write_agents.py` | Outils legacy. |

### ⚪ DROP (à supprimer définitivement après archivage)

- `memory/scalp_memory.json`, `trader_memory.json`, `memory/shared_memory.json` — état runtime V6 incompatible V7
- `memory/scalper_profiles` (dans shared_memory) — appartient au pipeline learner V6
- `agents/grid_bot/` (dossier non tracké, HTX experimental) — totalement hors scope
- LocalAI Docker container — plus utilisé après cutover

## Arborescence cible V7

```
salledesmarches/
├── config/
│   ├── settings.py            # pydantic
│   └── allocation.yaml        # matrice B, VOL_TARGET, bornes mult, seuils
├── core/
│   ├── types.py               # Regime, Signal, TargetPortfolio, Fill
│   ├── interfaces.py          # Protocols
│   └── clock.py               # ordonnanceur événements
├── data/
│   ├── feed.py                # ingestion live HL
│   ├── storage.py             # parquet / sqlite
│   ├── orderflow_collector.py # depuis V6 scripts/
│   └── historical/            # parquets backfill
├── regime/
│   ├── detector.py            # nouveau, softmax + hystérésis
│   └── features.py            # ADX, Hurst, vol percentile, autocorr
├── strategies/
│   ├── base.py
│   ├── grid.py                # wrap V6 GridManager
│   ├── mean_reversion.py      # wrap V6 AgentMeanReversion
│   └── momentum.py            # from scratch
├── allocation/
│   ├── allocator.py
│   └── performance.py
├── risk/
│   ├── manager.py             # migration depuis main_v6 + V6 risk_manager
│   └── stops.py
├── execution/
│   ├── engine.py              # reconcile + bande non-trade
│   ├── hyperliquid_client.py  # port V6
│   ├── hyperliquid_adapter.py # port V6 exchanges/hyperliquid
│   ├── order_registry.py      # port V6 memory/order_registry
│   └── paper.py               # paper trading
├── backtest/
│   ├── engine.py              # walk-forward
│   ├── costs.py               # maker/taker HL réels
│   └── metrics.py             # Sharpe, maxDD, attribution
├── monitoring/
│   ├── dashboard.py           # depuis V6 dashboard.py, étendu
│   └── alerts.py
├── scripts/
│   ├── audit.sh               # cron 6h adapté V7
│   ├── audit_metrics.sh
│   ├── audit_prompt.md        # nouveau prompt V7
│   ├── backfill_history.py    # déjà fait
│   ├── bot.sh                 # adapté V7
│   └── orderflow_collector.py # systemd service
├── utils/
│   ├── stats.py               # port V6
│   └── multi_tf.py            # éventuellement
├── legacy/                    # archives V6
│   └── v6/
│       └── agents/...
├── tests/                     # pytest, démarrage P0
├── data/historical/           # parquets backfill (déjà créés)
├── memory/                    # runtime V7 (vide au boot)
├── logs/                      # runtime V7
└── main.py                    # entrée V7
```

## Métriques d'effort estimé

| Phase | Effort dev | Réutilisation V6 |
|---|---|---|
| P-1 préparation | 1.5j | datalake fait ✓ |
| P0 fondations | 1-2j | quasi from scratch |
| P1 régime | 2-3j | features réutilisables 30% |
| P2a wrap Grid | 1-2j | ~95% réutilisation |
| P2b wrap MR | 1j | ~95% réutilisation |
| P2c Momentum | 2-3j | from scratch |
| P3 allocateur | 2-3j | from scratch |
| P4 risk | 2-3j | ~60% réutilisation (trail+emergency) |
| P5 backtester | 3-5j | from scratch (data déjà là) |
| P6 exec + paper | 2-3j | ~70% réutilisation (hyperliquid_client) |
| P7 paper // | 7j calendaire | observation |
| P8 cutover | 1-2j | migration state |
| P10 monitoring + audit | 2-3j | ~50% (dashboard base) |

**Total dev** : ~22-30j équivalent + 7j paper = ~5-6 semaines calendaires.

## Sortie P-1

✅ Worktree `~/SalleDesMarches_v7/` créé sur branche `v7-allocation` (commit a74ec2a)
✅ `data/historical/fills.parquet` (3995 fills, 28 jours)
✅ `data/historical/ohlcv_1h_{SYM}.parquet` × 17 (73457 candles, 6 mois)
✅ `scripts/backfill_history.py` (relançable pour update)
✅ `V7_README.md` + `V7_INVENTORY.md` (ce fichier)

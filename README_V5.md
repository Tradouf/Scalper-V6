# SalleDesMarches V5 — Scalping +1% HF sur Hyperliquid

## Arborescence

```
sdm-v5/                        ← NOUVEAU dossier (ne pas écraser V4)
├── main_v5.py                 ★ Point d'entrée — lance la boucle
├── config/
│   └── settings.py            ★ Tous les paramètres V5 (SIMULATION_MODE=True au départ)
├── agents/
│   ├── agent_scalper.py       ★ NOUVEAU — décision ENTER/MANAGE/EXIT +1% trailing
│   └── agent_orderbook.py     ★ NOUVEAU — lecture L2 sans LLM
├── utils/
│   ├── scalp_filter.py        ★ NOUVEAU — filtre pré-LLM (ATR/S/R/spread)
│   └── metrics.py             ★ NOUVEAU — tracking WR / PF / hit rate +1%
```

## Briques V4 réutilisées TELLES QUELLES (pas touchées)

```
exchanges/hyperliquid.py       ← connexion + ordres Hyperliquid
exchanges/base.py              ← interface ExchangeClient
memory/shared_memory.py        ← mémoire partagée thread-safe
agents/base_agent.py           ← sémaphore LLM, _llm(), _parse_json()
agents/agent_technical.py      ← indicateurs + interprétation LLM
agents/agent_bull.py           ← débat haussier
agents/agent_bear.py           ← débat baissier
agents/agent_news.py           ← news / sentiment
agents/agent_whales.py         ← on-chain whales
agents/agent_orchestrator.py   ← régime marché
```

## Cycle V5 (30 secondes)

```
1. Refresh News/Whales si > 15-20 min écoulés (0-2 appels LLM)
2. Orchestrateur → régime (1 appel LLM, inchangé V4)
3. Hard stop journalier -4% → fin de cycle si déclenché
4. Pour chaque symbole de SCALP_WATCHLIST :
   a. AgentTechnical.analyze()   → 1 LLM (~2s)
   b. AgentOrderbook.analyze()   → 0 LLM (<100ms)
   c. scalp_filter (pré-LLM)     → 0 LLM (<1ms)
      └─ ATR% ≥ 0.6%
      └─ Distance S/R ≥ 1.2%
      └─ Spread < 0.1%
      └─ Liquidité carnet OK
   d. AgentBull.argue()          → 1 LLM (~2s)
   e. AgentBear.argue()          → 1 LLM (~2s)
   f. compute_consensus()        → 0 LLM
   g. AgentScalper.decide()      → 1 LLM si ENTER, 0 si MANAGE
   h. Execute → Hyperliquid
```

Budget LLM par symbole : 4-5 appels (~8-10s sur Qwen 7B local)
Avec 3 symboles actifs : ~30s = pile dans le cycle.

## Logique +1% trailing

```
Entrée → prix se déplace +1% dans sens du trade
       → SL remonté à entry + 0.1% (break-even sécurisé)
       → trailing_active = True
       → SL = price − (1.0-1.5 × ATR) mis à jour chaque cycle
       → Si SL touché → sortie automatique market
       → Si MAX_TRADE_DURATION (30min) → sortie forcée market
```

## Démarrage

```bash
# 1. Copier les fichiers V5 dans le dossier SDM existant
cp -r sdm-v5-code/* /chemin/vers/sdm/

# 2. Vérifier SIMULATION_MODE = True dans config/settings.py

# 3. Lancer
cd /chemin/vers/sdm/
python main_v5.py

# 4. Observer les logs [SIM] pendant 1-2 heures
# 5. Valider la logique, puis SIMULATION_MODE = False
```

## Métriques disponibles

```python
from utils.metrics import MetricsV5
m = MetricsV5()
print(m.get_summary())
# → {total, wins, losses, total_pnl, hit_1pct, trail_extended,
#    avg_duration_min, profit_factor, winrate}
```

## Paramètres clés à ajuster en premier

| Paramètre | Valeur par défaut | Quand changer |
|---|---|---|
| SCALP_MIN_ATR_PCT | 0.006 (0.6%) | Baisser si peu de trades sur BTC |
| SCALP_MIN_SR_DIST | 0.012 (1.2%) | Baisser si entrées trop rares |
| MIN_CONSENSUS | 0.65 | Baisser légèrement si trop sélectif |
| DEFAULT_LEVERAGE | 3x | Monter prudemment après validation |
| MAX_POSITION_PCT | 2% equity | Monter après profits confirmés |


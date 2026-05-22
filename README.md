# SalleDesMarches — V6.1.0

Bot de scalping algorithmique haute fréquence sur Hyperliquid perpetuals.
Système multi-agents LLM local (LocalAI) avec grid bot intégré.

---

## Architecture générale

```
main_v6.py  ←  boucle principale 30s
    │
    ├── _hl_sync_loop()     thread indépendant, sync cache HL toutes les 2s
    ├── _trail_loop()       thread indépendant, trailing stop software toutes les 2s
    └── _grid_loop()        thread indépendant, grid bot tick toutes les 2s
```

### Pipeline par symbole (30s/cycle, 4 symboles/cycle)

```
FeatureEngine  →  RegimeEngine  →  [scalp_filter]
                                         │
                              AgentTechnical (LLM)
                              AgentOrderbook (déterministe)
                                         │
                              AgentMomentum (bull) ──┐
                              AgentRisk     (bear) ──┤→ _consensus() → AgentScalper (LLM)
                              AgentLearner  (déterministe)              → AgentTrader
```

---

## Agents

| Agent | LLM | Rôle | Output clé |
|---|---|---|---|
| `agent_orchestrator.py` | Oui | Régime global marché | trend, volatility, risk |
| `agent_technical.py` | Oui | Indicateurs technique | signal, confidence, rsi, atr |
| `agent_bull.py` (Momentum) | Oui | Direction du signal | signal buy/sell/wait, confidence |
| `agent_bear.py` (Risk) | Oui | Risque d'entrée | risk_level, risk_score 0-1 |
| `agent_scalper.py` | Oui | Décision finale ENTER/MANAGE/EXIT | entry, sl, tp, confidence |
| `agent_news_v2.py` | Oui | Sentiment RSS (CoinDesk, etc.) | overall_sentiment |
| `agent_whales.py` | Oui | Mouvements on-chain | sentiment |
| `agent_orderbook.py` | Non | Déséquilibre L2 | imbalance, pressure |
| `agent_learner.py` | Non | Adapte TP/SL depuis historique | scalper_profiles |
| `agent_trader.py` | Non | Exécution ordre | — |
| `agent_symbol_selector.py` | Non | Sélection symboles actifs | active_symbols |

### Consensus V6.1

```python
# Les deux s'accordent → confiance moyenne
if momentum_signal == tech_signal:
    base_conf = (mom_conf + tech_conf) / 2.0

# Tech seul → poids réduit
elif tech_signal in ("buy", "sell"):
    base_conf = tech_conf * 0.80

# Réduction par le risque (AgentRisk)
final_conf = base_conf * (1.0 - risk_score * 0.5)
```

---

## Grid Bot

Grille symétrique neutre activée automatiquement quand `regime.trend == "range"`.

```
activate()  →  buy_limit@(center - spacing/2)  +  sell_limit@(center + spacing/2)
                    │                                        │
              si buy rempli                           si sell rempli
              → cancel sell                           → cancel buy
              → TP sell reduce_only                   → TP buy reduce_only
                    └──────────────── nouveau cycle ──────────────────┘
```

| Paramètre | Valeur | Description |
|---|---|---|
| `GRID_NOTIONAL` | 20 USDT | Taille par unité de grille |
| `GRID_LEVERAGE` | 3× | Levier grille |
| `GRID_ATR_FACTOR` | 0.50 | spacing = ATR × 0.50 |
| `GRID_MAX_SYMBOLS` | 3 | Max grilles simultanées |
| `GRID_COOLDOWN_SEC` | 300s | Délai min entre désactivation et réactivation |
| `GRID_FORCE_SYMBOLS` | [] | Debug : force la grille (ignore régime) |

---

## Smart Entry (Limit Alo → fallback Market)

Réduit les frais en ciblant le côté maker (0.01%) vs taker (0.045%).

```
place_order_smart()
    │
    ├── spread < 20 bps ET conf ≥ 0.70 ET vol ∈ (low, medium)
    │       → LIMIT Alo @ best_bid (buy) ou best_ask (sell)
    │       → poll fill toutes les 0.5s pendant 30s max
    │       → si mid s'éloigne > 0.3% → cancel immédiat + fallback
    │       → si timeout → cancel + fallback
    │
    └── sinon → MARKET direct
```

| Paramètre | Valeur |
|---|---|
| `LIMIT_FILL_TIMEOUT_SEC` | 30s |
| `LIMIT_MAX_SPREAD_PCT` | 0.20% (20 bps) |
| `LIMIT_STALE_PCT` | 0.30% (cancel si mid s'éloigne) |
| `LIMIT_USE_MIN_CONFIDENCE` | 0.70 |

---

## Sizing

```python
risk_usdt = max(1.0, equity_spot × 1%)    # equity = compte spot USDC HL
qty_raw   = risk_usdt / abs(entry - sl)
qty       = min(qty_raw, equity × 15% / entry)   # cap notional 15%
qty      *= SIZING_CONF_FLOOR + (1 - SIZING_CONF_FLOOR) × (conf - MIN_CONF) / (1 - MIN_CONF)
# À conf=0.70 → factor=0.40 | À conf=1.0 → factor=1.0
```

**Note** : l'equity lue est `spotClearinghouseState.balances[USDC].total` (balance spot HL),
car le compte perp ne contient que la marge des positions ouvertes.

---

## Trailing stop (software)

Aucun SL natif sur exchange au départ. Géré par `_trail_loop()` toutes les 2s.

```
TP_arm atteint (0.80% ROE par défaut)
    → armed = True
    → trail_step toutes les 0.15% ROE supplémentaire
    → si ROE recule de 0.25% depuis best → place SL natif HL (positionTpsl)
```

---

## Risk management

| Paramètre | Valeur |
|---|---|
| `MAX_OPEN_POSITIONS` | 6 |
| `DAILY_LOSS_LIMIT_PCT` | 3% → arrêt total |
| `FREEZE_CONSEC_LOSSES` | 2 pertes consécutives → freeze 1h |
| `FREEZE_WINRATE_MAX` | WR < 34% sur 5 derniers → freeze 4h |
| `FLIP_MIN_CONFIDENCE` | 0.81 (conf élevée requise pour changer de direction) |
| `EXIT_COOLDOWN_SEC` | 600s après une sortie |

---

## Watchlist & SymbolSelector

`SCALP_WATCHLIST = [BTC, ETH, SOL, BNB, LINK, HYPE, ZEC, APE, DOGE, XRP, TAO, AAVE]`

`AgentSymbolSelector` filtre uniquement dans cette liste, puis trie par volume HL (`dayNtlVlm`).
Symboles sans historique Learner → exploration. Symboles avec WR ≥ 35% → confirmés.

---

## Equity

La balance $208 USDC est dans le **compte spot HL** (`spotClearinghouseState`).
Le compte perp ne contient que la marge des positions ouvertes (~$6.67 par grille active).
Le bot lit les deux et les additionne pour le sizing.

---

## Fichiers clés

```
main_v6.py                  — boucle principale, trail, grid, sync
config/settings.py          — tous les paramètres
agents/
    agent_bull.py           — AgentMomentum : direction signal
    agent_bear.py           — AgentRisk : risque entrée
    agent_scalper.py        — décision finale
    agent_learner.py        — adaptation TP/SL
    grid_manager.py         — logique grille
exchanges/hyperliquid.py    — wrapper exchange
hyperliquid_client.py       — client HL (WebSocket + REST, smart order)
memory/shared_memory.py     — mémoire partagée thread-safe JSON
```

---

## Lancement

```bash
source .venv/bin/activate
cp .env.example .env       # renseigner HL_PRIVATE_KEY, HL_ACCOUNT_ADDRESS
bash start_sdm.sh
```

Logs : `logs/sdm.log` (rotation 10 Mo × 5). Niveau logger : `sdm.*`

---

## Historique versions

| Version | Date | Changements principaux |
|---|---|---|
| V6.1.0 | 2026-05-04 | AgentMomentum + AgentRisk (prompts courts, risk_score numérique) ; consensus momentum×risque ; equity spot HL ; smart limit staleness guard ; SymbolSelector filtre watchlist |
| V6.0.0 | 2026-04-29 | Réécriture complète V6 : sync cache HL indépendant, trail natif, grid bot, smart entry limit Alo |
| V5.x | 2026-03 | Système multi-agents initial |

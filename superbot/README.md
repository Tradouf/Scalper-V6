# SuperBot

Bot Hyperliquid **déterministe** (zéro LLM, zéro pandas) — combine les seules
edges validées du projet : 3 sleeves + détection de régime HMM double couche +
exécution maker-first. Spécification complète : [SPEC.md](SPEC.md).

## Architecture (résumé)

| Brique | Rôle |
|---|---|
| `sleeves/momentum.py` | **A** — ROC 48h ±2 %, PAS de TP, SL 2×ATR, time-exit 12 j, filtres funding/spread. Params **figés** (OOS 833 j). Alloc 35 % |
| `sleeves/adaptive_ema.py` | **B** — EMA cross multi-TF (15m/1h choisi par symbole), RSI, EMA200 **figé hors grille**. Alloc 45 % |
| `sleeves/breakout.py` | **C** — Donchian 1h + expansion ATR obligatoire, TP 3×ATR. Alloc 20 % |
| `hmm.py` / `regime.py` | HMM marché (BTC 4h, K=4) + HMM par symbole (K=3), validation OOS à l'entraînement, fallback ADX, hystérésis 2 bougies |
| `orchestrator.py` | **Double gate** : le marché autorise les sleeves, le symbole autorise l'entrée (long si trending_up…), sizing × confiance, caps 6/5/3 + 10 total |
| `optimizer.py` | Walk-forward 70/30 toutes les 4 h — **premier set du rang train qui confirme**, jamais de sélection sur le PnL de validation ; arbitrage TF et sleeve par composite TRAIN |
| `risk.py` | Kill-switch -3 % jour / -8 % vs pic 7 j, hystérésis 2 confirmations, corrélation majors/alts |
| `live_trader.py` | Paper (défaut) ou live : maker-first, SL natif dès l'entrée, une position par symbole, réconciliation |

## Wallet

`HL3_PRIVATE_KEY` / `HL3_ACCOUNT_ADDRESS` — **troisième wallet**. Refus de
démarrer si identique à `HL_*` (V6) ou `HL2_*` (SimpleBot).

## Lancement

```bash
python -m pytest tests/test_superbot.py -v   # tests
python -m superbot.run --optimize-once        # une optimisation (cron-friendly)
bash start_superbot.sh                        # DRY-RUN (papier) — défaut
bash start_superbot.sh --live                 # ordres réels (exige HL3_*)
bash start_superbot_dashboard.sh              # http://localhost:8084
```

## Passage en live (SPEC §13)

Le dry-run papier doit tourner **14 jours** et montrer **PF ≥ 1.2, WR ≥ 35 %,
DD < 10 %** avant d'autoriser `SUPERBOT_DRY_RUN=0`. WR < 25 % → revoir les
filtres, pas le levier. Aucune promesse de gain — les métriques de validation
ne prédisent pas le live (leçon R&D 07/2026, mémoire `simplebot-edge-oos`).

## Fichiers d'état (`superbot/state/`, non versionnés)

`best_params.json` (params par symbole, sleeve et TF gagnants),
`optimizer_history.jsonl`, `live_state.json` (positions/trades papier, equity,
kill-switch, stats exec/gates), `regime_market.json`, `regime_symbols.json`,
`hmm/*.pkl` (modèles marché + par symbole actif), `superbot.lock`.

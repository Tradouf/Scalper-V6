# Prompt Claude Code — SuperBot (Option C)

Copier-coller le bloc ci-dessous dans Claude Code.

---

```
# MISSION : Implémenter SuperBot from scratch

Tu travailles dans le repo /home/francois/Scalper-V6.
La spec complète est dans superbot/SPEC.md — LIS-LA EN ENTIER avant de coder.

## Contexte projet (juillet 2026)

- SimpleBot (simplebot/) tourne et fonctionne : optimiseur walk-forward 15m,
  XPL +12.6% valid PF 1.62, SOL +4.1% valid PF 1.45 (résultats du 11/07/2026).
- llmbot/ existe (prototype LLM) — NE PAS TOUCHER, projet séparé.
- main_v6.py (bot LLM multi-agents) est arrêté — NE PAS TOUCHER.
- SuperBot = nouveau bot déterministe, 3 sleeves, HMM double couche, wallet HL3.

## RÈGLES ABSOLUES

1. Tout le code va dans superbot/ (nouveau package)
2. NE MODIFIE PAS : main_v6.py, simplebot/, llmbot/, agents/ (sauf import lecture)
3. Wallet HL3_PRIVATE_KEY / HL3_ACCOUNT_ADDRESS — refuse démarrage si = HL_* ou HL2_*
4. Dry-run par défaut (SUPERBOT_DRY_RUN=1)
5. Maker-first entrées (réutiliser simplebot/execution.smart_entry)
6. SL natif exchange OBLIGATOIRE à chaque entrée
7. Zéro LLM, zéro pandas
8. Walk-forward : classer sur TRAIN, validation = filtre binaire — JAMAIS choisir
   le meilleur PnL de validation (réintroduit overfit)
9. Ajouter hmmlearn>=0.3.0 à requirements.txt
10. Une phase à la fois — ne passe à la suivante que si DoD atteinte

## CODE À RÉUTILISER (import direct)

- simplebot/data.py
- simplebot/strategy.py (ema, rsi, atr, compute_signals)
- simplebot/backtester.py (étendre pour multi-TF + maker)
- simplebot/symbol_filter.py
- simplebot/execution.py
- simplebot/live_trader.py (patron kill-switch, réconciliation, paper)
- simplebot/momentum.py (patron Sleeve A)
- agents/regime_engine.py (patron markov.py — pseudo-Markov)
- hyperliquid_client.py

---

# PHASE 1 — Squelette + Sleeve B Adaptive EMA

Créer :
- superbot/__init__.py, config.py, data.py
- superbot/sleeves/base.py, sleeves/adaptive_ema.py
- superbot/backtester.py
- superbot/optimizer.py
- superbot/symbol_filter.py
- tests/test_superbot.py

Sleeve B :
- EMA cross + RSI + trend_ema=200 FIXE (hors grille)
- Multi-TF : tester 15m ET 1h par symbole, garder le meilleur
- Grille : ema_fast [9,12,21], ema_slow [26,50,100], tp_atr [1.5,2.5,3.5], sl_atr [1.0-4.0]
- Walk-forward 70/30, premier set train qui confirme (PF≥1.2, PnL>0, ≥5 trades valid)
- Filtre qualité : PF≥1.4, PnL valid≥2%, WR≥40%, max 8 actifs

Tests obligatoires :
- test_walk_forward_no_overfit_selection
- test_multi_tf_picks_best_interval
- test_quality_filter_demotes_weak_symbols
- test_backtester_maker_mode

DoD Phase 1 :
  python -m pytest tests/test_superbot.py -v  → tout vert
  python -m superbot.optimizer              → best_params.json avec ≥1 symbole actif

STOP et rapporte avant Phase 2.

---

# PHASE 2 — Live + HMM double couche + Orchestrateur

Créer :
- superbot/hmm.py (HMM marché BTC K=4 + HMM par symbole K=3)
- superbot/markov.py (pseudo-Markov depuis regime_engine.py)
- superbot/regime.py (façade HMM + fallback ADX)
- superbot/orchestrator.py (DOUBLE GATE)
- superbot/risk.py, execution.py, live_trader.py, run.py

HMM DOUBLE COUCHE (voir SPEC §4) :

Couche 1 — Marché (BTC 4h, 4 états) → autorise/bloque SLEEVES :
  bull_orderly, bear_orderly, range_compressed, high_vol_chaotic
  → state/hmm/market.pkl + state/regime_market.json

Couche 2 — Par symbole (K=3, chaque actif) → autorise/bloque ENTRÉES :
  trending_up → LONG only
  trending_down → SHORT only
  choppy → no entry
  → state/hmm/{SYMBOL}.pkl + state/regime_symbols.json
  Entraîner dans optimizer étape 8 (symboles active=True uniquement)

Double gate :
  allow_entry(signal, symbol, sleeve):
    1. marché autorise sleeve ?
    2. symbole autorise direction ?
    3. transition_risk < 0.50 ?
    4. confiance HMM symbole ≥ 0.50 ?

Hystérésis : 2 bougies consécutives + confiance min.

Tests obligatoires :
- test_hmm_market_train_and_save, test_hmm_market_hysteresis_blocks_flip
- test_hmm_symbol_blocks_long_in_trending_down
- test_hmm_symbol_blocks_entry_in_choppy
- test_hmm_symbol_fallback_when_no_pkl
- test_orchestrator_double_gate_market_and_symbol
- test_kill_switch_hysteresis, test_flip_cooldown, test_no_double_order_per_bar

DoD Phase 2 :
  python -m superbot.run  → dry-run, signaux + PnL papier logués

STOP et rapporte avant Phase 3.

---

# PHASE 3 — Sleeves A (Momentum) + C (Breakout)

Créer :
- superbot/sleeves/momentum.py (ROC 4h ±2%, PAS de TP, SL 2×ATR, time-exit 12j, filtre funding)
- superbot/sleeves/breakout.py (Donchian 20, 1h, TP 3×ATR, SL 1.5×ATR)
- Intégration orchestrateur (alloc 35% / 45% / 20%)

Tests :
- test_momentum_no_tp, test_momentum_funding_filter
- test_breakout_donchian_signal, test_orchestrator_regime_gating

DoD Phase 3 : dry-run 48h, 3 sleeves actives.

---

# PHASE 4 — Dashboard + scripts

Créer :
- superbot/dashboard.py (port 8084, stdlib, lecture seule)
  Cartes : régime marché HMM, régime par symbole, symboles actifs, positions, equity
- start_superbot.sh, start_superbot_dashboard.sh
- superbot/README.md

DoD Phase 4 :
  python -m superbot.dashboard  → http://localhost:8084 OK
  Pas de régression pytest

---

# CONFIG .env par défaut

SUPERBOT_DRY_RUN=1
SUPERBOT_SYMBOLS=ALL
SUPERBOT_MAX_SYMBOLS=40
SUPERBOT_MAX_ACTIVE_SYMBOLS=8
SUPERBOT_MAX_OPEN_TOTAL=10
SUPERBOT_LEVERAGE=3
SUPERBOT_MARGIN_PCT=0.04
SUPERBOT_EXEC_MAKER_FIRST=1
SUPERBOT_HMM_MARKET_STATES=4
SUPERBOT_HMM_SYMBOL_STATES=3
SUPERBOT_HMM_MARKET_MIN_CONF=0.55
SUPERBOT_HMM_SYMBOL_MIN_CONF=0.50
SUPERBOT_HMM_TRANSITION_FREEZE=0.50
SUPERBOT_DAILY_LOSS_LIMIT_PCT=0.03
HL3_PRIVATE_KEY=...
HL3_ACCOUNT_ADDRESS=...

---

# COMMENCE MAINTENANT

Phase 1 uniquement. Lis superbot/SPEC.md. Code. Teste. Rapporte DoD.
```
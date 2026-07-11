# Brief Claude Code — SimpleBot & profit (juillet 2026)

Contexte : analyse Grok du 11/07/2026. Le filtre symboles est **déjà implémenté**
(`simplebot/symbol_filter.py`). Ce brief liste le reste par priorité.

---

## ✅ Fait (ne pas refaire)

- **Filtre symboles post-optimiseur** : `apply_symbol_filter()` dans
  `optimizer.run_once()` après le walk-forward.
- Fichiers touchés : `symbol_filter.py`, `config.py`, `optimizer.py`,
  `live_trader.py` (`tradeable_symbols()`), `dashboard.py`, tests.
- Réglages env (défauts conservateurs) :

```bash
SIMPLEBOT_QUALITY_MIN_VALID_PF=1.4      # PF validation minimum
SIMPLEBOT_QUALITY_MIN_VALID_PNL_PCT=0.02  # +2% sur fenêtre valid
SIMPLEBOT_QUALITY_MIN_VALID_WINRATE=0.40
SIMPLEBOT_QUALITY_MIN_TRAIN_PF=1.05
SIMPLEBOT_MAX_ACTIVE_SYMBOLS=8          # top-N par score composite
# Optionnel :
SIMPLEBOT_SYMBOL_ALLOWLIST=BTC,ETH,SOL,XPL,JTO,MON
SIMPLEBOT_SYMBOL_BLOCKLIST=PUMP,FARTCOIN
```

---

## P0 — Correctifs capital (urgent)

### 1. Kill-switch faux positif equity (incident 2026-07-04)

**Symptôme** : `account value 19.96 ≤ pic 200.93` → fermeture brutale alors que
le wallet était ~200 $.

**Fichiers** : `simplebot/live_trader.py` (`_account_value`, `_kill_switch_engaged`),
`simplebot/config.py` (`EQUITY_CANON_TOL`, `KILL_MAX_READ_FAILURES`).

**À faire** :
- Si lecture spot échoue (429, timeout), **ne jamais** déclencher le kill-switch ;
  incrémenter `equity_read_failures` et geler les **nouvelles entrées** seulement
  après `KILL_MAX_READ_FAILURES` (déjà partiellement en place — vérifier le chemin
  spot=0).
- Exiger **2 lectures consécutives** sous le seuil avant kill (hystérésis).
- Logger `equity_raw={perp, spot, canon, clamped}` une fois par cycle kill-check
  (pas chaque 30s — trop verbeux).
- Test unitaire : mock spot=exception → pas de kill ; mock spot=0 + perp=200 →
  pas de kill si COUNT_SPOT_IN_EQUITY.

### 2. Réduire les flips market (frais taker)

**Symptôme** : logs `AVAX/VVV: signal opposé → flip` en rafale.

**Fichiers** : `simplebot/live_trader.py` (`_process_symbol`).

**À faire** :
- Cooldown post-flip par symbole (ex. 2 bougies = 30 min en 15m) — env
  `SIMPLEBOT_FLIP_COOLDOWN_BARS=2`.
- Option : ignorer flip si PnL latent > −0.5% (laisser TP/SL natifs gérer).
- Test dry-run : deux signaux opposés sur 2 bougies → un seul flip.

---

## P1 — Profit SimpleBot (1–2 jours)

### 3. Maker-first sur les entrées

**Constat MinuteLab** : edge brut existe, frais taker tuent le PnL.

**Fichiers** : `simplebot/live_trader.py` (`_open_position`), nouveau helper
`simplebot/execution.py` (s'inspirer de `main_v6.py` smart limit).

**À faire** :
- Tenter limit Alo post-only au mid ± 1 tick, timeout 30s, fallback market.
- Compter maker vs taker dans `live_state.json` pour le dashboard.
- Backtester : ajouter mode `entry_mode=maker|taker` avec probabilité de fill
  simplifiée (ex. 70% maker rempli en low vol).

### 4. Sizing dynamique par score validation

**Fichiers** : `live_trader.py`, `symbol_filter.py` (`quality_score`).

**À faire** :
- `margin_pct = MARGIN_PCT * (0.5 + 0.5 * normalized_score)` plafonné à
  `SIMPLEBOT_MARGIN_PCT_MAX` (ex. 0.08).
- Symbole top score (B) trade 8%, symbole limite cap (C) trade 5%.

### 5. MAX_OPEN_POSITIONS vs symboles actifs

**Constat** : beaucoup de `signal ignoré — MAX_OPEN_POSITIONS atteint` alors que
8 symboles sont actifs.

**À faire** :
- Recommandation doc : `MAX_OPEN_POSITIONS >= min(5, MAX_ACTIVE_SYMBOLS)`.
- Ou prioriser les entrées par `quality_score` quand slots pleins (file d'attente
  1 bougie).

---

## P2 — Momentum 4h (paper → live)

**Constat** : OOS historique solide, paper live juillet : equity 200→182, WR ~19%.

**Fichiers** : `simplebot/momentum.py`, `simplebot/config.py`.

**À faire avant live** :
- Rapport paper 14j : WR, PF, funding cumulé, drawdown.
- Filtre funding : ne pas LONG si funding horaire > seuil (ex. +0.01%/h).
- Cap positions : `MOMENTUM_MAX_OPEN=10` (plus 0=illimité).
- Si paper WR < 35% sur 14j → ne pas passer live.

**Passage live** (si validé) : module séparé `momentum_live.py`, même wallet HL2,
TP toujours absent, SL natif 2×ATR.

---

## P3 — V6 (bot principal, actuellement arrêté — pas de sdm.log)

### 6. Gate multi-TF adaptatif

**Fichiers** : `main_v6.py`, `agents/multi_tf.py`, `config/settings.py`.

**Problème** : `MULTI_TF_GATE_ENABLED=True` + veto strict → CONSENSUS=0 pendant
des heures en régime range.

**À faire** :
- Mode `gate_mode=strict|trend_only|off` :
  - `trend_only` : H1 bias obligatoire, M15/M1 informatifs (pas veto).
  - Activer `trend_only` si `regime.trend in (bull, bear)`.
- A/B 72h : mesurer EV/trade via `analyze_trades_v2.py`.

### 7. XGBoost + orderflow

**Fichiers** : `scripts/orderflow_collector.py`, `agents/xgb_gate.py`, script
réentraînement (chercher `backtest_alpha.py` ou équivalent).

**À faire** :
- Vérifier que `memory/orderflow.db` se remplit (daemon systemd/cron).
- Réentraîner avec features L2 imbalance + funding.
- Monter `XGB_GATE_THRESHOLD` à 0.62 si trop de trades filtrés à 0.55.

### 8. Heures bloquées

**Fichiers** : `config/settings.py` — `BLOCKED_HOURS_UTC` désactivé temporairement.

**À faire** : réactiver `{13,14,18,19,20,21,22}` après 1 semaine de logs avec
`analyze_trades_v2.py --csv` pour confirmer EV négative.

---

## P4 — Infra & observabilité

### 9. Dashboard SimpleBot

**Fichier** : `simplebot/dashboard.py`

- Afficher colonne `filter_reason` distincte de `reason` optimiseur.
- Carte « symboles filtrés / actifs / cap ».
- Courbe equity canon (pas perp+spot brute).

### 10. Tests d'intégration manquants

- `test_optimizer_applies_symbol_filter` : run_once avec 3 symboles synthétiques,
  vérifier cap et filter_reason dans JSON écrit.
- `test_kill_switch_hysteresis` (après impl P0).

---

## Commandes utiles

```bash
# Tests SimpleBot
python -m pytest tests/test_simplebot.py -v

# Forcer une optimisation + filtre
python -m simplebot.run --optimize-once

# Live (wallet HL2)
SIMPLEBOT_DRY_RUN=0 python -m simplebot.run

# Analyse V6 (quand sdm.log existe)
python analyze_trades_v2.py --csv report.csv
```

---

## Definition of Done (session Claude Code)

1. P0 kill-switch : test vert + 0 faux positif sur mock 429/spot fail.
2. P0 flip cooldown : test vert.
3. Au moins un de P1 (maker OU sizing dynamique) avec test.
4. Pas de régression `pytest tests/test_simplebot.py`.

Ne pas toucher à `main_v6.py` dans la même PR que SimpleBot sauf si explicitement
demandé — wallets séparés, cycles de release différents.
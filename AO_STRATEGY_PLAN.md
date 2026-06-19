# Stratégie Awesome Oscillator (AO) — plan de reprise

> Doc de reprise de session (créé 2026-06-17). Statut : **PLANIFIÉ, pas encore codé.**
> Reprendre par la section « Checklist de reprise » en bas.

## Spécification (validée par francois)

Indicateur **Awesome Oscillator standard de TradingView / Hyperliquid** :
- `AO = SMA(median, 5) − SMA(median, 34)`, avec `median = (high + low) / 2`.
- Histogramme autour de zéro. Couleur de barre : **verte** si `AO[t] > AO[t-1]` (croît),
  **rouge** si `AO[t] < AO[t-1]` (décroît).

Règles d'**entrée** :
- **LONG** : barre AO rouge (décroît) ∧ `AO < −x_long` ∧ bougie de prix verte (`close > open`).
- **SHORT** : barre AO verte (croît) ∧ `AO > +x_short` ∧ bougie de prix rouge (`close < open`).

Règle de **sortie** : **TP uniquement pour l'instant** (take-profit, pas de SL pour le moment
— décision francois 2026-06-17 ; SL à rajouter plus tard si besoin).

Paramètres :
- **Symbole** : BTC uniquement (→ seuil absolu OK, pas de normalisation nécessaire).
- **Timeframe** : 5 minutes.
- `x_long = 65` pour commencer ; `x_short` = paramètre **séparé** (défaut à fixer, 65 au départ).
- `x_long` et `x_short` doivent être **backtestés** puis **ajustés régulièrement par le LLM**
  (cf. Phase 2). Bornes dures à définir.

## Décisions arrêtées
- Livrer **OFF par défaut** (`enabled: false`, poids 0 dans `base_weights`), **backtesté avant tout live**.
  (Pattern repo : supertrend SL natif, governor livrés OFF.)
- **Sortie = TP seul** pour le moment.
- **Tuner LLM = qwen local** (gratuit, comme le governor), pas Opus. Cadence ~6h.
- L'AO est **exclu de l'allocateur** (comme la grille) et gère sa position BTC en propre.
  Quand activé : BTC réservé à l'AO (retiré des autres stratégies) pour éviter deux stratégies
  sur le même actif (cause racine des bugs szi=0 des audits).

## Architecture V7 — points d'intégration (références fichier:ligne)
- Pattern stratégie = `strategies/supertrend.py` : classe avec `generate_signals(market) -> list[Signal]`,
  `on_fill`, `sync_positions`, tracking `_positions`/`_intent` (level-triggered : HOLD silencieux = fermeture).
- `core/types.py:60` `Signal` (direction∈[-1,1], target_notional≥0, stop_price optionnel) ;
  `core/types.py:168` `Candle` (ts_open/open/high/low/close/volume).
- `core/config.py:195` `SupertrendStrategyConfig` (modèle) ; `:208` `StrategiesConfig` (ajouter le champ) ;
  `:256` validateur `_check_strategies_in_matrix` (ajouter le `enabled` check).
- `config/allocation.yaml` : matrice `base_weights[régime][stratégie]` — **les 4 régimes doivent avoir
  le même jeu de stratégies** (validateur strict `core/config.py:53`). Ajouter `awesome_oscillator: 0.0`
  partout.
- `main.py:109-119` instanciation des stratégies + `self.strategies = [...]`.
- `main.py:712-727` build `MarketSnapshot` avec **candles 1h** (`get_candles(sym, interval="1h", limit=200)`).
  → l'AO a besoin d'un **fetch 5m BTC dédié** : `self.hl_read.get_candles("BTC", interval="5m", limit=200)`.
- `main.py:802` l'allocateur **exclut la grille** : `directional_signals = [s for s in all_signals
  if s.strategy_id != self.grid.strategy_id]` → faire pareil pour l'AO (l'exclure de l'allocate,
  router sa cible BTC directement vers risk.project/reconcile/submit OU lui donner un track propre).
- Backtest : `backtest/backtester.py:28` `run(symbol, interval, days, strategy, tp_pct, sl_pct)` +
  `_signals_momentum`/`_signals_trend` → ajouter `_signals_ao` (idéal pour balayer x_long/x_short BTC 5m,
  simule déjà TP/SL). (`backtest/engine.py` = moteur complet mais verrouillé 1h/parquet.)
- Governor comme modèle de tuner LLM : `governor/risk_governor.py`, écrit `memory/risk_overrides.json`
  + `memory/governor_journal.jsonl`, bornes dures en code. L'AO tuner écrira `memory/ao_overrides.json`.

## Plan d'implémentation

### Phase 1 — stratégie + config + backtest (params statiques, paramétrables)
1. `strategies/awesome_oscillator.py` : calcul AO 5/34, couleur barre + bougie, règles entrée, sortie TP,
   tracking position, cooldown anti-rafale 5m.
2. `main.py` : fetch 5m BTC dédié, instancier `AwesomeOscillatorStrategy`, l'exclure de l'allocateur.
3. `core/config.py` + `allocation.yaml` : `AwesomeOscillatorStrategyConfig` (enabled=false, interval=5m,
   symbols=[BTC], fast=5, slow=34, x_long=65, x_short=65, notional_usdc, tp_pct). Poids 0 dans les 4 régimes.
4. `backtest/backtester.py` : `_signals_ao` + script de balayage x_long/x_short sur BTC 5m → métriques.

### Phase 2 — auto-tuning LLM (après validation backtest)
- Boucle qwen (cadence ~6h) : lit le journal trades AO, ajuste x_long/x_short dans bornes dures,
  écrit `memory/ao_overrides.json` + journal. La stratégie lit l'override au tick.

## Checklist de reprise
- [x] Confirmer `x_short` de départ (**60**, fixé par francois 2026-06-18) ; `x_long=65`, `tp_pct=0.012` (défaut, à affiner).
- [x] Confirmer le notional par trade BTC (**30 USDC** par défaut).
- [x] Coder Phase 1 (1→4 ci-dessus). **FAIT 2026-06-18** — voir « État Phase 1 ».
- [x] Lancer le backtest BTC 5m, balayer x_long/x_short, rapporter PnL/winrate/nb trades. **FAIT** — voir verdict.
- [ ] **DÉCISION francois requise** : ajouter un SL (ou max-hold / time-stop) — le backtest montre que le TP seul rend la stratégie non-viable. PUIS re-backtester.
- [ ] AVEC francois : décider activation live (régime, réservation BTC) avant de mettre enabled=true.
- [ ] Phase 2 : tuner LLM.

## État Phase 1 (codé 2026-06-18, OFF par défaut)
Fichiers livrés :
- `strategies/awesome_oscillator.py` — stratégie + `compute_ao` (SMA5/34, barre clôturée -2, TP seul, tracking position).
- `core/config.py` — `AwesomeOscillatorStrategyConfig` (enabled=false, x_long=65, x_short=60, tp_pct=0.012, notional 30).
- `config/allocation.yaml` — `awesome_oscillator: 0.0` dans les 4 régimes + bloc paramètres.
- `main.py` — instanciation, **BTC réservé quand enabled** (retiré MR/momentum/supertrend + grille), routage hors allocateur via `_merge_ao_target`, fills distribués à l'AO, cooldown post-emergency appliqué. **OFF = comportement strictement inchangé.**
- `backtest/backtester.py` — `_signals_ao`, sortie TP seul (`sl_pct<=0` = pas de SL), clôture mark-to-market en fin de données.
- `backtest/run_ao_sweep.py` — balayage x_long/x_short (lecture seule HL).
- `tests/test_awesome_oscillator.py` — 7 tests (indicateur, entrées, anti-rafale, TP, sync). Suite : 188 passent.

## Verdict backtest (BTC 5m, ~17j récents, HL cap 5000 candles)

### v1 — TP seul (sans SL) : NON-VIABLE
Tous les couples x_long/x_short donnent **−10 à −12%**, 1–2 trades seulement : une position prise à
contre-tendance ne touche jamais son TP → elle **verrouille le book** et **saigne** (visible grâce au
mark-to-market EOD ajouté au simulateur).

### v2 — SL ajouté, TP = 2×SL (décision francois 2026-06-18)
Le SL **résout le verrouillage** : 50–240 trades selon le TP (au lieu de 1–2). Mais **pas d'edge démontrable** :
- Balayage TP @ x_long65/x_short60 : best = TP 1,6%/SL 0,8% → **−0,03%** (PF 1,00, winrate 33%).
- Balayage seuils @ TP1,6% : best = x_long120/x_short320 → **+2,41%** (PF 1,05, winrate 35%), mais c'est le
  max de 16 cellules majoritairement autour de 0 → **bruit, probablement sur-ajusté**.
- **Le winrate stagne à 33–35%**, soit pile la limite de rentabilité d'un ratio 2:1, **AVANT frais**. Avec
  ~0,05–0,09% de frais taker × ~90 trades (~5–8%), la stratégie est **nette négative**.
- **Les seuils x_long/x_short ne bougent quasi pas le nombre de trades** (97→85 de 65 à 320) : l'entrée est
  dominée par le motif barre-AO + couleur de bougie, pas par la magnitude AO. ⇒ le tuning LLM de x_long/x_short
  (Phase 2) a peu de chances de créer de l'edge.

### v3 — balayage du ratio TP/SL @ TP 1,6% (x_long65/x_short60)
Ratio 1,5→5 : le **winrate suit EXACTEMENT la ligne de break-even `1/(1+ratio)`** à chaque pas (edge entre
−1,6 et +0,5 pp), PF coincé entre 0,90 et 1,02. Resserrer le SL pour viser un meilleur ratio fait chuter le
winrate d'autant → les deux se compensent. **Signature d'une absence d'edge** : cotes équitables, le ratio
TP/SL n'est pas un levier. (ratio 3 → winrate 24,2% pour 25% requis ; ratio 5 → 17,2% pour 16,7%, +1,05% =
bruit, et frais non modélisés.)

### v4 — motif AO zero-cross BTC 1h (~200j, motif ≠ seuils)
Backtester `_signals_ao_zerocross` (LONG/SHORT au franchissement de 0, sans seuil). Sur **200j** le winrate
décolle de la ligne break-even (ratio 3 → 26–30% pour 25% requis), best TP6%/SL2% ratio3 → **+18% PF 1,27**
(47 trades). **MAIS test out-of-sample destructeur** : rejoué sur les **100j récents**, ce même couple fait
**−12%** (winrate 18%, PF 0,67) et toute la zone prometteuse est négative (−6 à −28%). ⇒ le +18% venait
intégralement de la **moitié ancienne** des données (régime favorable passé) → **sur-ajustement /
non-stationnarité, pas d'edge persistant.**

### Conclusion / décision attendue
**Deux motifs testés (seuils 5m, zero-cross 1h) → aucun edge robuste sur BTC.** Le 5m hugge la ligne
break-even (cotes équitables) ; le 1h zero-cross ne tient pas hors échantillon. La **mécanique** (entrées sur
barre clôturée, TP+SL gérés, BTC réservé, hors allocateur, backtester + sweep TP/ratio/motif) est saine et
réutilisable. **Recommandation : laisser l'AO OFF / en pause** sauf si francois veut tester une variante
supplémentaire (autre symbole, AO+filtre tendance HTF, divergence) — avec **validation walk-forward + frais
modélisés** obligatoire avant de croire un PnL positif. **Reste OFF par défaut.**

### Harnais d'évaluation construit (2026-06-18)
Le garde-fou anti-overfit demandé existe désormais : `backtest/evaluator.py` (`WalkForwardEvaluator`) +
`backtest/run_walkforward.py`. Split train/test walk-forward, **frais HL modélisés** (taker 0,045%/côté),
**gate OOS** (pnl>0, ≥80% folds positifs, PF médian ≥1,05, t-stat ≥1,5 ; ~4% faux positifs sur marche
aléatoire). Passé sur l'AO zero-cross 1h : **in-sample +13,8% → OOS +0,4%, gate REJET** (écart overfit 13,4 pts,
params instables fold à fold). Toute variante future DOIT passer ce gate avant `enabled=true`. Détails :
[[project_eval_harness]].

## Contexte session 2026-06-17 (hors AO, déjà résolu)
- LocalAI avait perdu le GPU (NVML Unknown Error post unattended-upgrades daemon-reload) → governor mort 3h.
  Résolu : `docker compose up -d --force-recreate`. Voir mémoire `project_localai_gpu_cgroup`.
- Protections posées : `apt-mark hold` sur 24 paquets nvidia/libnvidia (rien ne touche au GPU sans accord)
  + auto-heal `~/.config/systemd/user/localai-gpu-heal.timer` (check 2min, force-recreate si NVML KO).
- Bug `prob_range` (audits) = déjà corrigé en code (main.py:604), 0 depuis restart 11:30.

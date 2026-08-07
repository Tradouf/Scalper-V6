# Brief Claude Code — SimpleBot (handoff Grok 30/07/2026)

> ⚠️ **OBSOLÈTE depuis le 2026-08-07** : le live EMA-cross est **arrêté
> définitivement** (verdict statistique : sélection = bruit, 0/48 sets
> rentables pooled, signal nu nul). Lire **`VERDICT_2026-08-07.md`** avant
> toute reprise de travail sur SimpleBot. La grille est gelée ; l'effort est
> reporté sur `xsmom/` (infra portée) et `placebo_gate.py` (racine repo).

> **Source** : analyse Grok Build du 2026-07-30.  
> **Remplace** le brief du 11/07 pour le travail restant.  
> **Bot concerné** : SimpleBot live HL2 (pas V6, pas SuperBot).  
> **Ne pas committer de secrets** ; ne pas logger de clés privées.

---

## Contexte runtime (snapshot 30/07 ~11h UTC)

| Item | État |
|---|---|
| Process live | `start_simplebot.sh --live` PID 132422 depuis 27/07, `SIMPLEBOT_DRY_RUN=0` |
| Paper A/B | `experiments/ab14/` — simplebot + llmbot paper depuis 28/07 |
| Equity live lue | **209,09 $** (saut 149→209 à 08:25 UTC = **dépôt ~60 $**, pas du PnL) |
| Positions live | **0** (flat), kill-switch off |
| Symboles actifs | **3/40** : `KAITO`, `PENGU`, `JTO` (optim 08:40 UTC) |
| Majors | BTC/ETH/SOL **inactifs** (aucun set confirmé walk-forward) |
| exec_stats | maker=39, taker=28, mixed=0, skip=1 |
| closed_trades | 48 total, **45 sans pnl** (`EXCHANGE_CLOSE`), **3 avec pnl** |
| PnL tracké | JTO TP +2.80%, JTO TP +1.82%, WLD SL −0.98% (~+1.26 $) |
| Momentum paper | equity **200→138.8**, 390 trades, **WR 23%**, 60 pos ouvertes — **ne pas passer live** |
| A/B paper | **0 trade** des deux bras après ~2 j ; LLMBot : « aucun setup quant ≥ 65 » en boucle |
| Tests | `pytest tests/test_simplebot.py tests/test_execution.py` → **66 passed** (30/07) |
| Branch | `claude/trading-algo-backtest-j1rbe1` — diffs locaux non commités sur simplebot |

Fichiers d’état à lire (pas de secrets) :
- `simplebot/state/best_params.json`
- `simplebot/state/live_state.json`
- `simplebot/state/momentum_state.json`
- `experiments/ab14/README.md` + `report/metrics.csv`
- `logs/simplebot.log`

---

## ✅ Déjà fait (ne pas refaire)

Issu du brief 11/07 + commits récents :

- Filtre symboles post-optimiseur (`symbol_filter.py`) — seuils assouplis juil. 2026 :
  `QUALITY_MIN_VALID_PF=1.30`, `PNL≥1.5%`, `WR≥38%`, `MAX_ACTIVE_SYMBOLS=12`
- Kill-switch durci : equity canonique portfolio, hystérésis `KILL_CONFIRMATIONS=2`,
  fail-safe lecture, clamp fantôme perp (`EQUITY_CANON_TOL`)
- Anti-churn : `FLIP_COOLDOWN_BARS=2` (flip + re-entry post close)
- Maker-first entrées (`execution.py`) — défaut **sans** market fallback
- Sizing dynamique par quality_score (`SIZING_DYNAMIC`, `MARGIN_PCT_MAX`)
- Trend EMA 200 **fixe hors grille** + `MIN_RR_RATIO=1.5`
- TP/SL natifs + réconciliation positions nues au boot
- Wallet HL2 séparé (refus si = V6)
- Rate-limit HL inter-process + cache OHLCV partagés
- Dashboard 8083, momentum paper isolé (pas d’exchange client)

---

## Verdict Grok (à prendre comme point de départ)

**SimpleBot est le bot le mieux construit du monorepo** (sécurités live, walk-forward
binaire anti-overfit, maker-first, tests verts).

**Le live n’est pas encore prouvé profitable** :
1. +40 % equity = dépôt, pas alpha
2. Comptabilité closes quasi aveugle (45/48 sans PnL)
3. Throughput trop bas (3 alts) pour juger en quelques jours
4. Momentum paper contredit l’OOS historique
5. A/B 14j inutilisable tant que 0 trade

---

## P0 — Urgent (capital / mesure)

### 1. PnL sur chaque `EXCHANGE_CLOSE` ⚠️ trou de mesure

**Symptôme** : `live_state.json` → `closed_trades` avec `reason=EXCHANGE_CLOSE`
n’a que `symbol, dir, entry, reason, closed_at` — **pas** `exit`, `pnl_pct`,
`pnl_usd`, `fee`, `notional`. Seuls 3 trades TP/SL manuels ont le PnL.

**Impact** :
- Impossible de calculer PF live / expectancy / winrate réel
- Le **live gate** (`LIVE_GATE_MIN_TRADES=8`, `LIVE_GATE_MIN_PF=0.9`) ne peut pas
  désactiver un symbole pourri faute de closes mesurés
- Dashboard et décisions de sizing faussés

**Fichiers** : `simplebot/live_trader.py` (`_sync_exchange_closes`, tracking
`live_tracked`), éventuellement `simplebot/data.py` (`fetch_ledger_updates`),
client HL fills/user fills.

**À faire** :
1. Au sync position disparue : résoudre fill de sortie (avg px, fee) via ledger
   ou `userFills` HL entre `entry_ts` et now.
2. Écrire `exit`, `pnl_usd`, `pnl_pct` (sur notional ou ROE — **documenter le
   convention et rester cohérent**), `fee`, `notional`, `reason` (`TP`/`SL`/
   `EXCHANGE`/`FLIP`/`MANUAL` si détectable).
3. Backfill best-effort des 45 closes orphelins si fills historiques dispo ;
   sinon marquer `pnl_pct=null, incomplete=true`.
4. Tests : mock position open → close exchange → assert trade complet dans state.
5. Afficher sur dashboard : WR / PF / cum PnL **uniquement** sur trades complets.

**DoD** : tout nouveau close live a un PnL non-null ou un flag `incomplete`
explicite ; test vert.

### 2. Ne jamais activer Momentum live

**Preuve paper** (30/07) :
- equity 200 → 138.8 (−30 %)
- 390 trades, WR 22.8 %
- exits : 259 FLIP / 128 SL / 3 TIME
- 60 positions ouvertes (cap 0 = illimité)

**Règle** : momentum reste `paper only` tant que :
- WR paper ≥ 35 % sur ≥ 14 j glissants **et**
- equity paper ≥ start × 0.95 **et**
- funding cumulé documenté

Pas de `momentum_live.py` dans cette session. Optionnel P2 : cap
`MOMENTUM_MAX_OPEN=10` + filtre funding pour assainir le paper.

---

## P1 — Profit / frictions

### 3. Réduire les fills taker (encore 42 % des fills)

Stats live : maker 39 / taker 28. Les entrées sont maker-first ; les **flips et
sorties** restent probablement market.

**À faire** (choisir 1–2, pas tout d’un coup) :
- Option A : flip en maker-first (même helper `smart_entry` / smart close) ou
  **skip flip** si PnL latent > −0.5 % (laisser TP/SL natifs)
- Option B : allonger `FLIP_COOLDOWN_BARS` 2 → 3–4 si logs montrent encore des
  rafales
- Compteur dashboard : ratio maker/taker **par type** (entry / flip / close)

**DoD** : test + log structuré ; pas de régression entrée maker.

### 4. Observabilité dépôts / retraits

Le jump 149→209 a l’air d’un dépôt. `fetch_ledger_updates` / `net_transfer_flow`
existent déjà côté data.

**À faire** :
- Taguer les points d’equity `deposit|withdraw|trading` dans `equity_history`
  ou série parallèle
- Kill-switch : baser le pic sur equity **ajustée des transferts** (sinon un
  retrait légitime peut tuer le bot ; un dépôt masque une perte trading)
- Carte dashboard « PnL trading vs net transfers »

### 5. Live gate réellement branché sur PnL complets

Une fois P0#1 fait : après `LIVE_GATE_MIN_TRADES` closes **complets** d’un
symbole, si PF < `LIVE_GATE_MIN_PF` → `live_disabled[sym]` jusqu’au prochain
reload optimiseur (code partiellement là — vérifier qu’il consomme les bons
champs).

---

## P2 — Expérience A/B 14 j (llmbot vs simplebot)

**Path** : `experiments/ab14/`  
**Start** : 2026-07-28 10:13 UTC  
**Problème** : 0 trade bras A et B → J+14 non décidable.

### Bras B (llmbot)
- Log en boucle : `aucun setup quant ≥ 65`
- **À faire** : baisser `LLMBOT_MIN_QUANT_SCORE` (essai 55 puis 50), logger
  distribution des scores quant chaque cycle (p50/p90/max), vérifier LocalAI
  uptime

### Bras A (simplebot paper)
- State isolé OK (`SIMPLEBOT_STATE_DIR=.../ab14/simplebot_state`)
- Log quasi mort après double start 12:13/12:14
- **À faire** : vérifier `best_params.json` figés du bras A ont des `active:true` ;
  si 0 actif, le paper ne traderait jamais — re-générer avec
  `filter_best_params.py` / optim one-shot dans le state A
- Confirmer `SIMPLEBOT_MOMENTUM=0` bien respecté (log initial montrait momentum
  start — possible bug ou double spawn)

### Process
- `snapshot_daily.sh` n’a produit que `day_00` — relancer / cron
- Ne **jamais** pointer l’A/B sur `simplebot/state/` live ni `DRY_RUN=0`

---

## P3 — Hors scope immédiat (ne pas digresser)

- V6 multi-agents / gate multi-TF / XGB (bot arrêté)
- SuperBot HMM / sleeves
- Passage live momentum
- Élargir la grille EMA pour forcer BTC/ETH actifs (overfit risk)

---

## Fichiers clés à toucher (P0/P1)

```
simplebot/live_trader.py   # _sync_exchange_closes, closed_trades schema, live gate
simplebot/data.py          # fills / ledger helpers si manquants
simplebot/execution.py     # éventuel smart_close / flip maker
simplebot/dashboard.py     # métriques trades complets, transfers
simplebot/config.py        # flags si besoin (pas de changement gratuit)
tests/test_simplebot.py
tests/test_execution.py
experiments/ab14/*         # seulement si on attaque P2
```

**Diffs locaux déjà présents** (git status au moment du brief) — les relire avant
d’éditer pour ne pas écraser du travail en cours :
`config.py`, `execution.py`, `live_trader.py`, `optimizer.py`, `strategy.py`,
tests associés.

---

## Commandes

```bash
cd /home/francois/Scalper-V6
source .venv/bin/activate

# Tests (ne pas casser)
python -m pytest tests/test_simplebot.py tests/test_execution.py -v

# État live (lecture seule)
python -c "import json; d=json.load(open('simplebot/state/live_state.json')); print('dry',d.get('dry_run'),'n_closed',len(d.get('closed_trades')or[]),'exec',d.get('exec_stats'))"
python -c "import json; b=json.load(open('simplebot/state/best_params.json')); print([s for s,v in b['symbols'].items() if v.get('active')])"

# A/B paper (ne touche pas au live)
tail -20 experiments/ab14/logs/llmbot_paper.log
tail -20 experiments/ab14/logs/simplebot_paper.log
bash experiments/ab14/snapshot_daily.sh

# Live : NE PAS relancer si PID 132422 tourne déjà (lock single-instance)
pgrep -af 'simplebot.run'
```

---

## Definition of Done (session Claude Code)

1. **P0#1** : closes exchange → PnL (ou `incomplete=true`) + test unitaire vert
2. **P0#2** : confirmé dans le code/docs que momentum live n’existe pas / n’est
   pas branché ; optionnel garde `assert` si quelqu’un ajoute un flag live
3. Au moins **un** item P1 (taker reduction **ou** transfers equity) avec test
4. `pytest tests/test_simplebot.py tests/test_execution.py` vert
5. Pas de `SIMPLEBOT_DRY_RUN=0` dans les scripts de test
6. Pas de commit de `.env`, locks, ni state runtime
7. Si commit : message clair, scope SimpleBot only

---

## Prompt de reprise suggéré (coller dans Claude Code)

```
Lis simplebot/BRIEF_CLAUDE_CODE.md (handoff Grok 30/07/2026).

Priorité stricte :
1) P0#1 — instrumenter le PnL de chaque EXCHANGE_CLOSE dans live_trader
   (+ test)
2) Vérifier que le live gate consomme ces PnL
3) Si temps : P1 réduction taker sur flips OU tag dépôts/retraits equity

Contraintes :
- Ne pas activer momentum live
- Ne pas toucher au wallet / process live en cours sans demander
- Ne pas casser l’isolation experiments/ab14
- pytest simplebot + execution doit rester vert
- Relire les diffs git locaux avant d’éditer les mêmes fichiers

Commence par un diagnostic court de _sync_exchange_closes / closed_trades,
puis implémente P0#1.
```

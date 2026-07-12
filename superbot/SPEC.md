# SuperBot — Spécification complète pour Claude Code

> **Objectif** : bot de trading Hyperliquid déterministe, sans LLM, qui combine les
> seules edges empiriquement validées du projet Scalper-V6 (juillet 2026).
> À implémenter dans `superbot/`, indépendant de V6 et SimpleBot.

---

## 1. Pourquoi un nouveau bot (et pas SimpleBot tel quel)

| Constat empirique | Source | Implication SuperBot |
|---|---|---|
| Edge brut réel, frais taker le tuent | MinuteLab walk-forward 72h | **Maker-first obligatoire** sur toutes les entrées |
| EMA 15m marche sur certains alts (XPL, SOL) mais pas BTC/ETH | Optimiseur 11/07/2026 | **Multi-timeframe par symbole** (15m / 1h / 4h) |
| Momentum 4h = meilleur OOS historique (833j, t-stat 2.9) | `simplebot/momentum.py` | Sleeve dédiée, **sans TP**, params figés |
| Paper momentum juillet décevant (WR ~19%) | Brief 11/07/2026 | **Filtre funding** + validation paper 14j avant live |
| `trend_ema` améliore l'edge (+23%) mais grille = overfit | `simplebot/strategy.py` L66-73 | Filtre **fixe** EMA200, pas dans la grille |
| LLM V6 = lent, cher, consensus bloqué en range | `main_v6.py` | **Zéro LLM** — HMM par symbole + marché |
| XPL marche, BTC non (même stratégie) | Optimiseur 11/07/2026 | **HMM propre à chaque coin**, pas un régime global unique |
| Scalping 1m non viable même à signal brut positif | `minutelab/README.md` | **Pas de timeframe < 15m** |

**Promesse honnête** : pas de gains garantis. SuperBot vise à maximiser l'EV/trade
net de frais en ne tradant que quand plusieurs couches de validation confirment.

---

## 2. Architecture

```
superbot/
├── __init__.py
├── config.py              # env SUPERBOT_*, wallet HL3_*
├── run.py                 # point d'entrée
├── regime.py              # façade régime (HMM + fallback règles)
├── hmm.py                 # Gaussian HMM entraîné offline, inférence online
├── markov.py              # pseudo-HMM / transitions (adapté de regime_engine V6)
├── orchestrator.py        # allocation capital entre sleeves + slots
├── risk.py                # kill-switch portfolio, corrélations, daily loss
├── data.py                # fetch OHLCV/funding (réutiliser simplebot/data.py)
├── execution.py           # wrapper maker-first (réutiliser simplebot/execution.py)
├── optimizer.py           # walk-forward multi-TF unifié
├── symbol_filter.py       # reprendre logique simplebot + score composite
├── sleeves/
│   ├── base.py            # interface Sleeve : signal(), on_bar(), sizing()
│   ├── momentum.py        # Sleeve A — ROC 4h, pas de TP
│   ├── adaptive_ema.py    # Sleeve B — EMA cross multi-TF optimisé
│   └── breakout.py        # Sleeve C — Donchian + ATR squeeze 1h
├── backtester.py          # simulation unifiée (maker + funding)
├── live_trader.py         # exécution live, TP/SL natifs
├── dashboard.py           # stdlib, lecture seule
├── state/                 # JSON non versionnés
│   ├── best_params.json
│   ├── live_state.json
│   ├── regime_market.json       # HMM global BTC 4h
│   ├── regime_symbols.json      # HMM par symbole actif
│   ├── hmm/
│   │   ├── market.pkl           # modèle global
│   │   └── {SYMBOL}.pkl         # un fichier par symbole actif
│   └── optimizer_history.jsonl
└── tests/
    └── test_superbot.py
```

**Wallet** : `HL3_PRIVATE_KEY` / `HL3_ACCOUNT_ADDRESS` — troisième wallet, refus
de démarrer si identique à HL_* ou HL2_*.

---

## 3. Les trois sleeves (stratégies)

### Sleeve A — Momentum 4h (`sleeves/momentum.py`)

Reprend la logique validée OOS de `simplebot/momentum.py` avec corrections paper :

| Paramètre | Valeur | Figé |
|---|---|---|
| ROC période | 12 bougies 4h (= 48h) | ✅ |
| Seuil entrée | ±2 % | ✅ |
| Take-profit | **AUCUN** | ✅ |
| Stop-loss | 2 × ATR(14), natif exchange | ✅ |
| Time-exit | 72 bougies 4h (12 jours) | ✅ |
| Flip | signal opposé → fermer + rouvrir | ✅ |

**Filtres live ajoutés (obligatoires)** :
```python
# Ne pas LONG si funding horaire > +0.01% (on paie la foule)
# Ne pas SHORT si funding horaire < -0.01%
# Ne pas entrer si spread > 0.15%
# Max 6 positions momentum simultanées
```

**Allocation capital** : 35 % du wallet (configurable `SUPERBOT_MOMENTUM_ALLOC`).

### Sleeve B — Adaptive EMA (`sleeves/adaptive_ema.py`)

Évolution de SimpleBot avec **choix automatique du timeframe** par symbole.

**Signaux** (identiques à `simplebot/strategy.py`) :
- Croisement EMA fast/slow
- Filtre RSI (pas de long si RSI > 75, pas de short si RSI < 25)
- Filtre directionnel **fixe** : EMA200 — long seulement si close > EMA200, short si close < EMA200

**Grille d'optimisation** (par symbole × timeframe) :
```python
TIMEFRAMES = ["15m", "1h"]   # pas 4h (réservé momentum), pas 1m
ema_fast  = [9, 12, 21]
ema_slow  = [26, 50, 100]    # contrainte slow >= 2 × fast
tp_atr    = [1.5, 2.5, 3.5]
sl_atr    = [1.0, 1.5, 2.0, 3.0, 4.0]
# trend_ema = 200 FIXE, hors grille
```

**Sélection du timeframe** : pour chaque symbole, l'optimiseur teste 15m ET 1h ;
le timeframe dont le **meilleur set train** a le meilleur score composite est retenu.
Publier dans `best_params.json` :
```json
{
  "SOL": {
    "active": true,
    "timeframe": "15m",
    "sleeve": "adaptive_ema",
    "params": {"ema_fast": 21, "ema_slow": 100, "tp_atr": 3.5, "sl_atr": 2.0},
    "train": {...},
    "valid": {...}
  }
}
```

**Walk-forward** (reprendre règles SimpleBot) :
1. Classer sur train (70 %)
2. Validation = filtre binaire sur les TOP_K du train
3. Premier set train qui confirme (PF ≥ 1.2, PnL > 0, ≥ 5 trades valid) → publié
4. **Jamais** sélectionner sur le PnL de validation

**Filtre qualité** (identique SimpleBot, seuils conservateurs) :
- `QUALITY_MIN_VALID_PF = 1.4`
- `QUALITY_MIN_VALID_PNL_PCT = 0.02`
- `QUALITY_MIN_VALID_WINRATE = 0.40`
- `MAX_ACTIVE_SYMBOLS = 8`

**Allocation capital** : 45 % (`SUPERBOT_EMA_ALLOC`).

### Sleeve C — Breakout 1h (`sleeves/breakout.py`)

Stratégie **nouvelle** mais simple — complémentaire aux EMA (moins de trades,
meilleur R:R en tendance forte).

**Signaux** :
```python
# Donchian(20) sur 1h
# LONG  : close > highest_high(20) ET ATR(14) > ATR_sma(50)  (expansion volatilité)
# SHORT : close < lowest_low(20)  ET idem
# SL    : 1.5 × ATR(14) natif
# TP    : 3.0 × ATR(14) natif
# Time-exit : 48 bougies 1h (2 jours) si ni TP ni SL
```

**Optimisation** (grille réduite, 24 combinaisons) :
```python
donchian_len = [15, 20, 30]
sl_atr       = [1.0, 1.5, 2.0]
tp_atr       = [2.5, 3.0, 4.0]
atr_expansion = [True]   # filtre volatilité toujours actif
```

Même walk-forward + filtre qualité que Sleeve B.

**Allocation capital** : 20 % (`SUPERBOT_BREAKOUT_ALLOC`).

---

## 4. Orchestrateur (`orchestrator.py`)

À chaque cycle (30s), l'orchestrateur :

1. Lit le **régime marché** (HMM BTC 4h) → autorise ou bloque les sleeves
2. Lit le **régime par symbole** (HMM propre à chaque coin) → autorise ou bloque
   l'entrée sur CE symbole + ajuste le sizing
3. Lit `best_params.json` (params par symbole/sleeve)
4. Applique la **double gate** (marché ET symbole doivent être OK)
5. Priorise les entrées par `quality_score × hmm_symbol_confidence`
6. Passe les signaux au `live_trader.py`

### Détection de régime — architecture à deux niveaux

SuperBot utilise les **chaînes de Markov cachées (HMM)** à **deux niveaux** :
un HMM **global** (BTC = proxy du marché) et un HMM **par symbole** (régime
propre à chaque coin tradé). Fallback déterministe si modèle absent ou confiance
basse. Zéro LLM.

```
                    COUCHE 1 — HMM MARCHÉ (BTC 4h, K=4)
                    ═══════════════════════════════════
                    Autorise / bloque les SLEEVES entières
┌─────────────────────────┐     ┌────────────────────────────┐
│ log-return, ATR%, ADX,  │ ──► │ bull_orderly               │
│ funding BTC             │     │ bear_orderly               │
└─────────────────────────┘     │ range_compressed           │
                                │ high_vol_chaotic           │
                                └─────────────┬──────────────┘
                                              │ gate sleeves
                    COUCHE 2 — HMM PAR SYMBOLE (K=3, chaque coin actif)
                    ═══════════════════════════════════════════════════
                    Autorise / bloque l'ENTRÉE sur ce symbole + sizing
┌─────────────────────────┐     ┌────────────────────────────┐
│ log-return, ATR%, ADX,  │ ──► │ trending_up   → longs OK  │
│ RSI distance, vol ratio │     │ trending_down → shorts OK  │
│ (timeframe de la sleeve)│     │ choppy        → NO ENTRY   │
└─────────────────────────┘     └─────────────┬──────────────┘
                                              │ gate par symbole
                                              ▼
                                    Signal sleeve → live_trader
```

#### Niveau 1 — HMM Marché (`hmm.py` → `state/hmm/market.pkl`)

**Bibliothèque** : `hmmlearn` (dépendance unique ajoutée : `hmmlearn>=0.3.0`).

**États latents** (K = 4, interprétés post-entraînement par les moyennes) :
| État | Interprétation | Sleeves favorisées |
|---|---|---|
| `bull_orderly` | Tendance haussière, vol modérée | A + B + C |
| `bear_orderly` | Tendance baissière, vol modérée | A + B + C |
| `range_compressed` | ADX bas, vol basse | B seulement |
| `high_vol_chaotic` | Vol élevée, direction floue | A réduit, B réduit, C off |

**Features d'observation** (vecteur 4D, standardisé) :
```python
obs = [
    log_return_1bar,           # close[t] / close[t-1] - 1
    atr_pct,                   # ATR(14) / close
    adx_norm,                  # ADX(14) / 100
    funding_hourly,            # taux funding HL courant
]
```

**Entraînement** (offline, toutes les 7 jours ou à chaque `optimize-once`) :
```python
from hmmlearn.hmm import GaussianHMM

model = GaussianHMM(
    n_components=4,
    covariance_type="diag",    # robuste, peu de params
    n_iter=200,
    random_state=42,
)
model.fit(X_train)             # X_train = features BTC 4h sur 180 jours
# Sauvegarder : state/hmm_model.pkl + mapping état → label
```

**Walk-forward pour le HMM** (anti-overfit) :
- Entraîner sur les 70 % premiers jours
- Vérifier sur les 30 % restants que la **log-vraisemblance out-of-sample** ne
  dégrade pas de plus de 10 % vs in-sample
- Vérifier que les 4 états restent **séparés** (distance inter-moyennes > seuil)
- Si validation échoue → repasser en mode fallback (règles ADX)

**Inférence live** (online, à chaque nouvelle bougie 4h) :
```python
state_probs = model.predict_proba(obs_current)   # forward filtering
regime = LABEL_MAP[state_probs.argmax()]
confidence = state_probs.max()
transition_risk = 1.0 - model.transmat_[current_state, current_state]
```

**Hystérésis HMM** (obligatoire — évite le churn de régime) :
```python
# Le régime ne change que si :
#   1. confiance > SUPERBOT_HMM_MIN_CONF (défaut 0.55)
#   2. le nouvel état est majoritaire sur les 2 dernières bougies 4h
#   3. transition_risk < 0.45 (sinon on reste sur l'état précédent)
```

#### Niveau 2 — Pseudo-HMM Markov (`markov.py`) — filet de sécurité

Adapté de `agents/regime_engine.py` (V6) — **pas un vrai HMM**, mais utile comme
fallback et pour enrichir les métriques :

```python
# Compte les transitions discrètes sur l'historique des états observés
trend_markov = markov_transition_stats(states_history, current_state)
# → stay_probability, switch_probability, next_probs{bull,bear,range}

# Lisse l'état latent avec inertie (pseudo-HMM)
latent = compute_latent_state(
    trend_state, trend_conf, trend_markov_stay,
    vol_state, vol_conf, vol_markov_stay,
    previous_regime,
)
# → latent_market_state, latent_confidence, transition_risk
```

**Quand utiliser quoi** :

| Condition | Source du régime |
|---|---|
| `hmm_model.pkl` existe ET confiance ≥ 0.55 | HMM Gaussian (`hmm.py`) |
| Modèle absent ou confiance < 0.55 | Fallback ADX (`regime.py` règles simples) |
| Toujours | Pseudo-Markov (`markov.py`) pour `transition_risk` et dashboard |

#### Fallback ADX (`regime.py`) — si HMM indisponible

```python
# Sur BTC 4h (identique à la spec initiale) :
if adx < 20:           regime = "range_compressed"
elif close > ema50 > ema200 and adx >= 25:  regime = "bull_orderly"
elif close < ema50 < ema200 and adx >= 25:  regime = "bear_orderly"
else:                  regime = "high_vol_chaotic"
# Hystérésis : 2 bougies 4h consécutives
```

#### Niveau 1bis — HMM par symbole (`hmm.py` → `state/hmm/{SYMBOL}.pkl`)

**Un modèle par symbole actif** — entraîné sur l'historique propre du coin,
au **timeframe de sa sleeve** (15m ou 1h, lu depuis `best_params.json`).

**États latents** (K = 3, plus simple que le marché — moins de données) :
| État | Interprétation | Entrées autorisées |
|---|---|---|
| `trending_up` | Momentum haussier net | LONG seulement |
| `trending_down` | Momentum baissier net | SHORT seulement |
| `choppy` | Pas de direction claire | **Aucune entrée** |

**Features d'observation** (vecteur 5D, standardisé par symbole) :
```python
obs_symbol = [
    log_return_1bar,              # sur le TF de la sleeve
    atr_pct,                      # ATR(14) / close
    adx_norm,                     # ADX(14) / 100
    rsi_distance,                 # (RSI - 50) / 50  ∈ [-1, 1]
    volume_ratio,                 # volume / SMA(volume, 20)
]
```

**Entraînement** (offline, à chaque cycle optimiseur — 4h) :
```python
# Uniquement pour les symboles qui passent le walk-forward (active=True)
for symbol in active_symbols:
    tf = best_params[symbol]["timeframe"]   # "15m" ou "1h"
    candles = fetch_ohlcv(symbol, tf, days=90)
    X = build_features(candles)
    model = GaussianHMM(n_components=3, covariance_type="diag", n_iter=150)
    model.fit(X_train)   # walk-forward 70/30 comme le marché
    # Validation OOS : log-likelihood + séparation des états
    if valid: save(f"state/hmm/{symbol}.pkl")
    else:     delete stale pkl, symbole tradé avec fallback ADX
```

**Mapping des états** (post-entraînement, par centroïdes) :
```python
# L'état dont la moyenne de log_return est la plus haute → trending_up
# La plus basse → trending_down
# Celui avec ADX le plus bas → choppy
LABEL_MAP = assign_labels_by_centroids(model.means_)
```

**Inférence live** (à chaque nouvelle bougie du symbole) :
```python
sym_regime = infer_symbol_hmm(symbol, obs_now)
# → {"state": "trending_up", "confidence": 0.68, "transition_risk": 0.22}

# Gate d'entrée :
if signal == +1 and sym_regime["state"] != "trending_up":
    skip("hmm_symbol: pas trending_up")
if signal == -1 and sym_regime["state"] != "trending_down":
    skip("hmm_symbol: pas trending_down")
if sym_regime["state"] == "choppy":
    skip("hmm_symbol: choppy")
if sym_regime["confidence"] < SUPERBOT_HMM_SYMBOL_MIN_CONF:  # défaut 0.50
    skip("hmm_symbol: confiance basse")
```

**Sizing par symbole** :
```python
hmm_size_mult = {
    "trending_up":   1.0 if signal == +1 else 0.0,
    "trending_down": 1.0 if signal == -1 else 0.0,
    "choppy":        0.0,
}[sym_regime["state"]] * sym_regime["confidence"]
# margin_pct_effectif = margin_pct × hmm_size_mult × market_size_mult
```

**Hystérésis par symbole** (identique au marché) :
- Confiance min 0.50 (`SUPERBOT_HMM_SYMBOL_MIN_CONF`)
- 2 bougies consécutives pour changer d'état
- `transition_risk > 0.50` → pas de nouvelle entrée sur ce symbole

**Fallback par symbole** (si pas de `.pkl` ou validation échouée) :
```python
# Règles ADX + EMA50 sur le TF de la sleeve :
if adx < 18:                    state = "choppy"
elif close > ema50 and adx >= 22: state = "trending_up"
elif close < ema50 and adx >= 22: state = "trending_down"
else:                           state = "choppy"
```

**API unifiée** (`hmm.py`) :
```python
class HMMRegimeEngine:
    def fit_market(candles_btc_4h) -> MarketModel
    def fit_symbol(symbol, candles, timeframe) -> SymbolModel | None
    def infer_market(obs) -> MarketRegime
    def infer_symbol(symbol, obs) -> SymbolRegime
    def prune_stale(active_symbols: set)  # supprime les .pkl des symboles inactifs
```

#### Impact sur l'orchestrateur — double gate

**Gate 1 — Marché** (sleeves autorisées) :

| État marché | Sleeve A | Sleeve B | Sleeve C | Sizing marché |
|---|---|---|---|---|
| `bull_orderly` | ✅ | ✅ | ✅ | ×1.0 |
| `bear_orderly` | ✅ | ✅ | ✅ | ×1.0 |
| `range_compressed` | ❌ | ✅ | ❌ | ×0.7 |
| `high_vol_chaotic` | ✅ | ✅ | ❌ | ×0.5 |

**Gate 2 — Symbole** (entrée autorisée sur ce coin) :

| État symbole | LONG | SHORT | Sizing symbole |
|---|---|---|---|
| `trending_up` | ✅ | ❌ | × confiance |
| `trending_down` | ❌ | ✅ | × confiance |
| `choppy` | ❌ | ❌ | ×0 (pas d'entrée) |

**Règles combinées** :
```python
def allow_entry(signal, symbol, sleeve):
    mkt = infer_market()
    sym = infer_symbol(symbol)
    if mkt.transition_risk > 0.50:          return False, "market_transition"
    if sym.transition_risk > 0.50:          return False, "symbol_transition"
    if not sleeve_allowed(sleeve, mkt.state): return False, "sleeve_blocked"
    if signal == +1 and sym.state != "trending_up":   return False, "hmm_no_long"
    if signal == -1 and sym.state != "trending_down": return False, "hmm_no_short"
    if sym.confidence < HMM_SYMBOL_MIN_CONF:        return False, "hmm_low_conf"
    return True, "ok"
```

Persister dans `state/regime_market.json` :
```json
{
  "regime": "bull_orderly",
  "confidence": 0.72,
  "transition_risk": 0.18,
  "source": "hmm",
  "state_probs": [0.72, 0.08, 0.12, 0.08],
  "updated_at": "..."
}
```

Persister dans `state/regime_symbols.json` :
```json
{
  "SOL": {
    "state": "trending_up",
    "confidence": 0.68,
    "transition_risk": 0.22,
    "source": "hmm",
    "timeframe": "15m",
    "allowed": {"long": true, "short": false},
    "updated_at": "..."
  },
  "XPL": {
    "state": "trending_down",
    "confidence": 0.61,
    "transition_risk": 0.15,
    "source": "hmm",
    "timeframe": "15m",
    "allowed": {"long": false, "short": true},
    "updated_at": "..."
  }
}
```

#### Dépendance

Ajouter à `requirements.txt` :
```
hmmlearn>=0.3.0
```

Pas de PyTorch, pas de TensorFlow — `hmmlearn` est léger (~200 KB).

#### Tests HMM obligatoires

```python
# Marché
test_hmm_market_train_and_save()
test_hmm_market_inference_confidence()
test_hmm_market_fallback_when_no_model()
test_hmm_market_hysteresis_blocks_flip()
test_hmm_market_walkforward_rejects_bad()

# Par symbole
test_hmm_symbol_train_per_active_symbol()
test_hmm_symbol_label_mapping_by_centroids()   # trending_up = centroïde return max
test_hmm_symbol_blocks_long_in_trending_down()
test_hmm_symbol_blocks_entry_in_choppy()
test_hmm_symbol_fallback_when_no_pkl()
test_hmm_symbol_prune_stale_pkls()
test_hmm_symbol_sizing_scales_with_confidence()

# Markov + orchestrateur
test_markov_transition_probs()
test_orchestrator_double_gate_market_and_symbol()
test_orchestrator_freezes_on_high_transition_risk()
test_orchestrator_allows_long_only_when_trending_up()
```

#### Risques connus et garde-fous

| Risque | Garde-fou |
|---|---|
| Overfit HMM marché sur 180j | Walk-forward log-vraisemblance OOS |
| Overfit HMM symbole sur 90j | Walk-forward + rejet si < 60 bougies dispo |
| États non interprétables | Mapping par centroïdes ; rejet si overlap |
| Churn de régime → frais | Hystérésis 2 bougies + seuil confiance |
| 40 symboles × fit HMM = lent | Entraîner **uniquement les actifs** (~8 max) ; parallèle optionnel |
| HMM dégénéré (1 état domine) | Rejet si un état > 80 % du temps en train |
| Symbole nouveau (< 30j listing) | Pas de HMM → fallback ADX jusqu'à 60 bougies |
| Latence entraînement | Offline dans optimiseur (4h), inférence < 1ms/symbole |

---

## 5. Gestion du risque (`risk.py`)

### Kill-switch portfolio
```python
DAILY_LOSS_LIMIT_PCT = 0.03      # -3% journalier → flatten tout + pause 12h
PORTFOLIO_DD_LIMIT   = 0.08      # -8% vs pic 7j → pause 24h
KILL_CONFIRMATIONS   = 2         # hystérésis (leçon incident SimpleBot 04/07)
```

### Limites de positions
```python
MAX_OPEN_TOTAL       = 10        # toutes sleeves confondues
MAX_OPEN_PER_SLEEVE  = {"momentum": 6, "adaptive_ema": 5, "breakout": 3}
MAX_SAME_DIRECTION   = 6         # pas 6 longs corrélés en même temps
```

### Sizing dynamique
```python
# Base : MARGIN_PCT = 0.04 (4% du wallet par trade)
# Interpolation linéaire selon quality_score → max MARGIN_PCT_MAX = 0.07
# Multiplicateur régime : range ×0.7, high_vol ×0.5
# Levier : 3x (défaut)
```

### Corrélation (simplifié)
```python
# Grouper : {BTC, ETH, SOL} = "majors", reste = "alts"
# Max 2 positions même direction dans "majors" simultanément
# Max 4 positions même direction dans "alts" simultanément
```

---

## 6. Exécution (`execution.py` + `live_trader.py`)

**Réutiliser** `simplebot/execution.py` (`smart_entry`) — ne pas réécrire.

Règles live (identiques SimpleBot + ajouts) :
- Entrées : maker-first (limit Alo, timeout 30s, fallback market)
- **SL natif exchange dès l'entrée** (obligatoire — crash-safe)
- TP natif pour Sleeves B et C ; **pas de TP** pour Sleeve A
- Flip cooldown : 2 bougies du timeframe de la sleeve
- Réconciliation au démarrage : positions sans SL → re-protéger ou fermer
- Dry-run par défaut (`SUPERBOT_DRY_RUN=1`)

---

## 7. Backtester unifié (`backtester.py`)

Un seul moteur pour les 3 sleeves avec :
- `entry_mode = "maker"` (modèle déterministe de `simplebot/backtester.py`)
- Funding accru par heure pour Sleeve A
- Frais : maker 0.015%, taker 0.045% + slippage 0.03%
- Métriques : PnL%, PF, winrate, max DD, EV/trade, maker_fill_rate

**Gate de publication** (toutes sleeves) :
```
train : n_trades >= 5, PF >= 1.0, PnL > 0
valid : n_trades >= 5, PF >= 1.2, PnL > 0
qualité : PF >= 1.4, PnL >= 2%, WR >= 40%
```

---

## 8. Optimiseur (`optimizer.py`)

Cycle toutes les **4 heures** (`SUPERBOT_OPTIMIZE_INTERVAL_SEC = 14400`).

```
0. Entraîner / valider HMM marché (BTC 4h, 180j) → state/hmm/market.pkl
   Si validation OOS échoue → régime marché en fallback ADX

Pour chaque symbole dans l'univers (top 40 perps HL par volume) :
  1. Télécharger OHLCV 60 jours (15m, 1h, 4h selon sleeve)
  2. Sleeve B : tester 15m ET 1h, garder le meilleur timeframe
  3. Sleeve C : tester 1h uniquement
  4. Sleeve A : pas d'optimisation (params figés)
  5. Walk-forward 70/30 sur chaque sleeve optimisable
  6. Appliquer symbol_filter (reprendre simplebot/symbol_filter.py)
  7. Écrire atomiquement best_params.json
  8. Pour chaque symbole active=True :
     a. Entraîner HMM symbole (K=3) sur 90j au TF de la sleeve
     b. Valider OOS → state/hmm/{SYMBOL}.pkl ou rejet
  9. Prune : supprimer state/hmm/*.pkl des symboles devenus inactifs
```

---

## 9. Dashboard (`dashboard.py`)

Stdlib, port **8084** (pas 8083 = SimpleBot).

Cartes :
- Régime **marché** (HMM BTC) + probabilités des 4 états
- Table **régime par symbole** : état HMM, confiance, long/short autorisé, source (hmm/fallback)
- Equity + PnL + drawdown (courbe portfolio HL si `HL3_ACCOUNT_ADDRESS` set)
- Table symboles : sleeve, timeframe, actif/inactif, PF train/valid, params, état HMM
- Positions ouvertes par sleeve
- Stats maker vs taker
- Kill-switch status

Auto-refresh 5s. Basic Auth optionnel (`SUPERBOT_DASHBOARD_PASSWORD`).

---

## 10. Configuration (.env)

```bash
# Wallet dédié SuperBot (3ème wallet)
HL3_PRIVATE_KEY=0x...
HL3_ACCOUNT_ADDRESS=0x...

# Mode
SUPERBOT_DRY_RUN=1                    # 0 pour live
SUPERBOT_SYMBOLS=ALL
SUPERBOT_MAX_SYMBOLS=40
SUPERBOT_LEVERAGE=3
SUPERBOT_MARGIN_PCT=0.04
SUPERBOT_MARGIN_PCT_MAX=0.07

# Allocations sleeves (doivent sommer à 1.0)
SUPERBOT_MOMENTUM_ALLOC=0.35
SUPERBOT_EMA_ALLOC=0.45
SUPERBOT_BREAKOUT_ALLOC=0.20

# Risque
SUPERBOT_DAILY_LOSS_LIMIT_PCT=0.03
SUPERBOT_PORTFOLIO_DD_LIMIT=0.08
SUPERBOT_MAX_OPEN_TOTAL=10

# Exécution
SUPERBOT_EXEC_MAKER_FIRST=1
SUPERBOT_FLIP_COOLDOWN_BARS=2

# HMM
SUPERBOT_HMM_MARKET_STATES=4          # états latents marché (BTC)
SUPERBOT_HMM_SYMBOL_STATES=3          # états latents par symbole
SUPERBOT_HMM_MARKET_MIN_CONF=0.55     # confiance min pour changer régime marché
SUPERBOT_HMM_SYMBOL_MIN_CONF=0.50     # confiance min pour autoriser entrée symbole
SUPERBOT_HMM_MARKET_DAYS=180          # historique entraînement marché
SUPERBOT_HMM_SYMBOL_DAYS=90           # historique entraînement par symbole
SUPERBOT_HMM_TRANSITION_FREEZE=0.50   # gel entrées si transition_risk > seuil

# Optimiseur
SUPERBOT_OPTIMIZE_INTERVAL_SEC=14400
SUPERBOT_BACKTEST_DAYS=60
```

---

## 11. Plan d'implémentation (ordre pour Claude Code)

### Phase 1 — Squelette + Sleeve B (2-3 jours)
> La sleeve la plus proche de SimpleBot, réutilise le plus de code existant.

- [ ] `superbot/config.py`, `data.py` (import depuis simplebot)
- [ ] `sleeves/base.py`, `sleeves/adaptive_ema.py`
- [ ] `backtester.py` (copier/adapter simplebot/backtester.py + multi-TF)
- [ ] `optimizer.py` (walk-forward multi-TF)
- [ ] `symbol_filter.py` (copier simplebot)
- [ ] `tests/test_superbot.py` — au minimum :
  - `test_walk_forward_no_overfit_selection`
  - `test_multi_tf_picks_best_interval`
  - `test_quality_filter_demotes_weak_symbols`
  - `test_backtester_maker_mode`

**DoD Phase 1** : `python -m superbot.optimizer` produit un `best_params.json`
avec ≥1 symbole actif ; tests verts.

### Phase 2 — Live + Risque (2 jours)

- [ ] `execution.py` (wrapper simplebot)
- [ ] `live_trader.py` (adapter simplebot/live_trader.py)
- [ ] `risk.py` (kill-switch, corrélations, sizing dynamique)
- [ ] `hmm.py` + `markov.py` + `regime.py`
      (HMM marché K=4 + HMM par symbole K=3 + fallback ADX + pseudo-Markov)
- [ ] `orchestrator.py` (double gate marché+symbole, freeze si transition_risk > 0.5)
- [ ] `run.py` (dry-run par défaut)
- [ ] Tests :
  - `test_kill_switch_hysteresis`
  - `test_flip_cooldown`
  - `test_hmm_market_*` + `test_hmm_symbol_*` (voir §4 tests HMM)
  - `test_orchestrator_double_gate_market_and_symbol`
  - `test_orchestrator_allows_long_only_when_trending_up`
  - `test_no_double_order_per_bar`

**DoD Phase 2** : `python -m superbot.run` tourne en dry-run, logue signaux
et PnL papier, kill-switch mock testé.

### Phase 3 — Sleeves A + C (2 jours)

- [ ] `sleeves/momentum.py` (adapter simplebot/momentum.py + filtres funding)
- [ ] `sleeves/breakout.py` (nouveau)
- [ ] Intégration orchestrateur (allocation par sleeve)
- [ ] Tests :
  - `test_momentum_no_tp`
  - `test_momentum_funding_filter`
  - `test_breakout_donchian_signal`
  - `test_orchestrator_regime_gating`

**DoD Phase 3** : dry-run 48h avec les 3 sleeves, dashboard affiche tout.

### Phase 4 — Dashboard + Paper → Live (1 jour)

- [ ] `dashboard.py` (port 8084)
- [ ] `start_superbot.sh`, `start_superbot_dashboard.sh`
- [ ] Paper 14 jours → décision live
- [ ] README.md dans `superbot/`

**DoD Phase 4** : paper WR ≥ 35%, PF ≥ 1.2 sur 14j → autoriser `SUPERBOT_DRY_RUN=0`.

---

## 12. Code à réutiliser (ne pas réinventer)

| Module existant | Usage SuperBot |
|---|---|
| `simplebot/data.py` | fetch OHLCV, funding, univers perps |
| `simplebot/execution.py` | `smart_entry()` maker-first |
| `simplebot/strategy.py` | `ema()`, `rsi()`, `atr()`, `compute_signals()` |
| `simplebot/backtester.py` | moteur de simulation (étendre) |
| `simplebot/symbol_filter.py` | `apply_symbol_filter()`, `quality_score()` |
| `simplebot/live_trader.py` | patron pour réconciliation, kill-switch, paper |
| `simplebot/momentum.py` | patron Sleeve A |
| `agents/regime_engine.py` | patron pseudo-Markov (`markov.py`) |
| `hyperliquid_client.py` | client exchange |

**Ne PAS réutiliser** : `main_v6.py`, agents LLM, MinuteLab (1m), shared_memory V6.

---

## 13. Métriques de succès (réalistes)

| Horizon | Objectif | Stop |
|---|---|---|
| Paper 14j | PF ≥ 1.2, WR ≥ 35%, DD < 10% | WR < 25% → revoir filtres |
| Live mois 1 | +2-5% net, DD < 8% | -5% en 7j → pause + review |
| Live mois 3 | Sharpe > 1.0, PF > 1.3 | 2 mois négatifs → désactiver sleeve la plus faible |

---

## 14. Commandes cibles

```bash
# Tests
python -m pytest tests/test_superbot.py -v

# Optimisation
python -m superbot.run --optimize-once

# Paper (défaut)
python -m superbot.run

# Live
SUPERBOT_DRY_RUN=0 python -m superbot.run

# Dashboard
python -m superbot.dashboard    # http://localhost:8084
```

---

## 15. Prompt à donner à Claude Code

```
Implémente SuperBot selon superbot/SPEC.md.

Commence par Phase 1 uniquement :
- Crée le squelette superbot/ avec config, sleeves/adaptive_ema, backtester,
  optimizer multi-TF, symbol_filter.
- Réutilise simplebot/data.py, simplebot/strategy.py, simplebot/symbol_filter.py
  (import direct, pas de copie aveugle).
- Walk-forward : premier set train qui confirme en valid, jamais sélection
  sur PnL de validation.
- Tests obligatoires listés en §11 Phase 1.
- Ne touche PAS à main_v6.py ni simplebot/.
- Wallet HL3_* uniquement.
- Dry-run par défaut.

Quand Phase 1 est verte (pytest + optimize-once avec ≥1 actif), passe Phase 2 :
- HMM marché (BTC K=4) + HMM par symbole actif (K=3) dans hmm.py
- Double gate orchestrateur (marché autorise sleeves, symbole autorise entrées)
- Entraînement HMM symbole dans optimizer.py étape 8
- Tests test_hmm_symbol_* et test_orchestrator_double_gate_*
```

---

*Spec rédigée le 11/07/2026 — basée sur les backtests walk-forward, le paper
momentum, l'optimiseur SimpleBot du jour (XPL +12.6% valid, SOL +4.1% valid),
et les constats MinuteLab sur les frais taker.*
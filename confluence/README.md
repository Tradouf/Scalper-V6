# ConfluenceAgent — confluence multi-timeframe déterministe

Implémentation de la SPEC V8. Quatre horizons, chacun **filtre à veto** : 1d
donne le biais, 1h le régime, 15m le timing, 1m l'exécution seule. Toute couche
non alignée ⇒ pas de trade. Le défaut est l'inaction.

> # ⛔ VERDICT : REJETÉ (2026-08-12)
>
> Le §9 a rendu son verdict sur DEUX fenêtres (2023-2026 et 2020-2023) : le gate
> placebo échoue aux deux, **p = 0,42** et **p = 0,61** contre α = 0,05. La
> rentabilité apparente n'est pas distinguable du bruit.
>
> **Détail : [VERDICT.md](VERDICT.md)** · Registre : `hypotheses/REGISTRY.md`
> (entrée n°1) · Blocage exécutable : `DEPLOY_BLOCKED`
>
> Le **code reste** : le dispositif anti-frais, le moteur de backtest, le
> protocole §9 et l'APM sont validés et destinés à être réutilisés. C'est
> l'hypothèse de SIGNAL qui est rejetée, pas la machinerie.
>
> `ConfluenceAgent(..., live=True)` lève `DeploymentBlocked`. Le backtest et
> l'étude restent autorisés.

---

## Pourquoi ce module existe

Le diagnostic de départ n'est pas « la stratégie perd » mais « les frais
représentent 64 % des pertes nettes sur 2 156 trades ». Un filtre d'entrée plus
fin n'y répond qu'à moitié. Ce qui y répond vraiment est dans `risk.py` : un
plafond dur de 3 trades/jour, des cooldowns qui survivent au restart, un seuil
d'edge minimal exprimé en multiples de frais (5×), et un kill-switch qui coupe
si les frais dépassent 25 % du PnL brut sur 30 jours.

Une confluence H1/M15/M1 existait déjà en production V7 (`agents/multi_tf.py`,
`strate_gate()`, appelée depuis `main_v6.py`). Elle interroge un LLM à chaque
strate : ni reproductible, ni backtestable, donc invalidable par aucun
protocole. Ce module en est la version déterministe — c'est ce qui rend le §9
exécutable.

## Carte du code

| Fichier | Rôle | §  |
|---|---|---|
| `types.py` | Contrat de signal, verdicts figés | §5 |
| `config.py` | Chargement + validation de `config/confluence.yaml` | §7 |
| `indicators.py` | EMA/SMA/ADX/ATR/BBW/VWAP/z-score/ADF — purs, causaux | §3 |
| `layers/bias.py` | Biais 1d, hystérésis 2 clôtures, veto macro | §4.1 |
| `layers/regime.py` | Régime 1h, zone morte ADX, percentile ATR, funding | §4.2 |
| `layers/timing.py` | Timing 15m, pullback/trigger/invalidation | §4.3 |
| `layers/execution.py` | Exécution 1m, post-only, timeout/requotes/abandon | §4.4 |
| `risk.py` | Sizing, stop, garde-fous, filtre d'edge, kill-switch | §6 |
| `state.py` | Persistance atomique des garde-fous | §8 |
| `macro.py` | MacroRegimeAgent (droit de veto seul) | §2 |
| `trailing.py` | TrailingStopAgent (ATR adaptatif) | §2, §6.3 |
| `meanrev.py` | MeanReversionAgent (z-score, ADF, demi-vie) | §2, §4.3 |
| `agent.py` | Orchestration descendante, idempotence, log JSON | §8 |
| `data.py` / `sources.py` | Chargement d'historique, intégrité, proxy profond | §3, §9.2 |
| `adaptive/registry.py` | ParameterSets versionnés immuables, journal append-only | §12.2 |
| `adaptive/conditioner.py` | Interpolation pure par percentile de volatilité | §12.3 |
| `adaptive/optimizer.py` | Ré-optimisation mensuelle, promotion sous garde-fous | §12.4 |
| `adaptive/posture.py` | PostureSelector LLM borné, ratchet, shadow mode | §12.5-12.6 |
| `adaptive/manager.py` | APM — rend toujours une config valide | §12 |
| `backtest.py` | Moteur event-driven, frais/funding/slippage réels | §9.1 |
| `walkforward.py` | Walk-forward, acceptation, sensibilité, placebo | §9.2–9.6 |

## Commandes

```bash
python -m confluence.run fetch              # charge et contrôle l'historique
python -m confluence.run backtest           # un backtest sur toute la période
python -m confluence.run explain            # POURQUOI le bot ne trade pas
python -m confluence.run collect            # archive les bougies HL natives
python -m confluence.run overlap            # fidélité du proxy vs Hyperliquid
python -m confluence.run --source binance --days 1100 --jobs 8 validate

python -m pytest tests/test_confluence.py -v
```

Les options globales (`--days`, `--source`, `--equity`…) se placent **avant**
la sous-commande.

`explain` est la commande à réflexe quand le module semble inerte : elle donne
la distribution des motifs de veto, seule façon de distinguer un filtre qui
travaille d'un module cassé.

## Ce qui bloque le §9

Le §9.2 exige 3 ans de données à une cadence de décision de 15 minutes.
`candleSnapshot` d'Hyperliquid ne rend que ses **5000 dernières** bougies par
intervalle et ignore un `endTime` antérieur — paginer en arrière ne remonte pas
le temps. Relevé sur BTC le 2026-08-10 :

| TF | bougies | couverture |
|---|---|---|
| 1d | 2001 | 2000 j ✔ |
| 4h | 5000 | 833 j ✔ |
| 1h | 5000 | 208 j ✘ |
| 15m | 5000 | **52 j** ✘ |
| 1m | 5000 | 3 j ✘ |

Deux réponses, dans `sources.py` :

* **`--source binance`** — BTCUSDT perp USD-M, historique profond. C'est un
  **proxy** : même sous-jacent, autre carnet. Il permet d'estimer si le SIGNAL
  a un edge ; il ne dit rien de l'exécution réelle sur Hyperliquid. Frais,
  funding et modèle de fill restent ceux d'Hyperliquid dans le backtest.
  Fidélité mesurée sur 40 jours communs : corrélation des rendements 0,9974 en
  15m et 0,9992 en 1h, base médiane −1,3 bps. `run overlap` la remesure.
* **`run collect`** — accumule les bougies natives dans une archive qui
  grossit. Ne rattrape pas le passé, mais construit à partir d'aujourd'hui la
  série qui permettra de rejouer le §9 sans proxy.

**Conséquence à assumer** : un verdict §9 obtenu sur le proxy est une
présomption d'edge sur le signal, pas une validation du système sur
Hyperliquid. C'est une atténuation, pas une équivalence.

### Le funding est NON VALIDÉ, quelle que soit sa source

`fundingHistory` d'Hyperliquid, lui, honore `startTime` : le funding **natif**
remonte jusqu'au lancement de la plateforme (2023-05-12, mesuré). Un backtest
récent mêle donc prix proxy et funding natif — strictement meilleur, à
condition de le dire.

Cela ne le rend pas validé pour autant. `FundingProvenance.validated` vaut
`False` en permanence, et le restera **jusqu'à ce que le paper trading ait
mesuré le portage réel sur le compte**, position par position. Deux raisons
distinctes :

* source `binance` (obligatoire avant 2023, Hyperliquid n'existant pas) —
  autre lieu, règlement toutes les 8 h contre 1 h, autre déséquilibre
  long/short. Ce n'est pas le taux qu'on paiera ;
* source `hyperliquid` — le bon lieu et la bonne cadence, mais un taux
  HISTORIQUE appliqué à des positions SIMULÉES. Il ne dit rien du taux que le
  bot rencontrera, ni du moment où il se trouvera du côté qui paie.

Sur une position tenue plusieurs jours, ce poste dépasse les frais de
transaction. C'est le coût le moins vérifiable du §9, d'où son marquage dans
`fetch`, `backtest`, le bandeau de `validate` et le rapport JSON.

Le moteur facture chaque **règlement traversé**, pas un pas de temps fixe :
appliquer une boucle horaire à des taux 8 h multiplierait le poste par huit.

### `validate` refuse de conclure sur des données incomplètes

Un contrôle préalable bloque le verdict si le funding est absent, s'il commence
après le début de la fenêtre de décision, s'il manque des barres, ou si la
couverture est trop courte. Motif : lors du premier run réel, un HTTP 429 a
laissé le funding vide, la couche 1h a veté 100 % des évaluations, et le
protocole a produit 90 backtests à zéro trade avec l'aplomb d'un vrai résultat.
Un échec par trou de données est indiscernable d'un échec de stratégie — sauf
si l'outil refuse de le rendre. `--force` passe outre, en le disant.

## §12 — AdaptiveParameterManager

**Principe cardinal : aucun LLM ne produit jamais de valeur numérique.** Les
nombres viennent de l'optimisation statistique (étage 1) ; le LLM (étage 2) ne
fait que choisir entre trois jeux déjà validés selon le §9, et ce choix passe
encore par un schéma strict, un seuil de confiance, un ratchet asymétrique
(défensif immédiat, agressif après 3 avis consécutifs) et un shadow mode de
45 jours.

Le conditionnement (§12.3) ne touche que des paramètres de la section `risk`,
lus **en aval** du percentile ATR qui le pilote — d'où l'absence de boucle de
rétroaction. `RegimeConditioner.assert_no_feedback()` le vérifie et refusera le
jour où l'on voudra conditionner un seuil d'ADX.

Le backtest tourne **avec le conditionner actif**, comme l'impose le §12.3.
L'écart mesuré sur données synthétiques (8 trades/PF 14,3 en figé contre
9 trades/PF 7,6 en conditionné) montre qu'il ne s'agit pas de la même
stratégie : valider l'une pour exécuter l'autre serait un trompe-l'œil.

**Ordre d'amorçage.** Le registre démarre vide et l'APM retombe alors sur un
set de repli embarqué, jamais validé (signalé par `degraded`). La séquence
correcte est donc : valider d'abord la stratégie à config figée, puis créer les
trois sets par l'optimiseur, puis re-valider avec le conditionner actif. Le
§12.3 s'applique à la validation d'un `ParameterSet`, ce qui suppose qu'il en
existe un.

## Écarts assumés par rapport à la spec

1. **`LayerVerdict.data`** — la spec impose `passed`/`reason`/`computed_at`
   (tous présents) ; un champ `data` a été ajouté, sans quoi l'orchestrateur
   devrait recalculer les indicateurs et la pureté des couches ne servirait à
   rien.
2. **Veto macro inactif par défaut** — `MacroRegimeAgent` n'a pas de
   fournisseur on-chain dans ce repo. `provider: none` rend `UNKNOWN`, qui ne
   veto ni n'autorise ; seul `EXTREME` force FLAT. `UNKNOWN` n'est pas
   `NORMAL` : une donnée absente ne se lit jamais comme une confirmation. Le
   fait est journalisé à chaque évaluation.
3. **Funding absent ⇒ veto** — un filtre qu'on ne peut pas évaluer n'est pas
   un filtre passé (§1).
4. **Fenêtre glissante fixe** — chaque couche reçoit exactement
   `warmup_bars + WINDOW_SLACK` bougies, la même longueur en live et en
   backtest. Les EMA sont donc amorcées sur fenêtre glissante : déterministe,
   et surtout identique des deux côtés. Changer `WINDOW_SLACK` entre un
   backtest validé et le live ferait diverger les deux.
5. **Modèle de fill au 15m** — l'ordre maker ne vit qu'une bougie 15m, ce qui
   correspond aux 90 s de timeout et 3 re-cotations du §4.4, et exige une
   traversée stricte de la limite. Volontairement pessimiste.
6. **Clés `[hors-spec]` du YAML** — paramètres exigés par le texte de la spec
   mais absents de son exemple (`k_edge` du §6.5, frais servant au filtre
   d'edge, `tick_size`…). Toutes marquées comme telles dans le fichier.

## Invariants tenus par le code

* **Anti-repaint** — `indicators.closed()` est le seul point de passage ;
  `now_ms` est toujours injecté, jamais lu par une couche.
  `test_anti_repaint_le_futur_ne_change_aucune_decision` donne à l'agent
  l'historique complet, futur inclus, et vérifie que les décisions sont
  identiques à celles prises sur le passé seul.
* **Idempotence** — rejouer une bougie 15m ne produit pas de second signal ;
  l'hystérésis du biais a sa propre clé (`last_bar_ts`).
* **Jamais de taker à l'entrée** — le prix limite est borné du bon côté du
  carnet par construction ; en fin de re-cotations, `ExecutionPlan` ABANDONNE.
* **Garde-fous persistants** — écriture atomique ; un état illisible est
  archivé et signalé, jamais écrasé en silence.
* **Backtest == live** — le moteur appelle `ConfluenceAgent.decide()`, pas une
  réimplémentation.

## Avant d'envisager le mainnet

Dans cet ordre, sans en sauter :

1. `run validate` conforme aux cinq critères du §9.4 ;
2. aucun paramètre fragile au §9.5 (±20 %) ;
3. gate placebo passé (`placebo_gate.py`) — **figer le pipeline avant le
   tirage**, re-tester après ajustement est du multiple-testing ;
4. paper testnet 2 semaines minimum (§9.6) ;
5. mainnet avec `risk_pct` divisé par 2 le premier mois.

Aucune combinaison d'indicateurs ne garantit des gains. Les critères du §9.4
sont la seule base de décision.

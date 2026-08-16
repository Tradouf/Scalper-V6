# SPEC — MomentumAgent : momentum cross-sectionnel sur panier de perps

**Projet** : SalleDesMarches (Hyperliquid, architecture multi-agents, Python async)
**Hypothèse n°3 au registre** — α = 0,0167 (Bonferroni, n = 3), 60 tirages minimum.
**Dépendances** : moteur de backtest 1m/OHLC existant, comptabilité §7 (GridAgent, réutilisée), registre `hypotheses/REGISTRY.md`, gates (placebo, config gelée + hash, garde anti-retouche).
**Hors dépendances** : RegimeLayer, ConfluenceAgent, GridAgent — ce module n'utilise **aucune** détection de régime. C'est délibéré : les deux rejets précédents incriminent cet étage ; l'hypothèse n°3 n'en dépend pas.
**Objectif de ce document** : spécification exécutable destinée à Claude Code.

> ## Amendements du 2026-08-14 (avant gel, avant tout tirage)
>
> Ce document a été amendé pour décrire **exactement ce qui tournera**, et non ce
> qui avait été envisagé. Trois sections changent, chacune parce qu'une
> vérification sur les données a contredit l'hypothèse de rédaction :
>
> 1. **§9.1 — source de données** : perps USD-M partout (univers, prix, funding),
>    et fenêtre bear décalée à **2021-01 → 2023-12**. Motif : au 2020-01 il
>    n'existait que **3 perps** sur Binance (BTC, ETH, BCH) ; 58 au 2021-01. Un
>    panier de 10 était impossible, et « les 10 plus liquides » aurait signifié
>    « tous ceux qui existent » — plus aucune sélection cross-sectionnelle.
> 2. **§9.2 — construction du placebo** : permutation **persistante** des séries
>    de scores, au lieu d'une permutation par date. Motif : permuter à chaque
>    date détruit la persistance du classement, donc l'hystérésis du §4 ne retient
>    plus rien, le placebo churne et paie bien plus de frais. On aurait comparé
>    « stratégie calme contre stratégie qui churne », pas « signal contre hasard »
>    — un biais en faveur du réel, sur le critère principal.
> 3. **Questions ouvertes** : les quatre sont tranchées et actées en fin de
>    document. La section « à trancher » disparaît.
>
> Le §11 gagne les paramètres de source de données que ces décisions rendent
> nécessaires. Rien d'autre n'est modifié.

---

## 0. Cadrage honnête (à lire avant d'implémenter)

Le momentum cross-sectionnel — acheter les actifs qui ont le mieux performé relativement aux autres, vendre les pires — est l'anomalie la mieux documentée de la littérature financière : des décennies de données, des dizaines de marchés, y compris crypto. La contrepartie économique présumée est la **sous-réaction** : les porteurs ajustent leurs positions plus lentement que l'information n'arrive, et celui qui suit le mouvement relatif encaisse la traînée de cet ajustement.

Ce que cette stratégie **ne promet pas** :

1. **Elle perd violemment par moments.** Drawdowns historiques de 30 à 50 % sur les implémentations crypto, et des *momentum crashes* documentés : quand le marché se retourne brutalement après une longue baisse, les perdants (shortés) rebondissent plus fort que les gagnants (longés). Ce risque est **accepté, pas mitigé** — toute tentative de le filtrer intelligemment est une hypothèse supplémentaire qui n'est pas celle-ci.
2. **Elle est lente et ennuyeuse.** Des jours sans rebalancement, des semaines à contre-sens. Ce n'est pas un bot qui « travaille » — c'est un pari statistique tenu avec discipline.
3. **L'espérance historique (15-40 % annualisés bruts selon les études crypto) n'est pas une prévision.** L'anomalie s'est affaiblie sur certains marchés à mesure qu'elle était arbitrée. C'est précisément ce que le tirage doit établir : existe-t-elle encore, ici, nette des frais réels, mieux qu'un placebo ?

**Hypothèse testable** (recopiée telle quelle au registre) : « Sur un panier de perps liquides, le classement des rendements passés prédit-il les rendements relatifs futurs, suffisamment pour que la stratégie long-fort/short-faible batte, nette de tous les coûts, un placebo où ce lien est détruit ? » Pas : « le momentum gagne ».

## 1. Univers et panier — le piège n°1 est ici

Le **biais du survivant** est le mécanisme classique par lequel un backtest cross-sectionnel ment : construire le panier avec les coins liquides *d'aujourd'hui*, c'est sélectionner rétroactivement les gagnants de la période testée.

- **Univers à la date t** : les `basket_size` (défaut 10) perps les plus liquides **à la date t**, mesurés par volume médian sur les `liquidity_lookback_d` (défaut 30) jours précédant t, recalculé à chaque rebalancement. Un coin qui n'existait pas ou n'était pas liquide en 2021 n'entre pas dans le panier de 2021.
- Source de la mesure de liquidité : les données historiques de volume de la source de données du §9.1 — jamais un classement actuel.
- **Exclusions figées** : stablecoins et assimilés, tokens à mécanique de supply exotique (rebasing), et tout perp dont les données présentent des trous > `max_gap_bars` sur la fenêtre de calcul du signal (exclusion mécanique, loggée, pas discrétionnaire).
- BTC et ETH sont dans l'univers s'ils satisfont le critère (ils le satisferont) — le panier n'est pas « alts only ».

## 2. Signal

- **Score de momentum** de l'actif i à la date t : rendement cumulé sur `lookback_d` (défaut 21 jours), **en excluant les `skip_d` derniers jours** (défaut 2) — le skip évite de capturer le retournement court terme (mean-reversion à quelques jours), effet documenté et opposé.
- Classement cross-sectionnel des scores. C'est le **rang** qui décide, pas la valeur absolue : la stratégie est relative par construction.
- Aucun indicateur additionnel : pas de volume, pas de filtre de régime, pas de confirmation. Un seul signal, un seul classement. Chaque ajout serait un degré de liberté de plus au registre.

## 3. Portefeuille

- **Long** les `n_legs` (défaut 3) mieux classés, **short** les `n_legs` moins bien classés, poids égaux par jambe.
- **Neutralité dollar** : exposition longue = exposition courte = `gross_exposure_frac / 2` de l'equity (défaut gross 100 % ⇒ 50 % long, 50 % short, levier net ~0). Le levier est un paramètre d'exploitation, pas de validation : le tirage se fait à gross 100 %.
- Plafond par actif : `max_weight_per_asset` (défaut 20 % du gross) — protège contre un panier temporairement étroit.
- Note économique en faveur du short (à vérifier dans les données, pas à présumer) : en régime de funding normal, la jambe short **reçoit** le funding payé par les longs à levier — le portage du short est structurellement moins coûteux en perp qu'en spot. La comptabilité §7 doit rendre ce flux visible.

## 4. Rebalancement

- Fréquence : toutes les `rebalance_d` (défaut 2 jours), à heure fixe UTC.
- **Bande de tolérance anti-churn** : un actif déjà en portefeuille n'est remplacé que s'il sort du top/bottom `n_legs + hysteresis_rank` (défaut +2) du classement. Sans hystérésis, un actif qui oscille entre les rangs 3 et 4 génère des allers-retours qui ne paient que l'exchange — c'est la leçon anti-frais du projet appliquée au cross-sectionnel.
- Exécution : **maker patient** (post-only, repricing par pas de `requote_s`), bascule market autorisée après `exec_timeout_min` (défaut 30 min) — un rebalancement bi-journalier tolère une exécution lente ; le coût taker résiduel est comptabilisé et surveillé par le critère de frais §9.
- Pas d'exécution hors fenêtre de rebalancement, à une exception près : §6.

## 5. Risque

- `max_drawdown_pct` (défaut 40 %, cohérent avec le §0) : franchi ⇒ flatten complet, arrêt de l'agent, intervention humaine requise pour redémarrer. C'est un disjoncteur de survie, pas un outil de pilotage.
- Plafond de levier dur `max_leverage` = 1,5× gross (marge de sécurité sur les mèches), vérifié à chaque rebalancement.
- Un actif dont le perp est déliste ou dont les données s'interrompent ⇒ jambe fermée au prochain rebalancement, remplacée par le suivant au classement.
- **Aucun stop par position.** Le risque d'une stratégie cross-sectionnelle se gère au niveau du portefeuille (le §0 l'assume) ; des stops par jambe transformeraient le système en autre chose que ce que l'hypothèse teste.

## 6. Arrêts d'urgence (les seuls ordres hors rebalancement)

- Disjoncteur §5 (drawdown).
- Veto exchange : suspension de trading, marge impossible à maintenir.
- C'est tout. Pas de sortie sur « conditions de marché » — le momentum crash est un risque accepté (§0), pas un événement à esquiver.

## 7. Comptabilité (réutilise le §7 du GridAgent)

Composantes séparées, `net_mtm_pnl` seule métrique de décision :
- `pnl_long_legs` / `pnl_short_legs` : contributions par jambe — c'est la décomposition qui dira si l'edge (s'il existe) vit côté long, côté short, ou dans le spread ;
- `funding_pnl` : signé, par jambe — pour vérifier la note du §3 sur le portage du short ;
- `fees` : maker et taker séparés ;
- `net_mtm_pnl = pnl_long + pnl_short + funding − fees`.

## 8. APM et postures

- Intégration minimale : la posture `defensive` peut réduire `gross_exposure_frac`, jamais modifier `lookback_d`, `skip_d`, `n_legs`, l'univers ou la fréquence — le **signal est hors de portée de tout conditionnement** (même principe que `assert_no_grid_feedback()` : un assert dédié le vérifie).

## 9. Backtest et validation

### 9.1 Données — le piège n°2  *(AMENDÉ 2026-08-14)*

**Source unique : les perps USD-M Binance** — univers, prix ET funding. C'est
l'instrument réellement tradable, et le funding vient nativement par actif
(`fapi/v1/fundingRate`), sans forfait ni proxy.

Motif de l'amendement, mesuré le 2026-08-14 sur `fapi/v1/exchangeInfo` :

| date | perps USD-M listés |
|---|---|
| 2020-01 | **3** (BTC, ETH, BCH) |
| 2021-01 | 58 |
| 2023-01 | 108 |

La rédaction initiale prévoyait des données **spot** et une fenêtre 2020-2023.
Deux problèmes, tous deux fatals à l'hypothèse testée :

* un panier de 10 perps est **impossible avant mars 2020**, et jusqu'à mi-2020
  « les 10 plus liquides » aurait désigné la totalité de l'univers — la
  sélection cross-sectionnelle, qui EST la stratégie, aurait disparu ;
* un univers construit sur les volumes **spot** aurait inclus des actifs
  **sans perp à la date t**. La jambe short aurait été inexécutable
  historiquement, et le backtest optimiste sur la moitié du portefeuille —
  une variante exacte du biais que le §1 cherche à tuer.

**Fenêtres retenues** :

| fenêtre | période | ce qu'elle mesure |
|---|---|---|
| récente | maximum disponible | le régime actuel |
| bear | **2021-01 → 2023-12** | sommet 2021, effondrement 2022, et le **momentum crash** de la reprise — ce que le §0 annonce |

La fenêtre bear perd l'année 2020 par rapport à la rédaction initiale. Elle
conserve l'essentiel : c'est en 2022 et au rebond de 2023 que le §0 se vérifie.

**Granularité** : **1 j pour le signal** (un rendement 21 jours n'a que faire du
bruit horaire) et **1 h pour l'exécution** (un rebalancement maker étalé sur
30 minutes s'y modélise raisonnablement). Volume : ~15 actifs × 6 ans × 1 h
≈ 790 k bougies. Le moteur 1 m existe et reste disponible si la simulation
d'exécution devait être affinée, mais 47 M de bougies pour une stratégie qui
rebalance tous les deux jours serait un coût sans contrepartie.

**Coûts** : frais aux conditions Hyperliquid vérifiées sur le compte réel le
2026-08-14 (maker 1,5 bps, taker 4,5 bps), funding historique Binance par actif.
Ce dernier reste une **hypothèse documentée** : c'est le bon instrument et la
bonne cadence, mais pas le bon lieu. Comme pour les candidats n°1 et n°2, le
funding est **NON VALIDÉ** jusqu'à mesure en paper trading.

### 9.2 Placebo — adapté au cross-sectionnel  *(AMENDÉ 2026-08-14)*

Le placebo doit détruire **le lien entre classement passé et rendement futur**
en préservant tout le reste.

**Méthode retenue : permutation PERSISTANTE des séries de scores.** À chaque
tirage, une permutation σ est tirée **une seule fois** et réaffecte la série de
scores complète de l'actif i à l'actif σ(i), pour toute la période. Le
portefeuille placebo a le même univers, la même structure, les mêmes coûts et
une **persistance de classement comparable** — seul le lien entre le passé d'un
actif et son propre futur est détruit.

Motif de l'amendement : la rédaction initiale prévoyait une permutation **par
date**. Elle détruit la persistance du classement, donc l'hystérésis du §4 ne
retient plus aucune position, le portefeuille placebo tourne à chaque
rebalancement et paie un multiple des frais du réel. On aurait comparé une
stratégie calme à une stratégie qui churne, et un « succès » du réel n'aurait
pu être distingué de sa moindre rotation. Sur le critère principal, ce biais
était disqualifiant.

≥ **60 tirages**, verdict à **α = 0,0167**. Si le vrai classement ne bat pas des
classements permutés, il n'y a pas de momentum — quelle que soit la couleur du
PnL absolu.

### 9.3 Protocole
- Config gelée + hash **avant tout tirage** (fichier propre `momentum.yaml` — aucune dépendance à `confluence.yaml` ni `grid.yaml`, cette hypothèse n'en lit rien), garde anti-retouche actif, `config_untouched` au rapport.
- Fenêtres : récente (max disponible) **et** 2021-2023 (cf. §9.1 amendé).
- Sensibilité ±20 % sur `lookback_d`, `skip_d`, `n_legs`, `rebalance_d` — la stratégie doit dégrader **progressivement**, pas s'effondrer sur un paramètre (un pic isolé = overfitting).
- **Compteurs d'activation par branche** avec alerte si une branche testée reste à zéro sur tout le run — leçon de l'A/B fantôme du GridAgent : un chemin de code jamais emprunté doit crier, pas se taire.

### 9.4 Critères d'acceptation (net_mtm_pnl, out-of-sample, les deux fenêtres)
- Placebo battu à α = 0,0167 — **critère principal**
- Profit factor net > 1,2
- Drawdown max ≤ 45 % (cohérence avec le §0 : on vérifie que le réalisé ne dépasse pas ce que le cadrage annonce)
- `fees / gross_pnl_abs` < 20 %
- ≥ 100 rebalancements cumulés sur les deux fenêtres
- Décomposition long/short/funding rapportée (diagnostic, pas critère)

### 9.5 Déploiement (si et seulement si verdict positif)
Backtest validé → paper/testnet ≥ 4 semaines (deux fois plus long que la grille : la stratégie est lente, il faut assez de rebalancements réels pour comparer fills et modèle) → mainnet sur le **wallet neuf** à gross réduit de moitié le premier mois. Le levier > 1× est une décision séparée, postérieure, jamais incluse dans ce verdict.

## 10. Hors périmètre (explicitement)

- Aucun filtre de régime, aucun timing d'entrée, aucune confirmation technique.
- Pas de pondération par score, par volatilité ou par « conviction » : poids égaux (chaque raffinement est une hypothèse future, pas un réglage).
- Pas de stops par position, pas de take-profit.
- Pas de gestion « intelligente » du momentum crash.
- Pas d'optimisation du lookback : la sensibilité §9.3 le sonde, elle ne le choisit pas.
- Le levier d'exploitation (post-validation) n'appartient pas à cette spec.

## 11. Configuration (YAML)  *(AMENDÉ : bloc `data` ajouté)*

```yaml
momentum:
  universe:
    basket_size: 10
    liquidity_lookback_d: 30
    max_gap_bars: 12          # bougies 1h sur la fenêtre de signal
    exclusions: [stables, rebasing]
  signal:
    lookback_d: 21
    skip_d: 2
  portfolio:
    n_legs: 3
    gross_exposure_frac: 1.0
    max_weight_per_asset: 0.20
    hysteresis_rank: 2
  rebalance:
    every_d: 2
    hour_utc: 8
    exec_timeout_min: 30
  risk:
    max_drawdown_pct: 0.40
    max_leverage: 1.5
  fees:
    maker_bps: 1.5      # vérifié compte réel 2026-08-14
    taker_bps: 4.5
  data:                       # [amendement §9.1]
    market: binance_perp_usdm
    signal_timeframe: 1d
    exec_timeframe: 1h
    funding_source: binance_perp
  backtest:
    placebo:
      n_draws: 60             # ≥ 1/α avec α = 0,0167
      alpha: 0.0167
      method: persistent_score_permutation   # [amendement §9.2]
    sensitivity:
      params: [signal.lookback_d, signal.skip_d, portfolio.n_legs, rebalance.every_d]
      deltas: [-0.2, 0.2]
```

## 12. Tests unitaires attendus

- Univers : le panier à la date t n'utilise aucune donnée postérieure à t (test anti-lookahead, propriété centrale) ; un coin sans historique suffisant à t est exclu ; l'univers 2021 ≠ l'univers 2026 sur données synthétiques.
- Signal : le skip exclut bien les derniers jours ; le classement est invariant par échelle des prix ; scores NaN ⇒ exclusion, jamais rang par défaut.
- Portefeuille : neutralité dollar au rebalancement ; plafond par actif respecté quand le panier rétrécit ; hystérésis — un actif au rang n_legs+1 reste, au rang n_legs+hysteresis+1 sort.
- Rebalancement : aucun ordre hors fenêtre sauf disjoncteurs §6 ; le churn avec hystérésis ≤ churn sans, sur séries synthétiques oscillantes.
- Risque : drawdown > seuil ⇒ flatten + arrêt + redémarrage refusé sans intervention ; délistage ⇒ remplacement propre.
- Comptabilité : somme exacte des composantes ; funding correctement signé par jambe.
- Placebo : la permutation préserve les distributions marginales (test statistique), destruit le lien passé/futur, et produit un **turnover comparable** au réel (contrôle de l'amendement §9.2).
- Anti-conditionnement : toute config qui conditionne le signal est rejetée au chargement.

---

## Décisions actées (2026-08-14, avant gel)

Les quatre questions ouvertes de la rédaction initiale sont tranchées.

| # | Question | Décision | Motif |
|---|---|---|---|
| 1 | Long-short vs long-only | **Long-short neutre dollar** | Seule structure où le placebo teste le momentum PUR. En long-only, le PnL est dominé par le beta crypto et un placebo aléatoire gagnerait aussi en marché haussier — le gate perdrait son pouvoir discriminant. |
| 2 | Granularité | **1 j signal, 1 h exécution** | Un rendement 21 j n'a que faire du bruit horaire. 47 M de bougies 1 m pour une stratégie rebalançant tous les 2 jours serait un coût sans contrepartie. |
| 3 | **Proxy funding 2020-2023** | **RÉSOLUE — sans objet** | Le choix des perps (§9.1) rend le funding **natif par actif** via `fapi/v1/fundingRate`. Ni forfait ni proxy d'instrument. Il reste un proxy de LIEU (Binance, pas Hyperliquid) et donc NON VALIDÉ jusqu'au paper. |
| 4 | Ambiguïtés résiduelles | **Deux tranchées** | `max_gap_bars: 12` = 12 bougies 1 h sur la fenêtre de signal. L'heure de coupe du signal est la clôture UTC quotidienne. |

*Note : ce module ne « bat le marché » par aucune construction. Il teste une anomalie documentée sur des données qui ne l'ont peut-être plus. Les critères du §9.4 sont la seule base de décision.*

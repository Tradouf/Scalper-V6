# MomentumAgent — VERDICT : REJETÉ

**Date** : 2026-08-16
**Statut** : **REJETÉ**. Aucun déploiement, ni paper ni mainnet.
**Config gelée** : `sha256 = 5c582f614a0792fef8e4b12c4b6fdc182ed1b67900f50da7c9f24b6bb9b0c0e8`
(`momentum.yaml` seul, §9.3), **intouchée** — `config_untouched: true`.
**Rapport brut** : `momentum/state/reports/validate-momentum.json`
**Registre** : `hypotheses/REGISTRY.md`, entrée n°3.

---

## Résultats

Métrique de décision : **`net_mtm_pnl` uniquement** (§7). Equity de départ
10 000 $ par fenêtre.

| | récente | bear 2021-2023 |
|---|---|---|
| `net_mtm_pnl` | **−3 804 $** | **−3 173 $** |
| rebalancements | 141 | **10** |
| drawdown max | 40,5 % | 40,7 % |
| **arrêté par le disjoncteur §5** | **oui** | **oui** |
| PnL jambe LONGUE | −279 | +257 |
| PnL jambe COURTE | **−3 537** | **−3 406** |
| funding long / short | +141 / −233 | +201 / −187 |
| frais | 81 $ | 11 $ |

## Critères §9.4

| Critère | Valeur | Seuil | |
|---|---|---|---|
| **Gate placebo** (PRINCIPAL) | **p = 0,9836** | α = 0,0167 | ✗ |
| Profit factor | 0,0 | > 1,2 | ✗ |
| Drawdown max | 40,7 % | ≤ 45 % | ✓ |
| Frais / PnL brut | *non mesuré* | < 20 % | ✗ (voir défaut n°1) |
| Rebalancements | 151 | ≥ 100 | ✓ |

## Ce qui tranche : le signal est pire que le hasard

| | net_mtm_pnl cumulé |
|---|---|
| **Signal réel** | **−6 977 $** |
| Placebo, médiane | −1 244 $ |
| Placebo, maximum | +8 334 $ |

**p = 0,9836.** Sur 60 permutations persistantes, **59 font mieux que le vrai
classement**. Des classements tirés au hasard perdent 5,6 fois moins que le
signal de momentum.

Ce n'est pas une absence d'edge : c'est un **anti-signal**. Sur ces deux
fenêtres, trier les perps par rendement passé et parier sur la persistance du
classement a été systématiquement pénalisant.

**Et la jambe courte porte tout.** −3 537 et −3 406, contre −279 et +257 côté
long. Shorter les moins performants a coûté douze fois plus que longer les
meilleurs n'a rapporté. C'est exactement le *momentum crash* décrit au §0 —
« quand le marché se retourne, les perdants shortés rebondissent plus fort que
les gagnants longés » — sauf qu'ici il ne s'agit pas d'un épisode : c'est le
régime dominant des deux fenêtres.

**Les deux fenêtres ont déclenché le disjoncteur** à −40 %. Le §0 annonçait des
drawdowns de 30 à 50 % ; le réalisé est cohérent avec le cadrage, ce qui valide
le §0 et condamne la stratégie. La fenêtre bear a atteint le seuil après
**10 rebalancements seulement**, soit environ trois semaines.

**Note sur le funding** : la jambe courte a bien **encaissé** du funding
(−233 et −187, convention « positif = payé »). La note du §3 est donc vérifiée
dans les données — mais le portage favorable ne pèse rien face à 3 500 $ de
perte directionnelle.

## Trois défauts de ce run, à connaître

**1. Le ratio de frais n'a pas pu être évalué.** `gross_pnl_abs` est calculé par
la comptabilité mais absent de `MomentumPnL.as_dict()` : le critère lit 0 et
rend `None`, compté comme échec. C'est un défaut d'export, pas une propriété de
la stratégie. Les frais bruts sont dérisoires (81 $ et 11 $ pour ~7 000 $ de
perte) — ce critère n'aurait jamais été le contraignant. **Corrigé dans le code
pour les runs futurs ; non rejoué ici**, un second tirage sur les mêmes données
étant du multiple-testing.

**2. La sensibilité ne teste réellement que deux paramètres sur quatre.**
`skip_d` et `every_d` valent 2 : ±20 % donne 1,6 et 2,4, qui arrondissent tous
deux à 2. Les quatre variantes correspondantes sont donc identiques au nominal
(−6 977,4 à l'unité près, visible dans le rapport). Le §9.3 n'est satisfait que
pour `lookback_d` et `n_legs`.

Correctif pour un futur candidat : pour les petits paramètres entiers, faire
varier de ±1 cran plutôt que de ±20 %, ou déclarer explicitement le pas de
variation.

**3. Les fenêtres sont tronquées par le disjoncteur.** Les deux runs s'arrêtent
à −40 %, la fenêtre bear après 10 rebalancements. Les critères portent donc sur
« combien de temps la stratégie met à perdre 40 % », pas sur son comportement de
période complète. C'est le fonctionnement voulu du §5, et c'est une information
en soi — mais elle limite la portée des chiffres autres que le placebo.

Aucun de ces trois défauts ne change le verdict : **p = 0,9836 est indépendant
de tous les trois**, et un signal battu par 59 permutations sur 60 ne serait
sauvé ni par un ratio de frais, ni par deux paramètres de sensibilité, ni par
une fenêtre plus longue.

## Compteurs de branche (§9.3)

| branche | récente | bear | lecture |
|---|---|---|---|
| `rebalance_executed` | 141 | 10 | |
| `rebalance_skipped_nochange` | 162 | 15 | l'hystérésis évite plus de la moitié des tours |
| `hysteresis_saved` | **386** | **34** | la bande du §4 **travaille** — pas décorative |
| `leg_opened` / `leg_closed` | 191 / 185 | 18 / 12 | |
| `taker_fallback` | 6 | 6 | flatten du disjoncteur uniquement |
| `drawdown_tripped` | 1 | 1 | |
| `universe_too_narrow` | 0 | 0 | attendu : pool de 25 actifs |
| `delisted_replaced` | 0 | 0 | attendu : aucun délistage sur la période |
| `leverage_capped` | 0 | 0 | attendu : gross 1,0 < plafond 1,5 |

Les trois branches à zéro sont **attendues**, pas suspectes — le mécanisme du
§9.3 a fait son travail en les signalant, et la lecture confirme qu'elles ne
pouvaient pas être empruntées dans cette configuration. C'est précisément le
contraire de l'A/B fantôme du GridAgent, où un zéro cachait un câblage manquant.

## Ce qui est validé et réutilisable

* **Univers sans biais du survivant** — la propriété centrale du §1, testée par
  un piège explicite (actif à volume ×1000 listé après la date d'évaluation,
  invisible à cette date). À reprendre pour toute stratégie cross-sectionnelle.
* **Placebo à permutation persistante** — la construction du §9.2 amendé, qui
  préserve la persistance du classement et donc le turnover. C'est elle qui rend
  ce verdict interprétable.
* **Comptabilité par jambe** — sans la séparation long/short, on aurait conclu
  « le momentum perd » au lieu de « la jambe courte perd douze fois plus que la
  longue ne gagne ».
* **Disjoncteur à redémarrage humain** — a fonctionné deux fois sur deux.

## Interdiction de déploiement

`momentum/DEPLOY_BLOCKED` rend `MomentumAgent(..., live=True)` impossible :
`MomentumDeploymentBlocked` est levée. Le backtest et l'étude restent autorisés.

Lever ce blocage supposerait un nouveau verdict §9 favorable, donc une nouvelle
entrée au registre — le seuil serait alors α = 0,05/4 = 0,0125, soit 80 tirages
minimum.

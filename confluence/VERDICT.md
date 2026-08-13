# ConfluenceAgent — VERDICT : REJETÉ

**Date** : 2026-08-12
**Statut du module signal** : **REJETÉ**. Aucun déploiement, ni paper ni mainnet.
**Statut de l'infrastructure** : **conservée et réutilisable** (voir plus bas).
**Configuration gelée** : `sha256 = 22be63e3501975e3e40d6da4320ec483e7d0636b7470ecfc43811d2cd604c743`
**Rapports bruts** : `confluence/state/reports/validate-binance-1100j.json`,
`confluence/state/reports/validate-bear-2020-2023.json`

---

## Ce qui a été testé

L'hypothèse du §1 : qu'une convergence de quatre horizons (biais 1d, régime 1h,
timing 15m, exécution 1m), chacun opposant un veto, produise un edge
directionnel net de frais.

Deux fenêtres, choisies pour couvrir des régimes de marché opposés :

| | 2023-2026 (1100 j) | Bear 2020-2023 (1460 j) |
|---|---|---|
| Prix | proxy Binance BTCUSDT perp | proxy Binance BTCUSDT perp |
| Funding | **natif Hyperliquid** (horaire) | Binance (8 h — HL n'existait pas) |
| Fenêtres walk-forward | 8 | 11 |
| Jours OOS | 730 | 1005 |

## Critères d'acceptation §9.4

| Critère | 2023-2026 | Bear 2020-2023 |
|---|---|---|
| PF net OOS > 1,3 | **1,035** ✗ | 1,75 ✓ |
| frais / PnL brut < 15 % | 6,5 % ✓ | 3,9 % ✓ |
| ≥ 100 trades | 101 ✓ | **65** ✗ |
| ≤ 3 trades/jour en moyenne | 0,138 ✓ | 0,065 ✓ |
| DD OOS ≤ 2× DD in-sample | 1,485 ✓ | **2,126** ✗ |
| Sensibilité ±20 % (§9.5) | non fragile ✓ | non fragile ✓ |
| **Gate placebo** | **p = 0,42** ✗ | **p = 0,61** ✗ |
| **VERDICT** | **REJETÉ** | **REJETÉ** |

## Ce qui tranche : le placebo, sur les deux fenêtres

Des séries dont l'autocorrélation a été détruite — rendements et formes de
bougies préservés, ordre permuté — produisent **autant** de fenêtres OOS
rentables que les vraies données. p = 0,42 et p = 0,61, contre α = 0,05.

La rentabilité apparente n'est pas distinguable du bruit. C'est le même test qui
avait condamné l'optimiseur EMA-cross de SimpleBot (p = 0,90 ; cf.
`simplebot/VERDICT_2026-08-07.md`), et le protocole permanent adopté depuis.

**Le PF de 1,75 du bear-market est le piège à comprendre.** Il passe le critère
de rentabilité, et il est trompeur : sur 1337 $ de PnL OOS cumulé, **1214 $
proviennent de deux fenêtres sur onze** (janvier–juin 2022, l'effondrement
LUNA/FTX). Neuf fenêtres sur onze ne produisent presque rien. Un edge se
manifeste régulièrement ; une chance de régime se concentre. Le placebo dit
laquelle des deux on observe.

La sensibilité ±20 % ne montre aucune fragilité — mais c'est ici sans valeur
consolante : un résultat *plat* et *nul* reste nul. Il n'y a rien à récupérer
par réglage, ce qui est justement ce que la platitude démontre.

## Lacune connue de la validation

**`adx_trend` −20 % n'a pas pu être testé.** 25 × 0,8 = 20, ce qui égale
`adx_range` : la validation de configuration refuse la combinaison, car une zone
morte nulle n'est plus une zone morte. Le §9.5 n'est donc couvert qu'à moitié
sur ce paramètre — la borne haute (+20 % → 30,0) a bien été testée, la borne
basse non.

Cela ne change pas le verdict : le placebo est indépendant de la sensibilité et
échoue franchement sur les deux fenêtres. Mais la couverture du §9.5 est
incomplète et doit être lue comme telle.

**Correctif pour un futur candidat** : faire varier `adx_trend` et `adx_range`
solidairement, ou tester la zone morte par sa largeur plutôt que par ses bornes
absolues.

## Ce qui est validé et sera réutilisé

Le rejet porte sur **l'hypothèse de signal**, pas sur la machinerie :

* **Dispositif anti-frais (§6.5)** — le diagnostic de départ (« les frais
  représentent 64 % des pertes nettes sur 2 156 trades ») est **résolu par
  construction** : 3,9 à 6,5 % du PnL brut contre un seuil de 15 %, et 0,07 à
  0,14 trade/jour contre un plafond de 3. Filtre d'edge minimal, cooldowns
  persistants, kill-switch frais : à reprendre tels quels.
* **Moteur de backtest** — event-driven, ordonnancement causal, frais
  maker/taker réels, funding facturé par règlement traversé, fill maker exigeant
  une traversée stricte.
* **Protocole §9** — walk-forward, critères, sensibilité, gate placebo, et le
  contrôle préalable qui **refuse de conclure sur des données incomplètes**.
* **AdaptiveParameterManager (§12)** — registre immuable, conditionnement pur,
  bornes du LLM. Aucun LLM n'y produit de valeur numérique.
* **Couches** — `RegimeLayer`, `BiasLayer`, `TrailingStopAgent`,
  `MeanReversionAgent` : indicateurs purs, causaux, testés anti-repaint.

Le protocole a fait exactement son travail : **tuer le candidat en quelques
heures plutôt qu'en deux ans de paper trading.** Au rythme observé
(0,136 trade/jour), atteindre les 100 trades du critère de significativité en
forward aurait demandé ~735 jours.

## Interdiction de déploiement

Aucun chemin de code ne mène au mainnet :

* `confluence/run.py` ne passe aucun ordre — il rend un code de sortie ;
* le module n'expose ni client d'exchange ni clé privée ;
* `confluence/DEPLOY_BLOCKED` marque le refus, et `agent.py` refuse de
  s'instancier en mode live tant que ce fichier existe.

Lever ce blocage supposerait un **nouveau** verdict §9 favorable, donc une
nouvelle entrée au registre des hypothèses (`hypotheses/REGISTRY.md`) avec le
seuil placebo corrigé du nombre de candidats testés à ce jour.

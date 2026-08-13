# GridAgent — VERDICT : REJETÉ

**Date** : 2026-08-13
**Statut** : **REJETÉ**. Aucun déploiement, ni paper ni mainnet.
**Config gelée** : `sha256 = 6286ed279db61c89e9506f81aae53100f1670d628e5bb0118b5463bef577d676`
(hash combiné `grid.yaml` + `confluence.yaml`), **intouchée** — `config_untouched: true`.
**Rapport brut** : `grid/state/reports/validate-grid.json`
**Registre** : `hypotheses/REGISTRY.md`, entrée n°2.

---

## Résultats

Métrique de décision : **`net_mtm_pnl` uniquement** (§7).

| | récente (1129 j) | bear 2020-2023 |
|---|---|---|
| `net_mtm_pnl` | **−9 475 $** | **−4 088 $** |
| sessions | 358 | 87 |
| profit factor | 0,053 | 0,087 |
| frais / PnL brut | 6,4 % | 5,6 % |
| pire perte de session | 3,11 % | 3,59 % |
| réalisé de grille | +1 725 | +1 039 |
| PnL d'inventaire | **−10 411** | **−4 782** |
| cycles | 2 394 | 685 |

Sur une equity de départ de 10 000 $, la fenêtre récente perd **95 % du compte**.

## Critères §9.4

| Critère | Valeur | Seuil | |
|---|---|---|---|
| Profit factor OOS | **0,0** | > 1,2 | ✗ |
| Pire perte de session | **3,59 %** | ≤ 1,65 % | ✗ |
| Frais / PnL brut | 6,2 % | < 20 % | ✓ |
| Sessions | 445 | ≥ 30 | ✓ |
| **Gate placebo** | **p = 0,634** | α = 0,025 | ✗ |

Sensibilité ±20 % : non fragile — mais sans valeur consolante, un résultat plat
et négatif restant négatif. Aucune variante ne remonte au-dessus de zéro.

## Ce qui a tranché

**1. La comptabilité §7 dit exactement ce qu'elle devait dire.** Le réalisé de
grille est positif sur les deux fenêtres (+1 725 et +1 039) : les cycles
gagnent, comme ils le font toujours par construction. Et le compte perd
14 000 $, parce que l'inventaire perd 15 000 $. C'est le cas d'auto-illusion du
§7, observé en grandeur nature : un tableau de bord affichant le PnL réalisé
aurait montré une stratégie gagnante pendant que le capital disparaissait.

**2. Le flatten ne contient pas les pertes.** Le critère « pire perte de session
≤ 1,1 × `max_grid_loss_pct` » existe précisément pour vérifier que le §6.1
fonctionne. Il échoue à **2,2×** le plafond (3,59 % contre 1,65 %). La cause est
celle signalée avant le run : le dimensionnement du §3.3 suppose un flatten AU
seuil de cassure, alors que le prix **gappe** régulièrement au-delà. Entre la
clôture 15 m qui déclenche et le prix auquel on sort, il y a un écart que la
contrainte de perte traversante ne modélise pas.

**3. Le placebo est pire que l'échec ordinaire.** Réel : 76 sessions rentables.
Placebo : **médiane 81, maximum 98**. Des séries dont l'autocorrélation a été
détruite font *mieux* que les vraies données. La grille ne se contente pas de ne
pas avoir d'edge — elle est activement pénalisée par la structure réelle du
marché, c'est-à-dire par les tendances que le filtre de régime du §2 ne
parvient pas à exclure.

C'est cohérent avec le §0 : « le filtre de régime EST la stratégie ». Le
verdict porte donc moins sur la grille que sur l'insuffisance d'un filtre
ADX < 20 + percentile ATR ∈ [15, 60] pour identifier un range qui va tenir.

## Limite de ce run : l'A/B du §9.5 est VIDE

**Le handoff ne s'est jamais déclenché : `handoffs = 0` sur les deux fenêtres.**
A et B rendent donc des résultats identiques au centime près (Δ = 0,00), et le
rapport JSON conclut « B ≥ A » — ce qui est vrai arithmétiquement et **dénué de
sens** méthodologiquement.

Cause : le backtest n'alimente pas `bias_by_day`. `_bias_at()` rend donc
toujours `None`, `_breakout_aligned_with_bias()` rend `False`, et la branche
« étape 2 » du §6.1 est inatteignable. Le biais 1d n'a jamais été câblé dans le
moteur de grille.

**Conséquence à assumer** : l'exigence du §9.5 n'est PAS satisfaite. Le
breakout handoff n'a pas été évalué, ni adopté, ni écarté — il n'a pas été
testé. Le champ `adopt_handoff: true` du rapport doit être lu comme un artefact,
pas comme une décision.

Cela ne change pas le verdict : une stratégie qui perd 95 % du compte avec un
profit factor de 0,053 et un placebo à p = 0,63 n'aurait pas été sauvée par un
raffinement de sa sortie de cassure. Mais si quelqu'un reprend cette hypothèse
un jour, il devra **d'abord** câbler le biais 1d, puis enregistrer une nouvelle
entrée au registre — le seuil placebo sera alors α = 0,05/3.

## Distribution des motifs d'arrêt (§9.4)

| motif | récente | bear | total |
|---|---|---|---|
| `regime_shift` | 131 | 39 | 170 |
| `breakout` | 104 | 35 | 139 |
| `drawdown` | 123 | 13 | 136 |
| `vol_spike` | 0 | 0 | 0 |

136 sessions sur 445 se terminent par un **drawdown**, c'est-à-dire par le
garde-fou du §6.3 et non par une sortie propre. Une grille sur trois est
stoppée en perte avant même que le range ne casse.

## Ce qui reste utilisable

* **Comptabilité §7** — la séparation réalisé / inventaire a fonctionné
  exactement comme prévu, et c'est elle qui rend ce verdict lisible. À reprendre
  telle quelle pour toute stratégie à inventaire.
* **Moteur de backtest 1 m** — 3,7 M de bougies, fills sur traversée stricte,
  ordonnancement intrabar par distance à l'ouverture.
* **Plancher de frais §3.2** — le ratio de frais reste à 6 %, très en dessous du
  seuil de 20 %. Les frais ne sont jamais devenus le problème.
* **`StrategyRouter`**, garde-fous, interdictions structurelles (anti-martingale,
  anti trailing grid, taker circonscrit) : tous tenus, tous testés.

## Interdiction de déploiement

`grid/DEPLOY_BLOCKED` rend `GridAgent(..., live=True)` impossible :
`GridDeploymentBlocked` est levée. Le backtest et l'étude restent autorisés.

Lever ce blocage supposerait un nouveau verdict §9 favorable, donc une nouvelle
entrée au registre avec le seuil Bonferroni du moment.

# Registre des hypothèses — APPEND-ONLY

Ce fichier existe pour contrer le **multiple testing au niveau stratégie**.

Le gate placebo protège chaque candidat pris isolément. Il ne protège de rien
si l'on essaie vingt candidats jusqu'à ce que l'un passe : à α = 0,05, un
candidat sur vingt passe **par construction**, même si aucun n'a d'edge. Le
protocole §9 appliqué vingt fois sans correction est une machine à fabriquer des
faux positifs, et elle est d'autant plus dangereuse qu'elle produit à chaque
fois un rapport chiffré, sensible et convaincant.

**Règle** : toute entrée est écrite **AVANT** le premier backtest du candidat.
Une entrée écrite après coup ne vaut rien — c'est précisément l'ordre qui
constitue la preuve qu'on n'a pas choisi l'hypothèse en regardant le résultat.

**Append-only**, comme le journal du `ParamRegistry` : on ajoute, on ne réécrit
jamais. Un candidat rejeté reste au registre pour toujours, parce que c'est lui
qui durcit le seuil du suivant.

## Seuil placebo corrigé (Bonferroni)

    α_corrigé = 0,05 / n

où `n` = nombre total de candidats enregistrés à ce jour, **celui-ci compris**.

| n | α corrigé | tirages placebo minimum |
|---|---|---|
| 1 | 0,050 | 20 |
| 2 | 0,025 | 40 |
| 3 | 0,0167 | 60 |
| 4 | 0,0125 | 80 |
| 5 | 0,010 | 100 |

Le nombre minimum de tirages vient d'une contrainte dure de `placebo_gate.py` :
la p-value la plus petite atteignable est `1/(n_placebo + 1)`. En dessous de
`1/α` tirages, le gate **ne peut pas** passer, quelle que soit la qualité de la
stratégie. Enregistrer un candidat sans augmenter les tirages en conséquence,
c'est le condamner d'avance.

Bonferroni est conservateur — c'est délibéré. Le coût d'un faux positif ici se
compte en argent réel et en mois de paper trading ; celui d'un faux négatif, en
une hypothèse abandonnée qu'on peut toujours reprendre plus tard.

**Justification économique obligatoire.** Chaque entrée doit répondre à : *qui
paie de l'autre côté, et pourquoi continuerait-il ?* Une stratégie sans réponse
à cette question n'est pas une hypothèse, c'est une corrélation trouvée dans
des données. C'est le filtre le moins coûteux du registre, et celui qui élimine
le plus de candidats.

---

## Entrée n°1 — ConfluenceAgent multi-timeframe

| | |
|---|---|
| **Enregistré le** | 2026-08-12 (rétroactif — voir note d'honnêteté) |
| **Hypothèse** | Edge directionnel par confluence d'indicateurs sur 4 horizons (1d biais, 1h régime, 15m timing, 1m exécution), chaque couche opposant un veto |
| **Justification économique** | *Faible.* L'hypothèse suppose qu'un alignement d'indicateurs techniques standards (EMA, ADX, ATR, Bollinger) capture une persistance de tendance que d'autres participants ne captureraient pas. Aucun mécanisme identifié n'explique **qui** paierait cet edge ni pourquoi il persisterait : ces indicateurs sont dans tous les terminaux depuis quarante ans. |
| **Config gelée** | `sha256 = 22be63e3501975e3e40d6da4320ec483e7d0636b7470ecfc43811d2cd604c743` — `confluence/state/frozen/confluence-2026-08-11-22be63e35019.yaml` |
| **Candidats à ce jour** | n = 1 |
| **Seuil placebo appliqué** | α = 0,05 (30 tirages) |
| **Fenêtres testées** | 2023-2026 (1100 j, 8 fenêtres OOS) et 2020-2023 (1460 j, 11 fenêtres OOS) |
| **Résultat placebo** | **p = 0,42** et **p = 0,61** |
| **VERDICT** | ❌ **REJETÉ** |
| **Détail** | `confluence/VERDICT.md` |

**Ce qui a tranché** : le placebo, sur les deux fenêtres. Des séries à
autocorrélation détruite produisent autant de fenêtres OOS rentables que les
vraies données. Le PF de 1,75 obtenu sur 2020-2023 passait pourtant le critère
de rentabilité — mais 1214 $ de son PnL de 1337 $ venaient de deux fenêtres sur
onze (l'effondrement de 2022). Concentration typique d'une chance de régime.

**Note d'honnêteté** : cette entrée est **rétroactive**. Le registre n'existait
pas quand le candidat a été testé. Elle est enregistrée telle quelle plutôt que
antidatée, et elle compte dans `n` — le prochain candidat en héritera d'un seuil
durci. C'est la seule entrée qui pourra jamais être rétroactive ; le principe
même du registre est que l'écriture précède le test.

**Réutilisable malgré le rejet** : dispositif anti-frais (§6.5), moteur de
backtest, protocole §9 complet, AdaptiveParameterManager (§12), couches
d'indicateurs purs. Le rejet porte sur l'hypothèse de signal, pas sur la
machinerie.

---

## Entrée n°2 — GridAgent (grille Long/Short maker sur BTC-PERP)

| | |
|---|---|
| **Enregistré le** | 2026-08-12, **avant tout tirage** |
| **Hypothèse** | Encaissement du bruit de range par une grille neutre maker. Mean reversion **locale**, pas de prédiction directionnelle : la grille ne parie pas sur le sens, elle vend la volatilité tant que le range tient. |
| **Justification économique** | *Plausible, et identifiable.* Une grille maker est un **fournisseur de liquidité** : elle est payée par ceux qui exigent l'immédiateté. Qui paie de l'autre côté — les preneurs de liquidité qui traversent le spread, et les liquidations forcées qui doivent s'exécuter maintenant quel que soit le prix. Pourquoi ils continueraient — l'impatience et la contrainte de marge ne disparaissent pas ; c'est une rémunération de service, pas une anomalie qui s'arbitre. **La contrepartie est explicite** : c'est une position short-volatilité, dont la perte maximale survient exactement quand le range casse. L'hypothèse testable n'est donc pas « la grille gagne » mais « le filtre de régime (§2) et la sortie à la cassure (§6.1) suffisent à ce que la prime encaissée dépasse le coût des cassures ». |
| **Config gelée** | `sha256 = 6286ed279db61c89e9506f81aae53100f1670d628e5bb0118b5463bef577d676` — hash **combiné** de `config/grid.yaml` et `config/confluence.yaml` (l'activation §2 lit les seuils de régime du ConfluenceAgent, ils font partie de l'hypothèse). Copies : `grid/state/frozen/`. |
| **Candidats à ce jour** | n = 2 |
| **Seuil placebo appliqué** | **α = 0,025** (Bonferroni 0,05/2), **40 tirages minimum** |
| **Fenêtres prévues** | 1100 j récents **ET** 2020-2023 (bear + covid) — obligatoires au §9.3 : une grille validée uniquement en marché calme est invalide par définition |
| **Métrique de décision** | `net_mtm_pnl` **uniquement** (§7). Le PnL réalisé d'une grille est toujours positif par construction ; le mettre en avant est un instrument d'auto-illusion. |
| **Granularité de simulation** | bougies **1 m** (Binance), fill maker sur **traversée stricte** du niveau — pas de touch, pas de modélisation de file d'attente favorable |
| **Protocole particulier** | A/B `flatten intégral` vs `breakout handoff` (§9.5) : B n'est adoptée que si elle est ≥ A sur `net_mtm_pnl` OOS sur **chacune** des deux fenêtres |
| **Runs effectués** | 1 protocole complet le 2026-08-13 : 4 backtests A/B + 12 backtests de sensibilité + 40 tirages placebo = **56 runs**, tous sur la config gelée `6286ed279db6…` (`config_untouched: true`) |
| **Résultat** | PF **0,053** (récente) et **0,087** (bear) · pire perte de session **3,59 %** contre plafond 1,65 % · **placebo p = 0,634** (réel 76 sessions rentables, placebo médiane 81, max 98) |
| **VERDICT** | ❌ **REJETÉ** (2026-08-13) |
| **Détail** | `grid/VERDICT.md` · blocage exécutable `grid/DEPLOY_BLOCKED` |

**Ce qui distingue ce candidat du n°1** : le ConfluenceAgent postulait un edge
directionnel tiré d'indicateurs présents dans tous les terminaux depuis quarante
ans, sans mécanisme identifié pour expliquer qui le paierait. Ici la contrepartie
est nommée et le service rendu est réel. Cela ne garantit rien — la question
ouverte est de savoir si la prime de liquidité couvre le coût des cassures de
range — mais l'hypothèse est au moins d'une nature qui peut être vraie.

**Engagement de méthode tenu** : le §9 a rejeté, aucun paramètre n'a été
retouché, aucun second tirage n'a été effectué. La configuration est restée
intouchée du gel au verdict (`config_untouched: true`).

**Ce que le rejet apprend.** Le §7 a fonctionné exactement comme prévu : le
réalisé de grille est POSITIF sur les deux fenêtres (+1 725 et +1 039) pendant
que le compte perd 14 000 $, l'inventaire ayant coûté 15 000 $. C'est
l'auto-illusion du §7 observée en grandeur nature. Et le placebo va plus loin
qu'un simple échec : des séries à autocorrélation détruite produisent PLUS de
sessions rentables que le réel — la grille est activement pénalisée par les
tendances que le filtre ADX < 20 + percentile ATR ∈ [15, 60] ne parvient pas à
exclure. Conformément au §0, le verdict porte donc sur le FILTRE DE RÉGIME, qui
« est la stratégie », plus que sur la grille elle-même.

**⚠ Limite du run — l'A/B du §9.5 est VIDE.** `handoffs = 0` sur les deux
fenêtres : le moteur n'alimente pas `bias_by_day`, donc la branche « étape 2 »
du §6.1 est inatteignable. A et B rendent des résultats identiques (Δ = 0,00) et
le rapport conclut « B ≥ A », ce qui est un artefact et non une décision. Le
breakout handoff n'a été ni adopté ni écarté : **il n'a pas été testé.** Toute
reprise de cette hypothèse devra d'abord câbler le biais 1d, puis enregistrer
une nouvelle entrée — le seuil sera alors α = 0,05/3 ≈ 0,0167, soit 60 tirages
minimum.

**Réutilisable malgré le rejet** : comptabilité §7 (séparation réalisé /
inventaire), moteur de backtest 1 m sur traversée stricte, plancher de frais
§3.2 (ratio de frais resté à 6 %, jamais le problème), `StrategyRouter`, et
toutes les interdictions structurelles — anti-martingale, anti trailing grid,
taker circonscrit au flatten.

---

## Entrée n°3 — MomentumAgent (momentum cross-sectionnel sur panier de perps)

| | |
|---|---|
| **Enregistré le** | 2026-08-14, **avant tout tirage** |
| **Hypothèse** | *Recopiée telle quelle du §0* : « Sur un panier de perps liquides, le classement des rendements passés prédit-il les rendements relatifs futurs, suffisamment pour que la stratégie long-fort/short-faible batte, nette de tous les coûts, un placebo où ce lien est détruit ? » — et **non** « le momentum gagne ». |
| **Justification économique** | *Forte, et documentée hors de ce projet.* La contrepartie présumée est la **sous-réaction** : les porteurs ajustent leurs positions plus lentement que l'information n'arrive, et suivre le mouvement relatif encaisse la traînée de cet ajustement. Qui paie — ceux qui ajustent en retard, structurellement. Pourquoi ils continueraient — la lenteur d'ajustement tient à des contraintes (attention, mandats, liquidité) qui ne disparaissent pas. **Réserve explicite** : c'est l'anomalie la mieux documentée de la littérature, donc aussi la plus arbitrée ; elle s'est affaiblie sur plusieurs marchés. Le tirage doit établir si elle existe **encore, ici, nette des frais réels**. |
| **Ce que l'hypothèse ne promet pas (§0)** | Drawdowns de 30 à 50 % documentés ; *momentum crashes* lors des retournements post-bear, où les perdants shortés rebondissent plus fort que les gagnants longés. Ce risque est **accepté, pas mitigé** — le filtrer serait une autre hypothèse. |
| **Config gelée** | `sha256 = 5c582f614a0792fef8e4b12c4b6fdc182ed1b67900f50da7c9f24b6bb9b0c0e8` (`config/momentum.yaml` **seul** — §9.3 : aucune dépendance à `confluence.yaml` ni `grid.yaml`). Spec amendée hashée séparément : `6c9c87622f7b…`. Copies : `momentum/state/frozen/`. |
| **Candidats à ce jour** | n = 3 |
| **Seuil placebo appliqué** | **α = 0,0167** (Bonferroni 0,05/3), **60 tirages minimum** |
| **Fenêtres prévues** | récente (max disponible) **et** **2021-01 → 2023-12** |
| **Métrique de décision** | `net_mtm_pnl` uniquement, décomposé long / short / funding (§7) |
| **Runs effectués** | 1 protocole complet le 2026-08-16 : 2 backtests de référence + 8 backtests de sensibilité + 60 tirages placebo (× 2 fenêtres) = **130 runs**, sur la config gelée `5c582f614a07…` (`config_untouched: true`) |
| **Résultat** | net **−3 804 $** (récente) et **−3 173 $** (bear) · disjoncteur −40 % déclenché sur les DEUX fenêtres · **placebo p = 0,9836** (réel −6 977, médiane nulle −1 244, max nul +8 334) |
| **VERDICT** | ❌ **REJETÉ** (2026-08-16) |
| **Détail** | `momentum/VERDICT.md` · blocage exécutable `momentum/DEPLOY_BLOCKED` |

**Décisions actées avant gel** (les quatre questions ouvertes de la spec) :

1. **Long-short neutre dollar**, pas long-only — seule structure où le placebo
   teste le momentum PUR. En long-only le PnL est dominé par le beta crypto, et
   un placebo aléatoire gagnerait aussi en marché haussier : le gate perdrait
   son pouvoir discriminant.
2. **1 j pour le signal, 1 h pour l'exécution** — un rendement 21 jours n'a que
   faire du bruit horaire.
3. **Funding natif par actif** (`fapi/v1/fundingRate`) — la question du proxy
   devient sans objet dès lors qu'on prend les perps. Il reste un proxy de LIEU
   (Binance, pas Hyperliquid), donc **NON VALIDÉ** jusqu'au paper trading.
4. `max_gap_bars` = 12 bougies 1 h ; heure de coupe du signal = clôture UTC.

**Deux amendements à la spec, faits AVANT le gel** — pour que le document gelé
décrive exactement ce qui tournera :

* **§9.1** — perps USD-M partout, et fenêtre bear décalée de 2020 à **2021**.
  Mesuré le 2026-08-14 : il n'existait que **3 perps** au 2020-01 (BTC, ETH,
  BCH), 58 au 2021-01. Un panier de 10 était impossible, et « les 10 plus
  liquides » aurait désigné la totalité de l'univers — la sélection
  cross-sectionnelle, qui EST la stratégie, aurait disparu. Un univers bâti sur
  les volumes spot aurait par ailleurs inclus des actifs sans perp à la date t,
  rendant la jambe short inexécutable et le backtest optimiste sur la moitié du
  portefeuille.
* **§9.2** — placebo par **permutation persistante** des séries de scores (une σ
  tirée une fois, appliquée à toute la période), au lieu d'une permutation par
  date. Permuter à chaque date détruit la persistance du classement : l'hystérésis
  du §4 ne retient plus rien, le placebo tourne à chaque rebalancement et paie un
  multiple des frais du réel. On aurait comparé une stratégie calme à une
  stratégie qui churne — un biais en faveur du réel, **sur le critère principal**.

**Ce qui distingue ce candidat des deux précédents** : il ne dépend d'**aucune
détection de régime**. Les rejets n°1 et n°2 incriminent tous deux cet étage —
le n°2 explicitement (« le filtre de régime EST la stratégie », §0 GridAgent).
L'hypothèse n°3 est délibérément construite pour ne pas en hériter.

**Engagement de méthode tenu** : le §9 a rejeté, aucun paramètre n'a été
retouché, aucun second tirage n'a été effectué.

**Ce que le rejet apprend — le signal est pire que le hasard.** Sur 60
permutations persistantes, **59 font mieux que le vrai classement**. Des rangs
tirés au hasard perdent 5,6 fois moins que le momentum réel. Ce n'est donc pas
une absence d'edge mais un **anti-signal** : sur ces deux fenêtres, parier sur la
persistance du classement a été systématiquement pénalisant.

**La décomposition §7 dit où.** Jambe courte −3 537 et −3 406 ; jambe longue
−279 et +257. Shorter les moins performants a coûté douze fois plus que longer
les meilleurs n'a rapporté. C'est le *momentum crash* annoncé au §0 — mais pas
comme épisode : comme régime dominant des deux fenêtres. Sans la séparation par
jambe, on aurait conclu « le momentum perd » au lieu de savoir quelle moitié
perd, et pourquoi.

Le §0 est validé en même temps que la stratégie est rejetée : il annonçait 30 à
50 % de drawdown, le réalisé est 40,5 % et 40,7 %. Le cadrage était honnête ; le
pari ne paie pas.

**Trois défauts du run, sans effet sur le verdict** (détail dans
`momentum/VERDICT.md`) : le ratio de frais n'a pas pu être évalué
(`gross_pnl_abs` non exporté — corrigé depuis, non rejoué) ; la sensibilité ne
teste réellement que 2 paramètres sur 4, `skip_d` et `every_d` valant 2 et
±20 % arrondissant au même entier ; et les deux fenêtres sont tronquées par le
disjoncteur. Aucun des trois ne touche le placebo, qui est indépendant et sans
appel.

**Réutilisable malgré le rejet** : l'univers sans biais du survivant et son test
piège (actif à volume ×1000 listé après la date d'évaluation), le placebo à
permutation persistante, la comptabilité par jambe, et le disjoncteur à
redémarrage humain — qui a fonctionné deux fois sur deux.

---

<!-- Nouvelles entrées ci-dessous. NE RIEN RÉÉCRIRE AU-DESSUS. -->

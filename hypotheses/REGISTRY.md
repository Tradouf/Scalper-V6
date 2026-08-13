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

<!-- Nouvelles entrées ci-dessous. NE RIEN RÉÉCRIRE AU-DESSUS. -->

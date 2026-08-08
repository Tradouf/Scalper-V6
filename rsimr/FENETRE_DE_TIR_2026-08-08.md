# Fenêtre de tir — RSI-MR long (2026-08-08)

Question posée : *où et quand l'espérance de capter une partie du mouvement
est-elle la plus forte ?* Méthode : 4 familles de conditionnement déclarées
d'avance pour un motif **mécaniste** (pas un balayage), jugées deux fois —
fenêtre pleine 200 j **et** segment OOS pur (antérieur au 03-06). Une coupe
n'est retenue que si l'**ordre des tranches se reproduit en OOS**.
Script : `fenetre_de_tir.py` (scratchpad session 6273a80a).
Base : 45 alts, 4920 barres 1h, 3075 signaux (2135 en OOS pur).

Référence tous signaux alts : **+16.7 bps net, t=3.66** (OOS +10.3, t=2.37).

## Ce qui a été RÉFUTÉ (hypothèses mécanistes séduisantes, fausses)

| hypothèse | pleine fenêtre | OOS pur | verdict |
|---|---|---|---|
| **Cascade** (≥35 % de l'univers en RSI<35 : signature de liquidation forcée) | +22.5 bps | **−1.6** | réfutée — ne survit pas |
| **Profondeur de purge** (RSI min <20 avant le rebond) | +11.2 | +6.8 | non monotone, pas d'effet |
| cascade × purge profonde (conjonction « idéale ») | **−52.0** | −41.7 | franchement négative |
| heure UTC (contrôle négatif) | 06-12 h : −33.6 | −43.2 | pas neutre, mais t faible (~−1.7) |

Le raisonnement « plus la purge est violente et généralisée, plus le rebond
est fort » est **faux dans ces données**. Une cascade est un marché où le
vendeur forcé n'a pas fini de vendre.

## Ce qui SURVIT : le régime de volatilité à l'entrée

État filtré du HMM canonique (causal, aucune info future) au moment du signal :

| état à l'entrée | vol | brut | **net** | t_cl | n | OOS net | 1re/2e moitié |
|---|---|---|---|---|---|---|---|
| 0 — calme | 41 bps/barre | −0.5 | **−15.5** | −0.03 | 550 | −26.7 | −20.6 / +20.0 |
| 1 — normal | 82 bps/barre | +27.4 | **+12.4** | +2.98 | 1978 | +12.4 | +31.8 / +23.1 |
| 2 — tempête | 213 bps/barre | +43.1 | **+28.1** | +1.92 | 547 | +28.8 | +39.6 / +46.2 |

L'ordre est **monotone et reproduit à l'identique en OOS** ; l'état 1 donne
+12.4 bps des deux côtés. En régime calme l'edge **brut** est nul (−0.5 bps) :
ce n'est pas un artefact de frais, il n'y a rien à capter.

### FENÊTRE DE TIR retenue : états 1 + 2 (exclure le régime calme)

| segment | net (bps/trade) | t_cl | n |
|---|---|---|---|
| pleine fenêtre | **+15.7** | +3.53 | 2525 |
| OOS pur | +13.7 | +2.50 | 1731 |
| 1re moitié | +15.0 | +2.19 | 1314 |
| 2e moitié | +16.5 | +2.88 | 1211 |
| + exclusion 06-12 h UTC | +15.2 | +2.98 | 1970 |

Bootstrap par jour (2000 tirages) : IC 90 % = **[+2.4 ; +29.9] bps**,
P(net>0) = 0.975. Fréquence : ~82 % des signaux (~390/mois sur 45 alts).

L'exclusion horaire n'ajoute rien (+15.2 vs +15.7) → **non retenue**
(un degré de liberté pour zéro gain).

## Correction importante de l'étape 2

La dérivation Monte-Carlo du 07-08 supposait le drift **identique dans tous
les régimes** (hypothèse conservatrice déclarée) et concluait à un sizing
Kelly 1.00 / 0.32 / 0.09 — donc **taille maximale en régime calme**.
Les données réfutent l'hypothèse : le drift n'est pas uniforme, il est
**concentré dans la vol** (brut −0.5 / +27.4 / +43.1). Appliquer le sizing de
l'étape 2 aurait mis le plus gros capital exactement là où l'edge est nul.

Sizing révisé, dérivé de E et Var mesurés par régime (Kelly ∝ E/σ²) :

| état | E net | σ/trade approx | Kelly relatif |
|---|---|---|---|
| 0 calme | −15.5 | 107 | **0** (ne pas trader) |
| 1 normal | +12.4 | 185 | **1.00** |
| 2 tempête | +28.1 | 374 | **0.55** |

La tempête paie mieux par trade mais coûte 4× la variance : elle mérite une
taille plus petite que le régime normal, pas plus grande.

## Statut

- Le paper `rsimr/` en cours **n'est pas modifié** : il tire dans les trois
  régimes, à taille fixe. C'est le juge en aveugle, et sa règle est plus
  large que la fenêtre → au verdict, on pourra **annoter offline** chaque
  trade avec son régime d'entrée (recalculable depuis l'OHLCV) et vérifier
  que la hiérarchie calme < normal < tempête se reproduit **en forward**.
  C'est le vrai test de la fenêtre, et il ne coûte rien.
- Aucune application en live avant ce contrôle forward + un test
  confirmatoire figé sur la règle restreinte (multiple-testing : la fenêtre
  est le 5ᵉ conditionnement essayé, même si les 4 autres ont été réfutés).

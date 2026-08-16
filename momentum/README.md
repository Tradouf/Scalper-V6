# MomentumAgent — momentum cross-sectionnel sur panier de perps

Implémentation de `SPEC_MomentumAgent.md` (amendée avant gel). **Candidat n°3**
du registre des hypothèses, α = 0,0167 (Bonferroni, n = 3), 60 tirages minimum.

> **État : en cours de validation.** Aucun ordre n'est passé par ce module.

---

## L'hypothèse, telle qu'enregistrée

> « Sur un panier de perps liquides, le classement des rendements passés
> prédit-il les rendements relatifs futurs, suffisamment pour que la stratégie
> long-fort/short-faible batte, **nette de tous les coûts**, un placebo où ce
> lien est détruit ? »

Et non « le momentum gagne ». La formulation interrogative est celle du §0 ;
elle est recopiée telle quelle au registre parce que c'est elle que le §9 teste.

**Ce que le §0 annonce et qu'il faut accepter** : drawdowns de 30 à 50 %,
*momentum crashes* lors des retournements post-bear, et des semaines à
contre-sens. Ce risque est **accepté, pas mitigé** — le filtrer serait une autre
hypothèse.

## Ce qui distingue ce candidat des deux précédents

Il ne dépend d'**aucune détection de régime**. Les rejets n°1 et n°2 incriminent
tous deux cet étage — le n°2 explicitement : « le filtre de régime EST la
stratégie » (§0 GridAgent). L'hypothèse n°3 est construite pour ne pas en
hériter, et sa contrepartie économique est nommée : la **sous-réaction** des
porteurs qui ajustent plus lentement que l'information n'arrive.

## Carte du code

| Fichier | Rôle | §  |
|---|---|---|
| `config.py` | Config autonome + anti-conditionnement du signal | §11, §8 |
| `core.py` | Univers sans biais du survivant, signal, portefeuille | §1-4 |
| `accounting.py` | Décomposition long / short / funding, `net_mtm_pnl` | §7 |
| `agent.py` | Rebalancement, disjoncteur, compteurs de branche | §4-6, §9.3 |
| `adaptive.py` | Postures — réduction d'exposition seule | §8 |
| `data.py` | Chargement multi-actifs perps USD-M | §9.1 |
| `backtest.py` | Moteur, causalité stricte | §9 |
| `validate.py` | Placebo, sensibilité, acceptation | §9.2-9.4 |

```bash
python -m momentum.run fetch       # charge les deux fenêtres §9.3
python -m momentum.run backtest    # un backtest par fenêtre
python -m momentum.run validate    # protocole §9 complet (bloquant)
python -m pytest tests/test_momentum_agent.py -v
```

Les tests sont dans `test_momentum_agent.py` : `test_momentum.py` existait déjà
et couvre le `MomentumPaperTrader` de SimpleBot.

## Les deux pièges, et comment ils sont fermés

**Piège n°1 — le biais du survivant (§1).** Construire le panier avec les coins
liquides d'aujourd'hui, c'est sélectionner rétroactivement les gagnants. Les
alts liquides de 2026 ne sont pas un échantillon aléatoire de ceux qui
existaient en 2021.

Fermé par construction : `select_universe()` prend un `as_of_ms` explicite et
tronque elle-même les séries. Un test le vérifie avec un piège franc — un actif
au **volume mille fois supérieur** à tous les autres, mais listé au jour 90 :
à t = jour 60 il est invisible, au jour 119 il entre normalement.

La liquidité est mesurée en **médiane** et non en moyenne : un unique jour de
volume aberrant suffirait sinon à propulser un illiquide dans le panier.

**Piège n°2 — les données (§9.1, amendé).** La rédaction initiale prévoyait du
spot et une fenêtre 2020-2023. Mesuré le 2026-08-14 :

| date | perps USD-M listés |
|---|---|
| 2020-01 | **3** (BTC, ETH, BCH) |
| 2021-01 | 58 |
| 2023-01 | 108 |

Un panier de 10 était impossible avant mars 2020, et « les 10 plus liquides »
aurait désigné tout l'univers — la sélection cross-sectionnelle, qui *est* la
stratégie, aurait disparu. Un univers bâti sur les volumes spot aurait par
ailleurs inclus des actifs **sans perp à la date t**, rendant la jambe short
inexécutable et le backtest optimiste sur la moitié du portefeuille.

D'où : perps USD-M partout, fenêtre bear décalée à **2021-2023**.

## Le placebo — pourquoi il a été amendé (§9.2)

C'est le **critère principal**, et sa première rédaction l'aurait biaisé.

Permuter les scores **à chaque date** détruit la persistance du classement.
L'hystérésis du §4 ne retient alors plus aucune position, le portefeuille
placebo tourne à chaque rebalancement, et paie un multiple des frais du réel. On
aurait comparé une stratégie calme à une stratégie qui churne — pas un signal à
du hasard.

Méthode retenue : **permutation persistante**. Une σ tirée une fois par tirage
réaffecte la série de scores de l'actif i à l'actif σ(i), pour toute la période.
Univers, structure, coûts et persistance du classement sont préservés ; seul le
lien entre le passé d'un actif et son propre futur est rompu.

Les permutations sont des **dérangements** (sans point fixe) : un actif gardant
son propre score réinjecterait du vrai signal dans le tirage nul.

## Ce que la comptabilité §7 doit révéler

`net_mtm_pnl` est la seule métrique de décision. Les composantes répondent à une
question de fond : **où vit l'edge, s'il existe ?**

* `pnl_long` / `pnl_short` — une stratégie long-short dont tout le PnL vient du
  long n'est pas du momentum cross-sectionnel, c'est du **beta déguisé**. Le
  diagnostic `edge_location` le nomme explicitement au-delà de 70 %.
* `funding_long` / `funding_short` — le §3 avance que le short *reçoit* le
  funding en régime normal. C'est une hypothèse à vérifier dans les données,
  d'où la séparation par jambe.

## Garde-fous

* **Disjoncteur §5** : drawdown > 40 % ⇒ flatten, arrêt, et `restart()` **lève**
  sans `human_override=True`. Un disjoncteur qui se réarme seul n'est pas un
  disjoncteur — il transformerait une perte de 40 % en pause de trois secondes.
* **Anti-conditionnement §8** : le signal est gelé. Seul `gross_exposure_frac`
  est réductible, et aucune posture ne peut l'augmenter — le tirage §9 s'est
  fait à gross 100 %.
* **Compteurs de branche §9.3** : un chemin jamais emprunté produit une alerte.
  Leçon directe de l'A/B fantôme du GridAgent, où le handoff n'avait jamais été
  exécuté et où le rapport concluait « B ≥ A » au centime près.
* **Sensibilité §9.3** : vérifie une **dégradation progressive**, pas seulement
  l'absence d'effondrement. Un nominal qui dépasse ses deux voisins d'un facteur
  2 est signalé comme « pic isolé — signature de surapprentissage ».

## Avant tout mainnet

1. `run validate` conforme au §9.4, **placebo en tête** ;
2. paper/testnet ≥ 4 semaines — deux fois plus long que la grille, parce que la
   stratégie est lente et qu'il faut assez de rebalancements réels pour comparer
   fills et modèle ;
3. mainnet sur le **wallet neuf**, gross réduit de moitié le premier mois.

Le funding du backtest est un **proxy de lieu** (Binance, pas Hyperliquid) :
**NON VALIDÉ** jusqu'à mesure en paper. Le levier > 1× est une décision séparée
et postérieure, jamais incluse dans ce verdict.

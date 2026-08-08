# RSI-MR — test confirmatoire figé du 2026-08-07

## Contexte

Candidat issu du scan « rythme du marché » (session 07-08 après-midi) :
4 déclencheurs × 5 régimes × 3 horizons sur cache 15m 65 j — la mean-reversion
(RSI-MR, Stoch) positive presque partout, le momentum négatif partout.
Candidat retenu : **RSI(14), rachat de survente (croisement 30↑), LONG only,
H≈4 h**, confirmé sur 1h le même jour (+33 bps, deux moitiés positives).

Étapes convenues avant tout live : (a) étendre l'historique ~200 j 1h et
rejouer FIGÉ, (b) pooled + gate placebo p<0.05, (c) paper.

## Protocole (critères figés AVANT exécution — `confirm_200d.py`)

- 48 symboles (univers de la découverte, figé), 4920 barres 1h chacun (~205 j) ;
- règle figée : RSI(14) Wilder, long au croisement ≤30→>30, sortie au close
  4 barres plus tard, frais RT 15 bps, warmup 220 barres ;
- t clusterisé par jour calendaire (chevauchement + corrélation cross-symboles) ;
- **OOS pur = les ~115 jours ANTÉRIEURS au 2026-06-03** (début de la fenêtre
  de découverte : jamais vus pendant la sélection) ;
- placebo : 40 permutations de barres (`placebo_gate.shuffle_candles`,
  rendements et formes de barres conservés, autocorrélation détruite) ;
- succès ⇔ (OOS brut > 15 bps ET t_cl ≥ 2) ET placebo p < 0.05.

## Résultats

| segment | brut (bps/trade) | net | t_cl/jour | n | jours |
|---|---|---|---|---|---|
| fenêtre pleine ~200 j | +31.97 | +16.97 | **+3.73** | 3317 | 175 |
| **OOS pur (avant découverte)** | **+26.69** | **+11.69** | **+2.55** | 2345 | 115 |
| fenêtre découverte (~65 j) | +36.88 | +21.88 | +2.21 | 972 | 61 |
| 1re moitié | +29.82 | +14.82 | +2.22 | 1703 | 84 |
| 2e moitié | +33.79 | +18.79 | +3.12 | 1614 | 92 |
| majors BTC/ETH/SOL | +7.54 | −7.46 | +0.65 | 242 | 78 |
| alts | +31.68 | +16.68 | +3.66 | 3075 | 174 |

Placebo : t réel +3.73 > max des 40 tirages (+1.81), **p = 1/41 = 0.024**.
Largeur : 24/48 symboles nets > frais (≥10 signaux).

**Verdict : CANDIDAT CONFIRMÉ** (les deux critères figés passés).

## Réserves honnêtes

1. **Majors mortes sur 200 j** (net négatif) — le « majors +25 bps t=3.6 » de
   la découverte 65 j n'a pas tenu. L'edge est dans les alts. L'univers paper
   reste figé à 48 (re-sélectionner les alts maintenant = nouveau degré de
   liberté).
2. Largeur moyenne : la moitié des symboles porte tout.
3. Un backtest confirmé n'est pas un forward : momentum 4h avait un beau
   backtest et a fait −21 % en 19 j de paper. **Le paper est le juge.**

## Étape (c) — paper lancé le 2026-08-07 22:24

`rsimr/` (service systemd user `rsimr.service`, log `logs/rsimr.log`,
état `rsimr/state/`) — PAPER ONLY, amorçage sans replay historique.
Jugement ~mi-septembre 2026 (comme xsmom) : moyenne nette/trade > 0 et du
même ordre que le backtest (+17 bps net), sur ≥300 trades (~19 signaux/j
attendus). Aucun réglage en cours de test — si ça échoue, le candidat meurt.

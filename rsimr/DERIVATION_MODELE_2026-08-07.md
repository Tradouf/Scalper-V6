# Étape 2 — paramètres dérivés du modèle (2026-08-07)

Protocole « modèle d'abord » : direction = constante empirique confirmée OOS
(+26.7 bps / 4 barres, uniforme, identique par régime — choix
conservateur) ; amplitude = HMM canonique K=3 calibré sur 45 alts
(rendements 1h standardisés, 200 j). Moyennes d'état forcées à 0 (IC
direction ≈ 0 prouvé). Sélection sur 40000 trajectoires synthétiques par
régime — zéro multiple-testing sur données réelles.

## Modèle canonique

| état | vol (bps/barre 1h) | persistance (barres) | stationnaire |
|---|---|---|---|
| 0 | 41 | 10.6 | 0.30 |
| 1 | 82 | 10.5 | 0.56 |
| 2 | 213 | 4.9 | 0.14 |

σ 1h médian alts : 100 bps. Transition :
[[0.905 0.086 0.009]
 [0.05  0.904 0.045]
 [0.002 0.204 0.794]]

## Politiques (régime d'entrée 0, δ×1.0, bps/trade net de 15 bps)

| politique | E[net] | σ | durée (barres) |
|---|---|---|---|
| TEMPS - | +11.49 | 107 | 4.0 |
| TP∞ SL2σ | +10.08 | 103 | 3.8 |
| TP∞ SL3σ | +11.03 | 105 | 3.9 |
| TP2σ SL∞ | +8.68 | 94 | 3.6 |
| TP2σ SL2σ | +7.33 | 90 | 3.4 |
| TP3σ SL3σ | +9.90 | 98 | 3.8 |
| RÉGIME voljump | +11.25 | 102 | 4.0 |

## Conclusions dérivées du modèle

### Régime d'entrée 0 (vol 41 bps/barre)

- TEMPS (règle paper) : E = +11.49 bps, σ = 107 bps, Sharpe/trade 0.1078
- meilleure E[net] : ('TEMPS', '-') → +11.49 bps (durée 4.0 b)
- meilleure croissance (E²/Var) : ('RÉGIME', 'voljump') → E +11.25 bps, σ 102 bps
- sensibilité δ×0.5 (TEMPS) : E = -1.86 bps
- sensibilité δ×1.5 (TEMPS) : E = +24.84 bps

### Régime d'entrée 1 (vol 82 bps/barre)

- TEMPS (règle paper) : E = +11.01 bps, σ = 185 bps, Sharpe/trade 0.0596
- meilleure E[net] : ('TEMPS', '-') → +11.01 bps (durée 4.0 b)
- meilleure croissance (E²/Var) : ('RÉGIME', 'voljump') → E +10.34 bps, σ 172 bps
- sensibilité δ×0.5 (TEMPS) : E = -2.34 bps
- sensibilité δ×1.5 (TEMPS) : E = +24.36 bps

### Régime d'entrée 2 (vol 213 bps/barre)

- TEMPS (règle paper) : E = +12.39 bps, σ = 374 bps, Sharpe/trade 0.0331
- meilleure E[net] : ('TEMPS', '-') → +12.39 bps (durée 4.0 b)
- meilleure croissance (E²/Var) : ('TEMPS', '-') → E +12.39 bps, σ 374 bps
- sensibilité δ×0.5 (TEMPS) : E = -0.96 bps
- sensibilité δ×1.5 (TEMPS) : E = +25.74 bps

## Sizing par régime d'entrée (Kelly relatif)

- régime 0 (vol 41 bps/barre) : 1.00
- régime 1 (vol 82 bps/barre) : 0.32
- régime 2 (vol 213 bps/barre) : 0.09

## Statut

Paramètres ENREGISTRÉS, non appliqués : le paper rsimr en cours reste
inchangé (test en aveugle). Application éventuelle = variante v2 soumise à
UN test confirmatoire figé + gate placebo, ou annotation offline des trades
paper au verdict (le régime filtré se recalcule a posteriori depuis l'OHLCV,
aucune modification du service nécessaire).

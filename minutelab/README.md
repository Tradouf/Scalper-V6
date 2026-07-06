# MinuteLab — labo permanent de stratégies BTC 1 minute

Recherche en continu la meilleure combinaison d'indicateurs (RSI, Supertrend,
Stochastique, croisements de moyennes — 148 variantes de paramètres) sur les
**60 dernières minutes**, en exigeant que la stratégie soit gagnante **net de
frais** sur la fenêtre entière ET sur les **20 dernières minutes**. La
championne est appliquée en **paper trading** et réévaluée à intervalle
**adaptatif** (15 min par défaut, borné [5, 30] : se resserre quand le
champion déçoit ou qu'on est flat, se détend quand il gagne). La sortie de
position : le **gain croise sous sa moyenne mobile**, échantillonné toutes les
**5 secondes** (12 échantillons par défaut), avec stop dur −0,4 % et durée max
30 min en garde-fous. Si rien ne bat les frais → FLAT, par construction.

## Commandes

```bash
python -m minutelab.run --scan-once      # une sélection, classement affiché
python -m minutelab.run                  # boucle permanente (paper) — état dans minutelab/state/
python -m minutelab.walkforward --hours 72 --step 15   # test out-of-sample honnête
python -m pytest tests/test_minutelab.py -v
```

Tout est surchargeable par env `MINUTELAB_*` (voir `config.py`) : fenêtres,
frais, rythme, MA de sortie, symbole.

## Résultat de recherche (2026-07-06, walk-forward BTC 72 h)

| Rythme | PnL net OOS | Trades |
|---|---|---|
| 5 min | −1,78 % | 14 |
| 15 min | −0,63 % | 12 |
| 30 min | −0,76 % | 12 |
| 15 min, **coût zéro** | **+1,59 %** | 364 |

Conclusion : la sélection 60/20 min a un pouvoir prédictif brut réel mais
minuscule (~+0,004 %/trade), soit ~35× moins que le coût aller-retour taker +
slippage (0,15 %). **Non viable en l'état sur Hyperliquid en taker** — cohérent
avec le constat « petits gains souvent = −frais ». Pistes : exécution maker
(coût ÷10), ou porter la même méta-sélection sur des fenêtres plus longues où
le gain moyen par trade dépasse les frais.

**PAPER TRADING UNIQUEMENT** : aucun ordre réel n'est envoyé, aucun wallet requis.

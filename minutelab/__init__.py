"""
MinuteLab — laboratoire permanent de stratégies BTC à la minute.

Recherche en continu la meilleure combinaison d'indicateurs (RSI, Supertrend,
Stochastique, moyennes mobiles) sur les 60 dernières minutes, avec exigence
de gain sur les 20 dernières. La stratégie championne est appliquée en paper
trading et réévaluée à intervalle adaptatif (15 min par défaut). La sortie de
position se fait quand le gain croise sous sa moyenne mobile, échantillonné
toutes les 5 secondes.

PAPER TRADING UNIQUEMENT : aucun ordre réel n'est envoyé.
"""

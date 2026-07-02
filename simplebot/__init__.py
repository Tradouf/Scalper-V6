"""
SimpleBot — algo de trading simple et paramétrique, indépendant de la V6.

Trois briques :
- strategy.py   : stratégie EMA cross + filtre RSI, TP/SL en multiples d'ATR
- optimizer.py  : agent qui re-backteste périodiquement la grille de paramètres
                  et publie le meilleur set dans simplebot/state/best_params.json
- live_trader.py: exécution live sur Hyperliquid avec un SECOND wallet
                  (HL2_PRIVATE_KEY / HL2_ACCOUNT_ADDRESS), recharge à chaud
                  les paramètres publiés par l'optimiseur.

Lancement : python -m simplebot.run   (dry-run par défaut, SIMPLEBOT_DRY_RUN=0 pour le live)
"""

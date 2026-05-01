Plan au 26 mars.

On part bien sur une vraie **salle des marchés multi‑agents IA** focalisée sur Hyperliquid + Whale Alert, pas juste un client technique.

### Architecture cible (niveau 10 000 m)

On aura 3 couches principales :

1. **Couche Exchange**  
   - `HyperliquidClient` qui implémente `ExchangeClient` (ton `base.py`). [ppl-ai-file-upload.s3.amazonaws](https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/attachments/24165468/dba2820a-3609-4809-a20f-546c97850b8a/base.py)
   - Il fournit : `get_markets`, `get_orderbook`, `get_trades`, `get_positions`, `get_balances`, `place_order`, `cancel_order`. [ppl-ai-file-upload.s3.amazonaws](https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/attachments/24165468/dba2820a-3609-4809-a20f-546c97850b8a/base.py)

2. **Couche Agents IA** (dans un dossier `agents/`)  
   - `MarketScannerAgent`  
     - Parcourt les paires Hyperliquid, lit orderbooks, volumes, volatilité, funding, etc. [copypipe](https://copypipe.io/crypto/hyperliquid-api-trading-guide/)
     - Utilise aussi Whale‑Alert (REST ou WebSocket) pour détecter les gros transferts sur BTC, ETH, stablecoins. [developer.whale-alert](https://developer.whale-alert.io/documentation/)
     - Score chaque marché (intérêt / risque / liquidité) et publie une liste de “marchés chauds” pour les autres agents.  
   - `StrategyAgent` (plusieurs instances possibles)  
     - Chacune suit une stratégie (trend‑following, mean‑reversion, breakout, whale‑follow, etc.).  
     - Prend en entrée les signaux du scanner + données de marché, et renvoie des “intentions d’ordres” normalisées (des `OrderRequest`). [ppl-ai-file-upload.s3.amazonaws](https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/attachments/24165468/dba2820a-3609-4809-a20f-546c97850b8a/base.py)
   - `RiskManagerAgent`  
     - Applique les règles globales : taille max par trade, leverage max, perte journalière max, exposition par coin, etc. [3commas](https://3commas.io/blog/ai-trading-bot-risk-management-guide-2025)
     - Peut annuler ou réduire une intention d’ordre, ou couper toutes les positions si un seuil de drawdown est atteint.  

3. **Superviseur IA (`TradingSupervisor`)**  
   - Coordonne tout ça :  
     - interroge régulièrement le `MarketScannerAgent`,  
     - demande des décisions aux `StrategyAgent`s sur les marchés sélectionnés,  
     - fait valider par le `RiskManagerAgent`,  
     - envoie les ordres finaux à `HyperliquidClient`.  
   - Peut créer/arrêter dynamiquement des `StrategyAgent`s, ajuster leurs paramètres, loguer les décisions et les résultats.

### Ce que je peux te coder maintenant

Vu la taille du chantier, on va avancer **en modules** pour que tu puisses copier/coller proprement et tester au fur et à mesure.

Ordre efficace :

1. **Couche Exchange propre**  
   - `config_hyperliquid.py` (déjà donné).  
   - `exchanges/hyperliquid.py` qui respecte exactement `ExchangeClient` (en se basant sur ton `hyperliquid_client-1.py`, mais je nettoie pour coller aux dataclasses `OrderRequest`, `OrderResult`, `CancelResult`, `Position`, `Balance`). [ppl-ai-file-upload.s3.amazonaws](https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/attachments/24165468/983ba5f4-5228-46c2-ac87-1cfd04f2f725/hyperliquid_client-1.py)

2. **Couche Agents**  
   - `agents/market_scanner.py` :  
     - lit Hyperliquid (REST ou SDK) pour récupérer markets + volumes + spreads,  
     - consomme Whale‑Alert (REST périodique au début, WebSocket plus tard) pour enrichir les signaux. [developer.whale-alert](https://developer.whale-alert.io/documentation/)
   - `agents/strategy_trend.py` :  
     - une première stratégie simple (par ex. trend‑following sur 1h avec gestion de position).  
   - `agents/risk_manager.py` :  
     - applique des limites de taille / leverage / perte max par jour. [tv-hub](https://www.tv-hub.org/guide/crypto-trading-bots-beginners)

3. **Superviseur**  
   - `supervisor/trading_supervisor.py` :  
     - boucle principale H24 (ou quasi) qui orchestre scanner → stratégies → risk → exécution. [arxiv](https://arxiv.org/html/2510.08068v1)

Pour rester dans ta contrainte de copier/coller sans te noyer, je te propose qu’on commence par **finaliser 100 % proprement le `HyperliquidClient`** aligné sur `ExchangeClient`, basé sur le fichier Hydraqueen que tu as envoyé. [ppl-ai-file-upload.s3.amazonaws](https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/attachments/24165468/983ba5f4-5228-46c2-ac87-1cfd04f2f725/hyperliquid_client-1.py)
C’est le socle sur lequel les agents IA vont se brancher.

Dans le prochain message, je peux te donner :

- soit uniquement `exchanges/hyperliquid.py` corrigé et parfaitement compatible avec `base.py`,  
- soit ce fichier + un premier squelette `agents/market_scanner.py` qui montre comment brancher Hyperliquid + Whale‑Alert.

Tu préfères qu’on commence par **seulement** le client Hyperliquid propre, ou client + premier agent scanner dans le même temps ?

# Propositions de code V7 (déposées par l'audit Opus, revues par l'humain)

## 2026-06-02 21:00 — [WARNING] Submit sell échoue silencieusement → emergency exit non garantie
**Severity** : warning
**Files** : execution/engine.py:157-164
**Pattern** : 203 HyperliquidClientError sur la fenêtre 6h ; échantillon : `ExecutionEngine submit error LINK sell: HyperliquidClientError(` et `DOGE sell`, mêmes symboles que les 3 EMERGENCY EXIT (LINK, DOGE, BNB).
**Diagnostic** : Dans `submit()`, toute exception de `place_order()` est loggée puis `continue` — l'ordre est abandonné sans retry ni fallback. Quand l'ordre raté est une sortie (reduce_only / EMERGENCY EXIT), la position reste ouverte alors que le système la croit fermée. La corrélation symboles submit-error ↔ emergency-exit suggère que les emergency exits eux-mêmes échouent à soumettre (203 erreurs en 6h = ~34/h, bien au-delà des 3 emergency loggés → beaucoup de tentatives ratées). Le repr `%r` est tronqué dans l'extraction : le message HL complet n'est pas visible, ce qui empêche le diagnostic racine (rate-limit ? prix Alo non-marketable ? notional ?).
**Proposed fix** :
```python
# Before
            try:
                result = self._exchange.place_order(req)
            except Exception as e:
                logger.error("ExecutionEngine submit error %s %s: %r", order.asset, order.side, e)
                continue
# After
            try:
                result = self._exchange.place_order(req)
            except Exception as e:
                # Log complet (le repr tronqué masquait la cause racine HL).
                logger.error(
                    "ExecutionEngine submit error %s %s qty=%.6f ro=%s: %s",
                    order.asset, order.side, order.qty, order.reduce_only, e,
                )
                # Une sortie ratée laisse la position ouverte alors qu'on la croit
                # fermée : fallback market_close pour garantir le flat.
                if order.reduce_only:
                    try:
                        self._exchange.market_close(order.asset)
                        logger.warning("ExecutionEngine fallback market_close %s OK", order.asset)
                    except Exception as e2:
                        logger.error("ExecutionEngine fallback market_close %s FAILED: %s", order.asset, e2)
                continue
```
**Risk si non corrigé** : Les sorties de risque (EMERGENCY EXIT, reduce-only) peuvent échouer silencieusement et laisser des positions ouvertes au-delà des limites de risque, le système les comptant comme fermées. Logs tronqués = cause racine des 203 erreurs invisible.
**Status** : pending
---

## 2026-06-07 15:00 — [INFO] Spike EMERGENCY EXIT (15 en 6h, vs 0-3 historique)
**Severity** : info
**Files** : risk/emergency_exit.py (modifié non-commité au moment de l'audit)
**Pattern** : `EMERGENCY EXIT : 15` sur la fenêtre 6h (DOGE×5, SUI×3, BTC×2, AAVE×2, SOL×1, LINK×1, BNB×1), équity $693.83→$680.29 (-$13.54). Audits précédents : emergency=0 (06-06/06-07 09:00), 1 (06-01), 3 (06-02).
**Diagnostic** : Le nombre d'emergency exits passe de 0-3 à 15 (5×) sur la même config de régime (100% range), réparti sur 7 symboles dont BTC et AAVE qui sont en `manual_symbols`/positions manuelles francois — un emergency exit sur BTC manuel solderait potentiellement un swing manuel (cf. feedback no_touch_positions). risk/emergency_exit.py apparaît modifié non-commité au moment de l'audit : forte suspicion qu'un changement récent du seuil ou de la condition de déclenchement génère des sorties intempestives, qui réalisent des pertes (corrélation avec la baisse d'équity). Diagnostic racine impossible sans relecture du diff de emergency_exit.py + logs par symbole (hors périmètre paramètre).
**Proposed fix** : Revue humaine du diff non-commité de `risk/emergency_exit.py` : (1) vérifier que la condition de déclenchement n'a pas été assouplie ; (2) confirmer que `manual_symbols` (BTC, AAVE non listé mais position manuelle ?) sont exemptés du force-close emergency ; (3) ne pas toucher les caps de risque (garde-fou audit).
**Risk si non corrigé** : Emergency exits intempestifs réalisent des pertes (équity -2%/6h) et peuvent solder des positions manuelles (BTC) contre l'intention de l'opérateur.
**Status** : pending
---

## 2026-06-08 09:00 — [WARNING] NameError 'prob_range' non défini → activation grille avortée
**Severity** : warning
**Files** : main.py (bloc "Grid activation <SYM>", logger `v7.main`) — ligne à localiser (référence `prob_range`)
**Pattern** : 8 `NameError` sur la fenêtre, échantillon : `2026-06-08 06:04:25 [WARNING] v7.main — Grid activation SUI error: NameError("name 'prob_range' is not defined")` et idem DOGE. 8 activations grille / 0 désactivation sur la fenêtre.
**Diagnostic** : Le chemin d'activation de la grille référence une variable `prob_range` non définie dans la portée locale (renommage incomplet, ou variable issue du détecteur de régime non propagée jusqu'au bloc d'activation). L'exception est rattrapée en WARNING et l'activation est abandonnée pour le symbole concerné → la grille ne se pose pas sur SUI/DOGE quand le régime range l'autoriserait. Symptôme silencieux : pas d'erreur fatale, mais perte d'opportunité grille + comportement non déterministe (certains symboles activent, d'autres lèvent NameError). Hors périmètre paramètre = régression code. Je ne lis pas le code (workflow) : à l'humain de localiser l'occurrence `prob_range` dans le bloc d'activation et de la définir (probablement la proba de régime range exposée par le détecteur) ou de corriger le nom.
**Proposed fix** :
```python
# Before (schéma — à confirmer dans main.py)
#   ... bloc Grid activation <SYM> ...
#   if prob_range > seuil:          # NameError : prob_range non défini ici
#       activate_grid(symbol, ...)
# After
#   prob_range = regime_result.prob_range   # ou le nom réel exposé par le détecteur de régime
#   if prob_range > seuil:
#       activate_grid(symbol, ...)
```
**Risk si non corrigé** : Activation grille non déterministe (NameError silencieux par symbole) → la grille ne moissonne pas le range sur les symboles affectés (SUI, DOGE…), perte d'opportunité et asymétrie de couverture entre actifs ; risque de masquer d'autres régressions dans le même bloc.
**Status** : pending
---

## 2026-06-08 21:00 — [INFO] Spike ReadTimeout/ConnectionError sur HL adapter (≥50) → pas de retry/backoff sur refresh
**Severity** : info
**Files** : exchanges/hl_adapter.py (méthodes refresh allMids + fetch candles 1h ; logger `v7.hl_adapter`)
**Pattern** : 146 `ReadTimeoutError` + 51 `ConnectionError` + 157 `error` génériques sur la fenêtre 6h (vs 0-1 ReadTimeout historiquement). Échantillons : `HL candles SUI 1h error: ConnectionError(ReadTimeoutError("HTTP…` et `HL allMids refresh error: ReadTimeout(ReadTimeoutError("HTTP…`.
**Diagnostic** : Pic de timeouts réseau sur les appels HL de refresh (allMids et candles 1h). Les exceptions sont rattrapées en WARNING et l'appel est abandonné sans retry ni backoff → la donnée n'est pas rafraîchie pour ce cycle. Impact observé cette fenêtre BÉNIN (equity plate, grille saine, aucune pathologie szi0/frozen), donc épisode probablement infra/HL transitoire plutôt qu'une régression. MAIS sans retry/backoff borné, un refresh allMids raté = prix/mids potentiellement stale pour le pricing grille/MR et la détection de régime au cycle suivant ; un volume soutenu (~200 erreurs/6h) augmente la fraction de cycles servis sur donnée non rafraîchie. À surveiller : si le compte ≥50 persiste sur plusieurs audits, escalader en warning. Je ne lis pas le code (workflow).
**Proposed fix** :
```python
# Before (schéma — à confirmer dans hl_adapter.py)
#   try:
#       mids = self._info.all_mids()
#   except Exception as e:
#       logger.warning("HL allMids refresh error: %r", e)
#       return  # abandon : mids stale ce cycle
# After
#   for attempt in range(3):
#       try:
#           mids = self._info.all_mids()
#           break
#       except (ReadTimeout, ConnectionError) as e:
#           if attempt == 2:
#               logger.warning("HL allMids refresh failed after 3 tries: %r", e)
#               return
#           time.sleep(0.5 * (2 ** attempt))   # backoff borné 0.5/1.0s
```
**Risk si non corrigé** : En cas de dégradation HL prolongée, mids/candles non rafraîchis sans retry → pricing et détection de régime sur données stale, décisions d'allocation/grille dégradées silencieusement.
**Status** : pending
---

## 2026-06-13 15:00 — [WARNING] Drawdown -16 %/6h non arrêté par kill_switch/daily_loss_limit (cascade TAO)
**Severity** : warning
**Files** : risk/manager.py (vérif daily_loss_limit_pct / kill_switch_dd_pct) ; risk/emergency_exit.py (TAO) — lignes à localiser
**Pattern** : équity $629.43→$526.44 = **-$102.99 (-16.4 %)** sur 6h, 100 % régime range, 3 EMERGENCY EXIT tous sur **TAO** (1re apparition, hors manual_symbols), grille saine (toutes pathologies à 0). Drawdown 6h = 1.6× le seuil kill_switch (10 %) et 5× le daily_loss_limit (3 %).
**Diagnostic** : La perte d'équity (-16 %) dépasse largement les deux freins catastrophe (`daily_loss_limit_pct=0.03`, `kill_switch_dd_pct=0.10`) sans qu'ils aient flatté le book — soit ils ne se déclenchent pas (logique de calcul du DD/PnL jour cassée ou non évaluée à chaque cycle), soit le force-close déclenché échoue à soumettre et la position TAO continue de saigner (cf. proposition pending 06-02 : submit reduce_only échoue silencieusement, 4 HyperliquidClientError cette fenêtre). La concentration sur un seul symbole nouveau (TAO, 3 emergency) suggère une position TAO ouverte et mal maîtrisée. Hors périmètre paramètre (interdiction de toucher les caps) → revue humaine. Je ne lis pas le code (workflow).
**Proposed fix** :
```python
# Schéma de vérification à confirmer dans risk/manager.py :
# (1) confirmer que le DD intraday/equity-peak et le PnL jour sont recalculés
#     à CHAQUE cycle et comparés à kill_switch_dd_pct / daily_loss_limit_pct ;
# (2) confirmer que le franchissement déclenche un flat GLOBAL (toutes positions)
#     et pas seulement un blocage de nouvelles entrées ;
# (3) vérifier que le force-close TAO (emergency/kill) confirme le flat réel
#     (relecture szi post-ordre) et retombe sur market_close si l'ordre est rejeté
#     (réf. proposition pending 06-02).
# NE PAS modifier les valeurs des caps (garde-fou audit) — seulement la logique
# de déclenchement/exécution.
```
**Risk si non corrigé** : Si les freins catastrophe ne stoppent pas un drawdown, une seule fenêtre destructrice (-16 % ici) peut se répéter et vider le compte ; un force-close qui échoue laisse une position perdante (TAO) ouverte au-delà des limites de risque.
**Status** : pending
---

## 2026-06-15 09:00 — [CRITICAL] Boucle emergency-exit runaway sur HYPE (514/6h) + 6583 submit errors
**Severity** : critical
**Files** : risk/emergency_exit.py (déclenchement par symbole) + execution/engine.py:157-164 (submit) — lignes à localiser
**Pattern** : `EMERGENCY EXIT : 542` sur 6h dont **HYPE×514** (~85/h), vs record historique 21 (06-10). `6583 HyperliquidClientError` + `541 exception` (vs max 203 le 06-02). Échantillons : `ExecutionEngine submit error LINK buy / BTC buy / XRP buy: HyperliquidClientError`.
**Diagnostic** : Deux pathologies couplées d'ampleur inédite (×25 le record emergency, ×30 le record d'erreurs). (1) **Boucle runaway** : l'emergency exit tire 514 fois sur HYPE en 6h. Mécanisme probable : emergency déclenche un reduce_only sur HYPE, le submit échoue (HyperliquidClientError), la position ne se solde jamais → le cycle suivant ré-évalue la même position encore ouverte et re-tire emergency, sans garde anti-retrigger par symbole ni vérif szi post-ordre. Le filet emergency s'applique désormais à HYPE depuis que `manual_symbols=[]` (06-13), exposant une grosse position 10x au force-close en boucle. (2) **Échec submit systémique** : 6583 HyperliquidClientError, sur des **buys** d'entrée multi-symboles (pas seulement les sorties) → toute la voie d'exécution rejette ce window. Équity quasi-plate (+0.57 %) justement parce que rien ne s'exécute. La cause racine HL reste invisible (repr tronqué — déjà pointé par pending 06-02). Hors périmètre paramètre. Je ne lis pas le code (workflow).
**Proposed fix** :
```python
# Schéma — à confirmer/localiser par l'humain.
# (A) risk/emergency_exit.py : garde anti-boucle par symbole.
#   - après un déclenchement emergency sur SYM, poser un cooldown (ex. 60-120 s)
#     ET re-lire szi(SYM) avant de re-tirer : si un ordre de sortie est déjà
#     en vol / la position est inchangée et l'ordre précédent a échoué, ne pas
#     spammer un nouveau reduce_only (back-off borné, compteur de tentatives).
#   - si N tentatives échouent d'affilée sur SYM → escalader market_close
#     (réf. pending 06-02) puis alerter, au lieu de boucler indéfiniment.
# (B) execution/engine.py:157-164 : appliquer le fallback market_close +
#     log HL complet de la proposition pending 06-02 (toujours non mergée) —
#     6583 erreurs masquées = cause racine du submit-fail invisible.
# NE PAS toucher aux caps de risque ni aux flags (garde-fou audit).
```
**Risk si non corrigé** : Une position (HYPE 10x) que le système croit en cours de fermeture mais qui ne se solde jamais génère une boucle infinie de force-close ratés (~85/h), sature l'API HL (6583 erreurs → rate-limit, candles/mids stale), et laisse la position réellement exposée au-delà des limites de risque. Le prochain mouvement adverse sur HYPE pourrait infliger une perte non plafonnée pendant que la boucle tourne à vide.
**Status** : pending
---

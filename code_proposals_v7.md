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

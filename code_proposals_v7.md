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

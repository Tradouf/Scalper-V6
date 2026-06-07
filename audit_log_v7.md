# Audit log V7 (append-only, écrit par scripts/audit_v7.sh)

## 2026-06-01 21:00 (audit Opus V7)
**Métriques 6h** : szi0_frozen=0, emergency=1 (BTC), drift=0, breakout=0, errors=5 (2 error/1 ValueError/1 HyperliquidClientError/1 AttributeError), equity=$709.90→$709.83
**Diagnostic** : Aucun pattern net. Grille saine (toutes pathologies à 0), régime 100% trend_down, equity quasi-plat. Erreurs éparses, aucun type ≥50.
**Changes** : aucun
**Code proposals** : aucune
**Alerts** : aucun (EMERGENCY EXIT BTC isolé <3, à surveiller au prochain audit ; HyperliquidClientError submit AAVE buy ponctuelle)

## 2026-06-02 21:00 (audit Opus V7)
**Métriques 6h** : szi0_frozen=3, emergency=3 (LINK/DOGE/BNB, 1 chacun), drift=18, breakout=0, errors=206 dont 203 HyperliquidClientError, equity=$707.92→$708.42
**Diagnostic** : Grille saine (szi0/abandons à 3, sous seuils). 203 HyperliquidClientError (≥50) = submit errors sur sells (LINK/DOGE) corrélés aux 3 EMERGENCY EXIT → les sorties échouent à soumettre, position potentiellement non flat. DRIFT=18<20 et equity en hausse → pas de baisse drift_window. Doublons=30 mais pas de breakdown par actif et health_check_sec vient d'être baissé 300→30 (cause attendue) → pas de bump min_spacing_ticks (anti-oscillation).
**Changes** : aucun
**Code proposals** : 1 (WARNING — submit sell échoue silencieusement, fallback market_close sur reduce_only + log HL complet)
**Alerts** : 203 HyperliquidClientError sur sells + 3 emergency exits multi-symboles : risque de positions non fermées, cause racine masquée par logs tronqués → revue humaine de la proposition recommandée.

## 2026-06-06 09:00 (audit Opus V7)
**Métriques 6h** : szi0_frozen=0, emergency=0, drift=0, breakout=0, errors=6 (2 error/1 ValueError/1 MaxRetryError/1 ConnectTimeoutError/1 AttributeError), equity=$692.78→$694.51
**Diagnostic** : Aucun pattern net. Régime 100% high_vol (125 ticks), toutes pathologies grille à 0, equity en légère hausse (+$1.73). Erreurs transitoires éparses (ConnectTimeout/MaxRetryError sur refresh allMids HL), aucun type ≥50. BootReconciler: 96 ghosts orphelins signalés au boot (informatif, à surveiller).
**Changes** : aucun
**Code proposals** : aucune (proposition submit-sell du 06-02 toujours pending)
**Alerts** : aucun

## 2026-06-07 15:00 (audit Opus V7)
**Métriques 6h** : szi0_frozen=3, emergency=15, drift=42, breakout=0, errors=18 (8 error/3 HyperliquidClientError/2 MaxRetryError/2 ConnectTimeoutError/1 ValueError/1 ReadTimeout/1 AttributeError), equity=$693.83→$680.29
**Diagnostic** : Régime 100% range. Grille subit des mini-trends : 42 DRIFT / 0 BREAKOUT / 0 désactivation, équity en baisse (-$13.54) → match pattern "lâcher plus vite". Surtout : 15 EMERGENCY EXIT multi-symboles (DOGE×5, SUI×3, BTC×2, AAVE×2…) vs 0-3 historique = 5× le niveau habituel et probable cause majeure de la perte d'équity. risk/emergency_exit.py est modifié non-commité → suspicion de régression. szi0/abandons/doublons sous seuils, aucun type d'erreur ≥50.
**Changes** : - `grid.drift_window_sec`: 900 → 600 — 42 DRIFT/0 BREAKOUT/0 désact en range + équity en baisse, lâcher les mini-trends plus vite
**Code proposals** : 1 (info — spike EMERGENCY EXIT 15 corrélé aux modifs non-commitées de emergency_exit.py, investigation humaine)
**Alerts** : 15 EMERGENCY EXIT (vs 0-3 habituel), DOGE récurrent — revue humaine de risk/emergency_exit.py (modifié non-commité) recommandée.

## 2026-06-07 09:00 (audit Opus V7)
**Métriques 6h** : szi0_frozen=0, emergency=0, drift=0, breakout=0, errors=7 (3 error/1 ValueError/1 ReadTimeoutError/1 HyperliquidClientError/1 AttributeError), equity=$693.84→$693.84
**Diagnostic** : Aucun pattern net. Régime 99% range (709/716 ticks), 8 activations grille sans aucune désactivation/DRIFT/frozen, equity strictement plat. Erreurs transitoires éparses (ReadTimeout/get_open_orders sur refresh HL), aucun type ≥50. BootReconciler: 10 ghosts orphelins au boot (informatif).
**Changes** : aucun
**Code proposals** : aucune (proposition submit-sell du 06-02 toujours pending)
**Alerts** : aucun

## 2026-06-07 21:00 (audit Opus V7)
**Métriques 6h** : szi0_frozen=3, emergency=0, drift=1, breakout=0, errors=8 (5 error/1 ValueError/1 ReadTimeout/1 AttributeError), equity=$680.30→$680.25
**Diagnostic** : Aucun pattern net. Régime 100% range. Spike EMERGENCY EXIT du précédent audit résorbé (15→0). Toutes pathologies grille sous seuils (szi0=3, abandons=3, drift=1, doublons=6), 32 activations sans désactivation, equity quasi-plat (-$0.05). Erreurs transitoires éparses (ReadTimeout refresh allMids), aucun type ≥50. drift_window déjà abaissé à 600 au dernier audit.
**Changes** : aucun
**Code proposals** : aucune (submit-sell 06-02 et spike emergency 06-07 toujours pending)
**Alerts** : aucun

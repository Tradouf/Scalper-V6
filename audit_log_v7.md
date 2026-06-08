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

## 2026-06-08 03:00 (audit Opus V7)
**Métriques 6h** : szi0_frozen=0, emergency=15, drift=45, breakout=6, errors=11 (3 HyperliquidClientError/3 error/1 ValueError/1 MaxRetryError/1 exception/1 ConnectTimeout/1 AttributeError), equity=$680.25→$685.54
**Diagnostic** : Régime 100% range. Grille saine (szi0/abandons/rejets RO=0). DRIFT=45 mais BREAKOUT=6 présent ET équity en HAUSSE (+$5.29) → pattern "lâcher plus vite" NON validé + drift_window vient d'être abaissé à 600 (anti-oscillation). Doublons=12 non ventilés par actif, health_check_sec récemment baissé (cause attendue) → pas de bump min_spacing. EMERGENCY EXIT=15 (SUI/LINK/DOGE×4, SOL×2, AAVE×1) : re-spike après résorption à 0 au dernier audit, MAIS équity en hausse cette fenêtre (vs -$13.54 le 06-07 15:00) → exits non destructeurs d'équity ici. Exception au force-close DOGE (HyperliquidClientError) = même famille que les pending submit-sell (06-02) / spike emergency (06-07). Aucun type d'erreur ≥50.
**Changes** : aucun
**Code proposals** : aucune (2 pending couvrent le sujet emergency/submit-exit — pas de re-proposition)
**Alerts** : EMERGENCY EXIT=15 récurrent (0→15) + exception force-close DOGE — revue humaine des 2 propositions pending recommandée (submit-sell 06-02, spike emergency 06-07).

## 2026-06-08 09:00 (audit Opus V7)
**Métriques 6h** : szi0_frozen=2, emergency=16, drift=31, breakout=0, errors=28 (12 error/8 NameError/2 HyperliquidClientError/2 Error/1 ValueError/1 ProtocolError/1 HTTPError/1 ConnectionResetError), equity=$685.56→$683.05
**Diagnostic** : Régime ~99% range. Signal dominant = bug structurel : 8 NameError("name 'prob_range' is not defined") sur l'activation grille (SUI/DOGE) → activation avortée silencieusement = régression code (proposition déposée). DRIFT=31/BREAKOUT=0 equity en légère baisse (-$2.51) matcherait "lâcher plus vite", MAIS (1) drift_window déjà abaissé à 600 le 06-07 (anti-oscillation), (2) l'activation grille est cassée cette fenêtre (NameError) → signal drift contaminé, (3) la baisse d'équity est surtout drainée par les 16 emergency directionnels, pas la grille → je tiens drift_window. EMERGENCY EXIT=16 (SOL×4/SUI×3/ETH×3/LINK×2/BNB×2/AAVE×2) reste sur le niveau spike récurrent mais équity non destructrice ici — déjà couvert par 2 propositions pending (pas de re-proposition). szi0=2/abandons=0/doublons=10 non ventilés sous seuils. Aucun type d'erreur ≥50.
**Changes** : aucun
**Code proposals** : 1 (warning — NameError 'prob_range' non défini, activation grille avortée)
**Alerts** : NameError 'prob_range' = régression code bloquant l'activation grille (SUI/DOGE) — revue humaine recommandée ; EMERGENCY EXIT=16 récurrent (2 propositions pending toujours non traitées).

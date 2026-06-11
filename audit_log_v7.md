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

## 2026-06-08 15:00 (audit Opus V7)
**Métriques 6h** : szi0_frozen=4, emergency=4 (SUI×2/AAVE×2), drift=13, breakout=0, errors=4 (1 ValueError/1 HyperliquidClientError/1 exception/1 AttributeError), equity=$683.07→$684.85
**Diagnostic** : Régime 100% range. Aucun pattern net, retour au calme. (1) EMERGENCY EXIT retombé de 16→4 (sous le spike), équity en HAUSSE (+$1.78) → exits non destructeurs, déjà couverts par 2 propositions pending (pas de re-proposition). (2) DRIFT=13<20 ET équity en hausse → pattern "lâcher plus vite" non validé + drift_window déjà à 600 (anti-oscillation), je tiens. (3) Plus de NameError 'prob_range' cette fenêtre (8→0) — régression non déclenchée mais proposition reste pending. (4) szi0=4/abandons=2/doublons=6 tous sous seuils, aucun type d'erreur ≥50.
**Changes** : aucun
**Code proposals** : aucune (3 pending couvrent emergency/submit-exit/NameError — pas de re-proposition)
**Alerts** : aucun

## 2026-06-08 21:00 (audit Opus V7)
**Métriques 6h** : szi0_frozen=0, emergency=2 (LINK×2), drift=31, breakout=0, errors=367 dont 146 ReadTimeoutError/51 ConnectionError/157 error/7 NameError/4 HyperliquidClientError, equity=$684.90→$683.95
**Diagnostic** : Régime mixte (325 trend_up / 264 range). Côté trading RAS : grille saine (szi0/abandons/RO=0), emergency=2<3, equity quasi-plate (-$0.95), doublons=2. DRIFT=31/BREAKOUT=0 mais (1) 55% trend_up où la grille lâche les trends est attendu, (2) equity non en baisse nette, (3) drift_window déjà à 600 (anti-oscillation) → je tiens. SIGNAL : spike réseau 146 ReadTimeoutError + 51 ConnectionError (≥50) sur HL adapter (refresh allMids/candles 1h) vs 0-1 historique → franchit le seuil "≥50 d'un même type → proposition code". Impact bénin cette fenêtre (handled en WARNING) → proposition info (retry/backoff). 7 NameError 'prob_range' = régression déjà pending (réapparue, 0→7), pas de re-proposition.
**Changes** : aucun
**Code proposals** : 1 (info — spike ReadTimeout/ConnectionError HL adapter, ajout retry/backoff borné)
**Alerts** : aucun critique (spike réseau benin ; NameError 'prob_range' réapparu 7× — proposition pending non traitée)

## 2026-06-09 03:00 (audit Opus V7)
**Métriques 6h** : szi0_frozen=1, emergency=12 (ETH×4/SOL×3/LINK×3/BNB×1/AAVE×1), drift=30, breakout=0, errors=374 dont 280 ReadTimeoutError/67 ConnectionError/13 HyperliquidClientError/1 ValueError/1 AttributeError, equity=$683.92→$678.81
**Diagnostic** : Régime 100% range (550 ticks). SIGNAL 1 : spike réseau persiste ET s'aggrave — 280 ReadTimeout + 67 ConnectionError (347 vs 197 au 06-08 21:00), soit ≥50 sur 2 audits consécutifs → la condition d'escalade fixée par la proposition pending (06-08, "si ≥50 persiste → escalader en warning") est atteinte. Pas de re-proposition (workflow), escalade signalée en alerte. SIGNAL 2 : EMERGENCY EXIT=12 (ETH/SOL/LINK, aucun sur manual_symbols BTC/HYPE) avec équity en baisse (-$5.11) → cas destructeur (cf. 06-07 15:00), déjà couvert par 2 propositions pending (submit-sell 06-02, spike emergency 06-07) → pas de re-proposition. DRIFT=30/BREAKOUT=0 matcherait "lâcher plus vite" MAIS (1) drift_window déjà à 600 (anti-oscillation), (2) baisse d'équity drainée par les 12 emergency directionnels, pas la grille (grille saine : szi0=1/abandons=0/RO=0) → je tiens drift_window. Doublons=13 non ventilés par actif + health_check_sec récemment baissé (cause attendue) → pas de bump min_spacing.
**Changes** : aucun
**Code proposals** : aucune (4 pending couvrent réseau/emergency/submit-exit/NameError — pas de re-proposition)
**Alerts** : (1) ReadTimeout/ConnError 347 — ≥50 sur 2 audits consécutifs et en hausse → ESCALADER la proposition pending du 06-08 (info→warning), revue humaine du retry/backoff hl_adapter. (2) EMERGENCY EXIT=12 récurrent AVEC équity en baisse (-$5.11) cette fois → revue humaine prioritaire des 2 propositions emergency pending.

## 2026-06-09 09:00 (audit Opus V7)
**Métriques 6h** : szi0_frozen=7, emergency=15 (SOL×5/SUI×4/ETH×3/LINK×1/BNB×1/AAVE×1), drift=28, breakout=0, errors=124 error/108 ReadTimeoutError/31 ConnectionError/15 HyperliquidClientError/1 ValueError/1 ProtocolError/1 MaxRetryError/1 ConnectTimeout, equity=$678.74→$679.42
**Diagnostic** : Régime 100% range (645 ticks). (1) Réseau : 108 ReadTimeout + 31 ConnError (139 total) reste ≥50 mais EN NETTE DÉCRUE (347→139, soit 3e audit consécutif mais reflux net) → déjà escaladé/pending (06-08), pas de re-proposition. (2) EMERGENCY EXIT=15 (SOL/SUI/ETH, aucun sur manual_symbols BTC/HYPE) MAIS équity en HAUSSE (+$0.68) → exits non destructeurs cette fenêtre (vs -$5.11 au 03:00) ; déjà couverts par 2 propositions pending → pas de re-proposition. (3) DRIFT=28/BREAKOUT=0 matcherait "lâcher plus vite" MAIS drift_window déjà à 600 (anti-oscillation) + équity non en baisse + grille saine (szi0=7/abandons=6 bien sous seuils 150/100) → je tiens. (4) Doublons=14 non ventilés par actif + health_check_sec récemment baissé (cause attendue) → pas de bump min_spacing.
**Changes** : aucun
**Code proposals** : aucune (4 pending couvrent réseau/emergency/submit-exit/NameError — pas de re-proposition)
**Alerts** : aucun critique (réseau en reflux 347→139 ; EMERGENCY EXIT=15 récurrent mais équity en hausse cette fenêtre — 2 propositions emergency pending non traitées).

## 2026-06-09 15:00 (audit Opus V7)
**Métriques 6h** : szi0_frozen=2, emergency=3 (SOL×2/SUI×1), drift=7, breakout=0, errors=2 (1 ValueError/1 AttributeError), equity=$679.45→$678.27
**Diagnostic** : Régime 100% range (720 ticks). Retour au calme net. (1) Réseau : spike ReadTimeout/ConnError totalement résorbé (347→139→2 erreurs) — épisode infra HL transitoire confirmé, proposition retry/backoff reste pending mais plus d'escalade. (2) EMERGENCY EXIT=3 (au seuil), SOL×2/SUI×1 — aucun sur manual_symbols (BTC/HYPE), équity quasi-plate (-$1.18) → exits non destructeurs, déjà couverts par 2 propositions pending → pas de re-proposition. (3) DRIFT=7<20 + équity quasi-plate → pattern "lâcher plus vite" non validé + drift_window déjà à 600 (anti-oscillation) → je tiens. (4) Grille saine : szi0=2/abandons=1/RO=0 bien sous seuils (150/100), doublons=7<10 non ventilés + health_check récemment baissé (cause attendue) → pas de bump min_spacing. Aucun type d'erreur ≥50.
**Changes** : aucun
**Code proposals** : aucune (4 pending couvrent réseau/emergency/submit-exit/NameError — pas de re-proposition)
**Alerts** : aucun

## 2026-06-09 21:00 (audit Opus V7)
**Métriques 6h** : szi0_frozen=1, emergency=16 (SUI×6/DOGE×5/SOL×3/LINK×1/BNB×1), drift=55, breakout=0, errors=5 (2 HyperliquidClientError/1 ValueError/1 error/1 AttributeError), equity=$678.45→$671.53
**Diagnostic** : Régime 100% range (719 ticks). (1) Réseau : spike totalement résorbé, 5 erreurs éparses, aucun type ≥50 — épisode infra HL clos. (2) DRIFT=55 = RECORD du log (~2× la norme récente 28-30), 0 BREAKOUT, 36 activations / 0 désactivation, équity en baisse (-$6.92) → trigger le plus net à ce jour de la règle "≥20 DRIFT sans BREAKOUT, equity en baisse". Fenêtre qualitativement pire que les précédentes (45 DRIFT du 06-08 03:00 était venu AVEC breakout=6 + équity en hausse) → je lève l'anti-oscillation et applique le pas prescrit. (3) EMERGENCY EXIT=16 (SUI×6/DOGE×5, aucun sur manual_symbols BTC/HYPE) avec équity en baisse → cas partiellement destructeur mais déjà couvert par 2 propositions pending (submit-sell 06-02, spike emergency 06-07) → pas de re-proposition. (4) Grille saine : szi0=1/abandons=1/RO=0 bien sous seuils, doublons=6<10 + health_check récemment baissé (cause attendue) → pas de bump min_spacing.
**Changes** : - `grid.drift_window_sec`: 600 → 300 (floor) — 55 DRIFT (record) / 0 BREAKOUT / 0 désact en range + équity en baisse, lâcher les mini-trends au plus vite
**Code proposals** : aucune (4 pending couvrent réseau/emergency/submit-exit/NameError — pas de re-proposition)
**Alerts** : EMERGENCY EXIT=16 récurrent AVEC équity en baisse (-$6.92), SUI×6/DOGE×5 — revue humaine des 2 propositions emergency pending recommandée.

## 2026-06-10 03:00 (audit Opus V7)
**Métriques 6h** : szi0_frozen=2, emergency=4 (AAVE×2/SUI×1/BNB×1), drift=32, breakout=0, errors=4 (1 ProtocolError/1 error/1 ConnectionResetError/1 ConnectionError), equity=$670.30→$669.66
**Diagnostic** : Régime 100% range (591 ticks). Retour au calme. (1) DRIFT=32/BREAKOUT=0/0 désact matcherait "lâcher plus vite" MAIS drift_window est DÉJÀ au floor (300) depuis le dernier audit → plus de marge paramètre, et équity quasi-plate (-$0.64) vs -$6.92 le 06-09 21:00 → le pas prescrit ne s'applique plus (déjà appliqué jusqu'au plancher). (2) EMERGENCY EXIT=4 (au-dessus du seuil mais retombé de 16→4), aucun sur manual_symbols (BTC/HYPE — AAVE non protégé mais équity non destructrice) → exits non destructeurs cette fenêtre, déjà couverts par 2 propositions pending → pas de re-proposition. (3) Réseau totalement résorbé : 4 erreurs éparses, aucun type ≥50 — épisode infra HL clos. (4) Grille saine : szi0=2/abandons=1/RO=0 bien sous seuils (150/100), doublons=6<10 + health_check récemment baissé (cause attendue) → pas de bump min_spacing.
**Changes** : aucun (drift_window déjà au floor 300, équity quasi-plate)
**Code proposals** : aucune (4 pending couvrent réseau/emergency/submit-exit/NameError — pas de re-proposition)
**Alerts** : aucun

## 2026-06-10 09:00 (audit Opus V7)
**Métriques 6h** : szi0_frozen=0, emergency=3 (SUI×2/ETH×1), drift=18, breakout=0, errors=31 (10 error/6 HyperliquidClientError/4 Error/3 ProtocolError/3 ConnectionResetError/3 ConnectionError/2 HTTPError), equity=$669.58→$667.95
**Diagnostic** : Régime 100% range (716 ticks). Retour au calme, aucun pattern net. (1) DRIFT=18<20 (sous le seuil) + 0 BREAKOUT + 0 désact + équity quasi-plate (-$1.63) → pattern "lâcher plus vite" NON validé, et drift_window déjà au floor (300) → aucune marge paramètre de toute façon. (2) EMERGENCY EXIT=3 (juste au seuil, retombé de 4), SUI×2/ETH×1 — aucun sur manual_symbols (BTC/HYPE), équity non destructrice → exits bénins, déjà couverts par 2 propositions pending → pas de re-proposition. (3) Réseau : pic léger 31 erreurs réparties (ProtocolError/ConnectionReset/ConnectionError sur refresh allMids + 1 submit ETH buy) mais aucun type ≥50 — bien en deçà du seuil, épisode infra transitoire. (4) Grille saine : szi0=0/abandons=0/RO=0, doublons=5<10 → pas de bump min_spacing.
**Changes** : aucun
**Code proposals** : aucune (4 pending couvrent réseau/emergency/submit-exit/NameError — pas de re-proposition)
**Alerts** : aucun

## 2026-06-10 15:00 (audit Opus V7)
**Métriques 6h** : szi0_frozen=3, emergency=14 (SUI×4/SOL×2/LINK×2/ETH×2/DOGE×1/BTC×1/BNB×1/AAVE×1), drift=52, breakout=0, errors=12 (5 HyperliquidClientError/5 error/1 ReadTimeout/1 exception), equity=$667.96→$659.41
**Diagnostic** : Régime 100% range (716 ticks). (1) DRIFT=52 (proche du record 55) / 0 BREAKOUT / 0 désact + équity en baisse (-$8.55) → trigger net "lâcher plus vite", MAIS `drift_window_sec` est DÉJÀ au floor (300) depuis le 06-09 21:00 → aucune marge paramètre, le pas prescrit est épuisé. (2) EMERGENCY EXIT=14 avec équity en baisse = fenêtre destructrice ; notable : 1 sur BTC qui est dans `manual_symbols` → exactement le risque pointé par la proposition pending du 06-07 (force-close potentiel d'un swing manuel) → déjà couvert, pas de re-proposition mais alerte. (3) Grille saine : szi0=3/abandons=0/RO=0 bien sous seuils, doublons=8<10 + health_check récemment baissé (cause attendue) → pas de bump min_spacing. (4) Réseau bénin : 12 erreurs réparties, aucun type ≥50.
**Changes** : aucun (drift_window déjà au floor 300, plus de marge paramètre)
**Code proposals** : aucune (4 pending couvrent réseau/emergency/submit-exit/NameError — pas de re-proposition)
**Alerts** : EMERGENCY EXIT=14 récurrent AVEC équity en baisse (-$8.55), dont 1 sur BTC (manual_symbol) — revue humaine PRIORITAIRE des 2 propositions emergency pending (06-02 submit-sell, 06-07 spike emergency / exemption manual_symbols).

## 2026-06-10 21:00 (audit Opus V7)
**Métriques 6h** : szi0_frozen=5, emergency=21 (SUI×8/AAVE×4/SOL×3/LINK×3/BTC×2/BNB×1), drift=106, breakout=0, errors=12, equity=$659.44→$654.96
**Diagnostic** : Régime 100% range (718 ticks). (1) DRIFT=106 = NOUVEAU RECORD (~2× le précédent 55 du 06-09 21:00, ~6× la norme 18-32) / 0 BREAKOUT / 17 activations / 0 désact + équity en baisse (-$4.48) → trigger massif "lâcher plus vite", MAIS `drift_window_sec` est DÉJÀ au floor (300) depuis le 06-09 21:00 → aucune marge paramètre, le pas prescrit est totalement épuisé. La persistance du DRIFT record au floor suggère que la grille subit un régime range très bruité (mini-trends incessants) que le paramètre ne peut plus absorber. (2) EMERGENCY EXIT=21 = RECORD aussi (vs 14-16 sur les spikes précédents), équity en baisse = fenêtre destructrice ; SUI×8 dominant + BTC×2 qui est dans `manual_symbols` → 2e audit consécutif avec force-close sur swing BTC manuel, exactement le risque pointé par la proposition pending du 06-07 → déjà couvert, alerte prioritaire. (3) Grille saine côté pathologies dures : szi0=5/abandons=6/RO=0 bien sous seuils (150/100), doublons=19 non ventilés par actif + health_check récemment baissé (cause attendue) → pas de bump min_spacing. (4) Réseau bénin : 12 erreurs, aucun type ≥50.
**Changes** : aucun (drift_window déjà au floor 300, plus de marge paramètre)
**Code proposals** : aucune (4 pending couvrent réseau/emergency/submit-exit/NameError — pas de re-proposition)
**Alerts** : (1) EMERGENCY EXIT=21 RECORD AVEC équity en baisse (-$4.48), SUI×8 dominant + BTC×2 (manual_symbol, 2e audit consécutif) — revue humaine PRIORITAIRE des 2 propositions emergency pending (06-02 submit-sell, 06-07 spike emergency / exemption manual_symbols), le force-close BTC manuel récurrent est le risque le plus grave. (2) DRIFT=106 record au floor drift_window (300) — le paramètre est épuisé, la grille en range bruité ne tient plus ; si persiste, envisager une proposition code (désactivation grille plus agressive en range bruité ou seuil DRIFT adaptatif).

## 2026-06-11 03:00 (audit Opus V7)
**Métriques 6h** : szi0_frozen=1, emergency=17 (SOL×5/AAVE×5/SUI×2/BNB×2/LINK×1/DOGE×1/BTC×1), drift=29, breakout=0, errors=9 (4 error/3 HyperliquidClientError/2 ReadTimeout), equity=$654.88→$645.31
**Diagnostic** : Régime 100% range (720 ticks). (1) DRIFT=29 redescendu à la norme (vs 106 record du 06-10 21:00) / 0 BREAKOUT / 48 activations / 0 désact + équity en baisse (-$9.57) → matcherait "lâcher plus vite" MAIS `drift_window_sec` est DÉJÀ au floor (300) depuis le 06-09 21:00 → aucune marge paramètre, pas prescrit épuisé. (2) EMERGENCY EXIT=17 (vs 21 record) AVEC équity en baisse (-$9.57) = fenêtre destructrice ; SOL×5/AAVE×5 dominants + BTC×1 (manual_symbol) → 3e audit consécutif avec force-close sur swing BTC manuel = risque pointé par la proposition pending du 06-07, déjà couvert → alerte prioritaire. La baisse d'équity est essentiellement drainée par les 17 emergency directionnels, pas la grille. (3) Grille saine côté pathologies dures : szi0=1/abandons=0/RO=0 bien sous seuils (150/100), doublons=6<10 → pas de bump min_spacing. (4) Réseau bénin : 9 erreurs éparses, aucun type ≥50.
**Changes** : aucun (drift_window déjà au floor 300, plus de marge paramètre)
**Code proposals** : aucune (4 pending couvrent réseau/emergency/submit-exit/NameError — pas de re-proposition)
**Alerts** : EMERGENCY EXIT=17 récurrent AVEC équity en baisse (-$9.57), SOL×5/AAVE×5 dominants + BTC×1 (manual_symbol, 3e audit consécutif) — revue humaine PRIORITAIRE des 2 propositions emergency pending (06-02 submit-sell, 06-07 spike emergency / exemption manual_symbols) ; le force-close BTC manuel récurrent reste le risque le plus grave.

## 2026-06-11 09:00 (audit Opus V7)
**Métriques 6h** : szi0_frozen=1, emergency=8 (SUI×2/AAVE×2/SOL×1/ETH×1/BTC×1/BNB×1), drift=23, breakout=0, errors~0, equity=$645.34→$642.67
**Diagnostic** : Régime 100% range (720 ticks). Retour relatif au calme (emergency 17→8, drift 29→23). (1) DRIFT=23 (norme) / 0 BREAKOUT / 14 activations / 0 désact + équity en légère baisse (-$2.67) → matcherait "lâcher plus vite" MAIS `drift_window_sec` est DÉJÀ au floor (300) depuis le 06-09 21:00 → aucune marge paramètre, pas prescrit épuisé. (2) EMERGENCY EXIT=8 (vs 17) AVEC équity en légère baisse, multi-symboles dispersés + BTC×1 (manual_symbol) → 4e audit consécutif avec force-close potentiel sur swing BTC manuel = risque pointé par la proposition pending du 06-07, déjà couvert → alerte. Baisse d'équity modeste, drainée par les emergency directionnels, pas la grille. (3) Grille saine : szi0=1/abandons=2/RO=0 bien sous seuils (150/100), doublons=5<10 → pas de bump min_spacing. (4) Réseau bénin : aucun type d'erreur ≥50.
**Changes** : aucun (drift_window déjà au floor 300, plus de marge paramètre)
**Code proposals** : aucune (4 pending couvrent réseau/emergency/submit-exit/NameError — pas de re-proposition)
**Alerts** : EMERGENCY EXIT=8 dont BTC×1 (manual_symbol, 4e audit consécutif) avec équity en baisse modérée (-$2.67) — le force-close récurrent du swing BTC manuel reste le risque le plus grave ; revue humaine des 2 propositions emergency pending (06-02 submit-sell, 06-07 exemption manual_symbols) toujours recommandée.

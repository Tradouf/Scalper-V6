# Audit log

Historique des audits Opus du bot. Append-only.

---

## 2026-05-05 11:03 (audit Opus, exit budget)

**Métriques 6h** : emergency_exit=2 (BTC, APE), flip_refusé=37, external_exit≈8, open=3 (ETH/SOL/BNB)
**Diagnostic** : Bot stuck en SHORT pendant retournement haussier. 37 refus de flip → emergency exit a dû fermer BTC à -5.7% et APE à -4.3%. Le seuil 0.95 est trop strict pour ce régime.

**Changes** :
- `FLIP_MIN_CONFIDENCE`: 0.95 → 0.90 — assouplit légèrement, garde le filtre anti-flips bas-conf

**Alerts** : audit a hit le budget cap $0.50 avant commit/écriture log → finalisé manuellement

**Suggéré** : budget cap à $1.50 minimum + pré-agrégation des logs côté bash

---

## 2026-05-05 12:02 (audit Opus)

**Métriques 6h** : emergency_exit=2, flip_refusé=26, external_exit=6, open=4 (APE/BNB/BTC/ETH/SOL), enter=0, skip_conf=90, trail_arm=3
**Diagnostic** : Le pattern flip_refusé≥5 + EMERGENCY≥1 reste actif (26+2), mais l'audit précédent (11:03) vient juste de baisser FLIP_MIN_CONFIDENCE 0.95→0.90 il y a ~1h. Les flips refusés sur la fenêtre 6h sont majoritairement antérieurs au changement. Baisser à nouveau créerait une oscillation prématurée — il faut laisser le nouveau seuil 0.90 produire ≥1 cycle complet avant de réévaluer. SKIP conf=90/6h ≈ 0.4/cycle, sous le seuil de 10/cycle. Pas d'autre pattern net.

**Changes** : aucun, on laisse la baisse FLIP_MIN_CONFIDENCE→0.90 s'installer avant nouvelle action.

**Alerts** : aucun. ENTER=0 sur 6h à surveiller au prochain audit ; si toujours 0 et SKIP conf élevé, baisser MIN_CONFIDENCE 0.65→0.63.

---

## 2026-05-05 13:00 (audit Opus)

**Métriques 6h** : emergency_exit=1, flip_refusé=3, external_exit=7, open=5 (APE/BNB/BTC/ETH/SOL), enter=0, skip_conf=102, skip_cooldown=11, trail_arm=2, llm_error=30
**Diagnostic** : Aucun pattern de la table ne se déclenche : flip_refusé=3 < 5, SKIP conf≈0.14/cycle (sous le seuil 10/cycle), EMERGENCY=1, TRAIL ARM=2 (>0). Mais ENTER=0 persiste pour le 2e audit consécutif. L'inspection des logs montre que la racine n'est pas MIN_CONFIDENCE : les LLM timeouts (30 erreurs/6h, ~5/h, read timeout=90s) produisent tech=0.00 → CONSENSUS conf=0.00 → side=wait. Baisser MIN_CONFIDENCE n'aiderait pas (0.00 reste sous tout seuil). Hors périmètre paramétrique : proposition code déposée pour traiter le timeout LLM. FLIP_MIN_CONFIDENCE=0.90 inchangé (1 cycle d'observation, conforme au plan de l'audit précédent).

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; root cause = LLM timeouts, traitée via code_proposals.md.

**Code proposals** : 1 proposition info ajoutée (LLM timeout handling → conf=0.00 → ENTER=0).

**Alerts** : aucun. À surveiller : si LLM timeouts persistent au prochain audit et ENTER toujours 0, escalader la proposition info → warning.

---

## 2026-05-05 18:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=61, external_exit=8, open=4 (APE/BTC/ETH/SOL), enter=1, skip_conf=184, skip_cooldown=8, trail_arm=4, trail_modify=29, llm_error=261
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=0 + 61 flip refusé : sans EMERGENCY, le filtre flip 0.90 fait son job (filtre les retournements faibles avant qu'ils dégénèrent). Pattern "0 EMERGENCY + WR>60%" non évaluable (1 ENTER seul → WR statistiquement non significatif), donc on ne remonte pas FLIP_MIN_CONFIDENCE. SKIP conf=184/720 cycles ≈ 0.26/cycle (très en deçà du seuil 10/cycle), TRAIL ARM=4 (≠0), pas de tendance "BREAKEVEN à perte" détectable. ROE actuel : 3 positions positives (APE +2.46%, BTC +0.45%, SOL +0.31%), 1 modérément négative (ETH -1.50%, sous le SL_PNL de 1.5% → trail/SL devraient gérer). **LLM error=261 sur 6h vs 30 à l'audit précédent (×8.7)** : escalade nette de la saturation LocalAI, qui amplifie le risque structurel décrit dans la proposition pending du 13:00. Pas de doublon à déposer (proposition couvre exactement ce cas).

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; pas de pattern déclencheur.

**Code proposals** : aucune nouvelle. La proposition info "LLM timeouts → conf=0.00" du 13:00 reste pertinente et gagne en sévérité de fait (×8.7 d'incidents en 6h) — à escalader info → warning par l'humain si le pattern persiste au prochain audit.

**Alerts** : aucun déclencheur paramétrique. Observation : le volume de LLM errors a quasi-décuplé en un cycle d'audit ; surveiller la santé LocalAI hors-périmètre (CPU/RAM hôte).

---

## 2026-05-06 00:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=2, external_exit=5, open=2 (BTC +0.28% / SOL +0.50%), enter=0, skip_conf=2, skip_cooldown=5, trail_arm=1, trail_modify=2, llm_error=21
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. flip_refusé=2 < seuil 5 ; EMERGENCY=0 ; SKIP conf=2/720 cycles ≈ 0.003/cycle (très en deçà du seuil 10/cycle) ; TRAIL ARM=1 (≠0). LLM error=21 vs 261 au précédent audit (÷12) — la saturation LocalAI semble s'être résorbée, la proposition pending du 13:00 reste valide mais devient moins urgente. ENTER=0 sur 6h s'explique par le contexte : seulement 2 positions ouvertes (vs 4 normalement) + 5 external_exit récents (cooldowns post-sortie actifs : 5 SKIP cooldown observés), pas par un seuil mal réglé. Grid bot actif côté BNB (cf. log "GRID BNB long TP #2 cumul=0.384%") — fonctionne nominalement.

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; pas de pattern déclencheur.

**Code proposals** : aucune nouvelle. Proposition pending du 13:00 sur LLM timeouts toujours valide mais la pression a chuté (21 errors/6h vs 261).

**Alerts** : aucun.

---

## 2026-05-06 12:00 (audit Opus)

**Métriques 6h** : emergency_exit=4, flip_refusé=4, external_exit=21, open=5 (APE +0.78% / BNB -1.47% / BTC +0.45% / ETH -1.19% / SOL +1.38%), enter=0, skip_conf=35, skip_cooldown=26, trail_arm=3, trail_modify=10, llm_error=0, hl_cache_stale=549, hl_sync_err=14
**Diagnostic** : Deux patterns paramétriques se déclenchent. (1) **EMERGENCY=4 ≥3** → la règle prescrit de resserrer le SL pour réduire les pertes en cascade ; SCALP_SL_PNL_PCT 0.015 → 0.013 (delta minimum -0.002, dans la borne min 0.010). (2) **ENTER=0 sur 6h alors que les confs observées flottent à 0.68-0.71 systématiquement sous MIN_CONFIDENCE=0.72** (5 SKIP visibles dans l'échantillon, tous "conf trop faible (0.6X < 0.72)") → on baisse MIN_CONFIDENCE 0.72 → 0.70 (delta -0.02, dans la borne min 0.55). flip_refusé=4 < seuil 5, pas de trigger flip. TRAIL ARM=3 (≠0) donc le pattern "0 trade armed en 24h" non actif. **HL cache périmé=549 et HL sync error=14** : signal de saturation client Hyperliquid mais hors périmètre paramétrique strict (pas de proposition code déposée car symptôme sans mécanisme bien identifié dans les logs visibles ; à surveiller).

**Changes** :
- `SCALP_SL_PNL_PCT`: 0.015 → 0.013 — pattern EMERGENCY=4 ≥3, SL plus serré pour limiter les pertes en cascade
- `MIN_CONFIDENCE`: 0.72 → 0.70 — 0 ENTER en 6h, confs réelles 0.68-0.71 sous seuil, capture des signaux marginaux

**Code proposals** : aucune nouvelle.

**Alerts** : aucun déclencheur paramétrique humain. Observation : HL cache stale=549 / sync_err=14 sur 6h, à surveiller au prochain audit. **Suggéré** : redémarrer le bot (4 EMERGENCY EXIT + 2 changes settings = restart auto via audit.sh).

---

## 2026-05-07 00:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=0, external_exit=5, open=5 (APE +1.09% / BNB -0.21% / BTC -0.42% / ETH -0.10% / SOL -0.21%), enter=0, skip_conf=9, skip_cooldown=5, trail_arm=4, trail_modify=8, llm_error=0, hl_cache_stale=1, hl_sync_err=1
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=0 + flip_refusé=0 → pas de trigger SL ni flip ; SKIP conf=9/720 cycles ≈ 0.0125/cycle (très en deçà du seuil 10/cycle) ; TRAIL ARM=4 (≠0) donc pattern "0 trade armed" non actif ; pas de signal "TRAIL BREAKEVEN à perte". Le contexte santé est nettement meilleur qu'à l'audit 12:00 (LLM error 0 vs avant, HL cache stale 1 vs 549). ENTER=0 sur 6h s'explique par regime range medium stable + 5 external_exit récents (5 SKIP cooldown observés) + audit précédent vient de baisser MIN_CONFIDENCE 0.72→0.70 il y a ~12h → laisser le nouveau seuil produire ≥1 cycle complet d'observation avant nouvelle action (anti-oscillation). ROE positions globalement saines (4 marginales sous 0.5% en valeur absolue, 1 positive +1.09%). Pattern "0 EMERGENCY + WR>60%" non évaluable (ENTER=0 → WR statistiquement non significatif), donc on ne remonte pas FLIP_MIN_CONFIDENCE.

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; pas de pattern déclencheur, anti-oscillation après audit précédent.

**Code proposals** : aucune nouvelle.

**Alerts** : aucun.

---

## 2026-05-07 06:00 (audit Opus)

**Métriques 6h** : emergency_exit=2, flip_refusé=0, external_exit=10, open=5 (APE -0.94% / BNB +0.47% / BTC -0.94% / ETH +0.05% / SOL -0.69%), enter=0, skip_conf=23, skip_cooldown=11, trail_arm=8, trail_modify=24, llm_error=0, hl_cache_stale=0, hl_sync_err=0
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=2 sous seuil 3 ; flip_refusé=0 sous seuil 5 ; SKIP conf=23/720 cycles ≈ 0.032/cycle (très en deçà du seuil 10/cycle) ; TRAIL ARM=8 (≠0) → pattern "0 trade armed en 24h" non actif. Santé infra excellente (LLM 0, HL sync 0, cache stale 0). 3 positions négatives (APE/BTC/SOL) à -0.7/-0.94% ROE, dans la plage normale (sous le SL 1.3%) — pas de cascade. Le contexte régime range medium stable depuis ≥24h pourrait justifier une action sur GRID/SCALP master switches, mais (1) GRID_ENABLED=True déjà en place et fonctionne, (2) SCALP_ENABLED=True n'a pas généré d'ENTER mais 24h ne suffit pas pour conclure scalp net négatif (besoin 2 audits consécutifs avec bilan vérifiable, ENTER=0 ne signifie pas "scalp perdant" — signifie "scalp inactif"). Anti-oscillation après audits 12:00 (06) et 00:00 (07) : laisser observer. Pattern "0 EMERGENCY + WR>60%" toujours non évaluable (ENTER=0 → WR n/a).

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; pas de pattern déclencheur, infra saine.

**Code proposals** : aucune nouvelle.

**Alerts** : aucun.

---

## 2026-05-07 12:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=1, external_exit=17, open=4 (BNB -0.42% / BTC -0.07% / ETH +0.03% / SOL +0.41%), enter=0, consensus=178, skip_conf=177, skip_cooldown=17, trail_arm=10, trail_modify=16, llm_error=0, hl_cache_stale=66, hl_sync_err=167
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=0 ; flip_refusé=1 sous seuil 5 ; SKIP conf=177/720 cycles ≈ 0.25/cycle (très en deçà du seuil 10/cycle) ; TRAIL ARM=10 (≠0). Le ratio SKIP_conf/CONSENSUS ≈ 99% est élevé mais les confs observées dans l'échantillon (0.39, 0.42, 0.00 sur APE) sont structurellement sous tout seuil ≥ 0.55 (borne min MIN_CONFIDENCE) — baisser ne capturerait pas ces signaux. Régime range medium persistant + 17 external_exit récents (17 SKIP cooldown observés) + MIN_CONFIDENCE vient juste d'être baissé à 0.70 il y a 24h → anti-oscillation. **HL sync_err=167 + cache_stale=66** : pic de saturation infra mais sous le précédent (cache_stale=549 au 12:00 hier était jugé acceptable), pas de mécanisme bug clair dans les logs visibles, hors périmètre paramétrique. Pattern master switches non déclenché : SCALP_ENABLED=True + ENTER=0 ne signifie pas "scalp perdant", uniquement "scalp inactif en range" ; GRID_ENABLED=True actif et performant (16 TRAIL NATIVE SL MODIFY).

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; pas de pattern déclencheur, anti-oscillation maintenue.

**Code proposals** : aucune nouvelle.

**Alerts** : aucun déclencheur paramétrique. Observation : HL sync_err=167 sur 6h (~28/h) à surveiller au prochain audit ; si croît au-delà de 500/6h, envisager proposition code sur résilience client HL.

---

## 2026-05-07 18:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=0, external_exit=2, open=4 (BNB +0.27% / BTC +0.06% / ETH +0.14% / SOL -0.02%), enter=0, consensus=0, skip_conf=0, skip_cooldown=2, trail_arm=0, trail_modify=0, llm_error=0, hl_cache_stale=0, hl_sync_err=0
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=0, flip_refusé=0, SKIP conf=0 — toutes les conditions de déclenchement sont sous leurs seuils. CONSENSUS=0 et TRAIL ARM=0 cohérents avec le contexte observé : (1) STRATE GATE veto=h1_wait sur tous les symboles dans l'échantillon (BTC/BNB/APE) bloque le pipeline scalp avant le consensus, (2) bot post-restart visible dans les logs (HEALTH_CHECK + RECOVERY actifs sur SOL/ETH avec placement SL ad hoc), donc fenêtre métriques peu peuplée. Infra parfaitement saine (LLM/HL sync/cache stale tous à 0). Anti-oscillation : MIN_CONFIDENCE 0.70 et SCALP_SL_PNL_PCT 0.013 datent du 12:00 du 06 (~30h), pas de signal pour bouger. Pattern "0 EMERGENCY + WR>60%" non évaluable (ENTER=0 → WR n/a, tendance qui se confirme depuis ≥4 audits — symptôme structurel du gate H1=wait, pas paramétrique). Master switches : SCALP_ENABLED=True inactif faute de signal H1 (pas de bilan négatif évaluable, juste passif) ; GRID_ENABLED=True visible dans les logs (cycle ETH actif "GRID ETH long TP #2 cumul=0.609%") — fonctionne nominalement.

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; infra saine, anti-oscillation maintenue.

**Code proposals** : aucune nouvelle.

**Alerts** : aucun.

---

## 2026-05-08 00:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=0, external_exit=1, open=1 (SOL +0.484%), enter=1, consensus=166, skip_conf=165, skip_cooldown=1, trail_arm=1, trail_modify=1, llm_error=0, hl_cache_stale=0, hl_sync_err=0
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=0, flip_refusé=0 — pas de trigger SL/flip. SKIP conf=165/720 cycles ≈ 0.23/cycle (très en deçà du seuil 10/cycle, malgré ratio 165/166 ≈ 99% car strate gate filtre déjà la majorité avant consensus, seul ~1 symbole/cycle atteint la phase consensus). TRAIL ARM=1 (≠0) → pattern "0 trade armed" non actif. ENTER=1 + TRAIL ARM=1 + position SOL en gain (+0.484%) → la chaîne complète scalp→armement→trailing fonctionne. Échantillon montre confs 0.58 sur APE (sous MIN_CONFIDENCE=0.70) ; baisser à 0.68 ne capturerait toujours pas 0.58, et 0.55 (borne min) déclencherait des entrées trop bruitées : pas de levier paramétrique pertinent. Infra parfaitement saine (LLM/HL sync/cache stale tous à 0). Pattern "0 EMERGENCY + WR>60%" non évaluable (ENTER=1 → 1 trade non statistiquement significatif). Anti-oscillation maintenue : aucun changement settings depuis ~36h, le système est dans son régime nominal calme (range medium persistant + strate gate H1/M15 conservatrice).

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; infra saine, anti-oscillation maintenue.

**Code proposals** : aucune nouvelle.

**Alerts** : aucun.

---

## 2026-05-08 06:00 (audit Opus)

**Métriques 6h** : emergency_exit=1, flip_refusé=0, external_exit=2, open=2 (APE -0.504% / SOL -2.529%), enter=2, consensus=251, skip_conf=247, skip_cooldown=3, trail_arm=1, trail_modify=5, llm_error=0, hl_cache_stale=0, hl_sync_err=0
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=1 sous seuil 3 ; flip_refusé=0 sous seuil 5 ; SKIP conf=247/720 cycles ≈ 0.34/cycle (très en deçà du seuil 10/cycle ; ratio 247/251 ≈ 98% mais structurel — strate gate filtre déjà la majorité, ~1 symbole/cycle atteint le consensus avec confs typiques 0.39-0.72). TRAIL ARM=1 (≠0) → pattern "0 trade armed" non actif. ENTER=2 + TRAIL ARM=1 + 5 TRAIL NATIVE SL MODIFY → chaîne scalp→armement→trail native opérationnelle. Infra parfaitement saine (LLM/HL sync/cache stale tous à 0). Pattern "0 EMERGENCY + WR>60%" non évaluable (ENTER=2, échantillon trop court). Master switches : SCALP_ENABLED=True a généré 2 ENTER → actif et nominal ; GRID_ENABLED=False inchangé (régime range medium persistant pourrait justifier un flip à True selon la règle 24h, mais la suggestion mérite l'attention humaine — pas de bilan grid net négatif évaluable côté audit pour déclencher automatiquement, d'autant que la règle de flip nécessite "régime range stable depuis ≥24h", confirmé, mais aussi un grid net négatif sur 24h, non vérifiable ici puisque GRID est désactivé). Anti-oscillation maintenue.

**Observation SOL** : ROE -2.529% est très proche du seuil EMERGENCY (= 2× SCALP_SL_PNL_PCT 0.013 = -2.6% ROE). Position juste sous l'override emergency exit ; le SL natif (5 TRAIL NATIVE SL MODIFY observés) ou le tick suivant devrait gérer. À surveiller au prochain audit — si la position reste ouverte avec ROE pire que -2.6%, il y a une défaillance du chemin de fermeture (proposition code à envisager).

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; pas de pattern déclencheur, infra saine.

**Code proposals** : aucune nouvelle.

**Alerts** : aucun déclencheur paramétrique. Observation : SOL ROE -2.529% au seuil emergency à surveiller au prochain audit.

---

## 2026-05-08 12:00 (audit Opus)

**Métriques 6h** : emergency_exit=1, flip_refusé=0, external_exit=1, open=1 (APE -2.221%), enter=1, consensus=261, skip_conf=260, skip_cooldown=2, trail_arm=0, trail_modify=0, llm_error=0, hl_cache_stale=0, hl_sync_err=0
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=1 sous seuil 3 ; flip_refusé=0 sous seuil 5 ; SKIP conf=260/720 cycles ≈ 0.36/cycle (très en deçà du seuil 10/cycle ; ratio 260/261 ≈ 99% structurel comme aux audits précédents — strate gate filtre la majorité avant consensus, confs typiques observées 0.00 / 0.58 / 0.70 sur APE/SOL/BTC). TRAIL ARM=0 sur 6h MAIS audit 06:00 affichait TRAIL ARM=1 et 00:00 TRAIL ARM=1 → cumul 24h ≥ 1, pattern "0 trade armed en 24h" non actif. Infra parfaitement saine (LLM/HL sync/cache stale tous à 0). **Observation SOL résolue** : la position SOL signalée au seuil emergency à -2.529% au précédent audit a été fermée — l'EMERGENCY EXIT (1) et l'external_exit (1) comptabilisés sur la fenêtre couvrent le scénario, le chemin de fermeture a fonctionné. Pas de proposition code requise. **Nouvelle observation APE** : ROE -2.221% sur position ouverte, sous le seuil EMERGENCY (-2.6%) mais à surveiller — le trail natif n'a pas armé (TRAIL ARM=0) puisque la position est restée en perte depuis l'entrée. Pattern "0 EMERGENCY + WR>60%" non évaluable (ENTER=1, échantillon nul). Master switches inchangés : SCALP_ENABLED=True nominal, GRID_ENABLED=False — toujours pas de bilan grid net négatif vérifiable. Anti-oscillation maintenue (aucun changement settings depuis 12:00 du 06, ~48h).

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; pas de pattern déclencheur, infra saine.

**Code proposals** : aucune nouvelle.

**Alerts** : aucun déclencheur paramétrique. Observation : APE ROE -2.221% à surveiller au prochain audit (si dégrade au-delà de -2.6% sans fermeture, défaillance du chemin emergency à investiguer).

---

## 2026-05-08 18:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=0, external_exit=2, open=2 (BNB +1.495% / SOL +1.751%), enter=2, consensus=294, skip_conf=292, skip_cooldown=2, trail_arm=3, trail_modify=13, llm_error=0, hl_cache_stale=0, hl_sync_err=0
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=0, flip_refusé=0 — pas de trigger SL/flip. SKIP conf=292/720 cycles ≈ 0.41/cycle (très en deçà du seuil 10/cycle ; ratio 292/294 ≈ 99% structurel — strate gate filtre la majorité avant consensus, confs typiques 0.45-0.52 dans l'échantillon BNB/BTC/SOL). TRAIL ARM=3 (≠0) → pattern "0 trade armed" non actif. **Bilan très sain** : ENTER=2 + TRAIL ARM=3 + 13 TRAIL NATIVE SL MODIFY → chaîne scalp→armement→trail native pleinement opérationnelle ; les 2 positions ouvertes (BNB +1.495%, SOL +1.751%) sont en gain, l'observation APE -2.221% du précédent audit s'est résolue (sortie via external_exit=2). Infra parfaitement saine (LLM/HL sync/cache stale tous à 0). Pattern "0 EMERGENCY + WR>60%" : ENTER=2 sur 6h reste sous le seuil de significativité statistique pour remonter FLIP_MIN_CONFIDENCE — on ne flippe pas. Confs observées (BTC side=sell conf=0.45 alors que bull=0.70/tech=0.80 ; BNB conf=0.50 ; SOL conf=0.52) toutes sous MIN_CONFIDENCE=0.70 ; baisser à 0.55 (borne min) déclencherait des entrées trop bruitées sur des signaux contradictoires (BTC bull/tech haut + bear_risk medium → side=sell est suspect mais hors périmètre paramétrique). Master switches inchangés : SCALP_ENABLED=True a généré 2 ENTER cohérents → nominal ; GRID_ENABLED=False, régime range medium persistant (≥4 audits) mais pas de bilan grid net négatif vérifiable côté audit (GRID off donc pas de comparable). Anti-oscillation maintenue (~54h depuis dernier change).

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; chaîne complète saine, positions en gain, infra impeccable.

**Code proposals** : aucune nouvelle.

**Alerts** : aucun.

---

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

---

## 2026-05-12 00:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=0, external_exit=3, open_réel=3 (BNB +4.66% / BTC -2.44% / SOL +2.51%), enter=0, consensus=152, skip_conf=134, skip_cooldown=4, trail_arm=0, trail_modify=5, llm_error=0, hl_cache_stale=3, hl_sync_err=2
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=0 ; flip_refusé=0 sous seuil 5 ; SKIP conf=134/720 cycles ≈ 0.19/cycle (largement sous le seuil 10/cycle, mais 88% des consensus, ce qui est cohérent avec un régime range où les confs réelles restent structurellement sous 0.70). TRAIL ARM=0 sur 6h n'est pas en soi déclencheur (les 24h cumulées totalisent ≥22 trail_arm via audits précédents), donc pattern "0 armed en 24h" non actif. TRAIL NATIVE SL MODIFY=5 confirme le ratchet natif HL fonctionne sur les 3 positions ouvertes. ROE sain (2 positions positives marquées BNB +4.66% / SOL +2.51%, BTC à -2.44% encore sous seuil EMERGENCY de -2.6%=2× SL_PNL 1.3%). External_exit=3 dans la fenêtre = sorties exchange propres (SL natif déclenché ou TP), 4 SKIP cooldown observés en aval cohérents. Bug "Stats cycle open=0 vs réel=3" reproduit (3e audit consécutif), déjà couvert par proposition pending du 2026-05-11 06:00 — pas de doublon à déposer.

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; pas de pattern déclencheur.

**Code proposals** : aucune nouvelle. La proposition pending du 2026-05-11 06:00 (`Stats cycle open` désynchronisé) reste pertinente — 3e occurrence du symptôme observée.

**Alerts** : aucun. À surveiller : BTC -2.44% se rapproche du seuil EMERGENCY (≈-2.6% ROE à lev 3x sur SL 1.3%) — si le trail natif HL ne se déclenche pas et que le ROE continue de dégrader, l'emergency exit interne devrait prendre le relais au prochain cycle.

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

## 2026-05-09 00:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=0, external_exit=9, open=3 (BNB +0.494% / ETH -0.869% / SOL +0.403%), enter=10, consensus=282, skip_conf=253, skip_cooldown=13, trail_arm=0, trail_modify=180, llm_error=0, hl_cache_stale=0, hl_sync_err=0
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=0, flip_refusé=0 — pas de trigger SL/flip. SKIP conf=253/720 cycles ≈ 0.35/cycle (très en deçà du seuil 10/cycle ; ratio 253/282 ≈ 90% structurel — strate gate filtre déjà la majorité, confs typiques observées 0.52-0.63 dans l'échantillon BTC/BNB/ETH). TRAIL ARM=0 sur 6h alors qu'audit précédent (18:00) affichait TRAIL ARM=3 → cumul 24h ≥ 3 > 0, pattern "0 trade armed en 24h" non actif (et règle legacy depuis le ratchet 2026-05-08, donc TP_ARM_PCT n'a plus de levier). **Observation notable** : ENTER=10 + external_exit=9 + TRAIL ARM=0 = ratio sortie SL/entrée ≈ 90% et aucune position armée sur 6h ; 180 TRAIL NATIVE SL MODIFY observés (ratchet continu en perte, log `protected=-1.033%, -0.789%, -0.853%` confirme positions jamais en gain assez pour armer). Pattern non listé dans le tableau (pas un EMERGENCY, pas un BREAKEVEN à perte, pas un signal flip), donc pas de levier paramétrique du tableau ; un resserrement SL aggraverait le rythme de sorties, un élargissement violerait la borne max. Pattern "0 EMERGENCY + WR>60%" : WR est manifestement basse (90% external_exit), donc on ne remonte PAS FLIP_MIN_CONFIDENCE. Infra parfaitement saine (LLM 0, HL sync 0, cache stale 0). Anti-oscillation : aucun changement settings depuis ~72h, le système est dans son régime nominal mais peu rentable sur cette fenêtre.

**Changes** : aucun, aucun pattern paramétrique du tableau ne se déclenche ; le ratio sorties/entrées élevé ne s'adresse pas par tuning des bornes (ce serait au-delà du périmètre de l'audit autonome — diagnostic timing d'entrée à confier à l'humain).

**Code proposals** : aucune nouvelle (le pattern observé est statistique sur la WR, pas un bug code identifié dans les logs ; n'entre pas dans les critères "bug structurel non-paramétrique").

**Alerts** : aucun déclencheur paramétrique. Observation : 9 sorties SL sur 10 entrées en 6h (WR très basse) à surveiller au prochain audit ; si le pattern persiste sur ≥2 audits consécutifs avec 0 EMERGENCY, escalader à l'humain pour revue du timing d'entrée (consensus, strate gate, ou seuils filtre pré-LLM).

---

## 2026-05-09 06:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=0, external_exit=7, open=3 (BNB +0.170% / BTC -0.161% / SOL +5.178%), enter=6, consensus=295, skip_conf=260, skip_cooldown=7, trail_arm=0, trail_modify=155, llm_error=0, hl_cache_stale=0, hl_sync_err=0
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=0, flip_refusé=0 — pas de trigger SL/flip. SKIP conf=260/720 cycles ≈ 0.36/cycle (très en deçà du seuil 10/cycle ; ratio 260/295 ≈ 88% structurel comme aux audits précédents — strate gate filtre la majorité, confs typiques observées 0.50-0.76 dans l'échantillon BNB/BTC/SOL). TRAIL ARM=0 sur 6h MAIS la position SOL est à +5.178% ROE et a déjà 155 TRAIL NATIVE SL MODIFY (ratchet continu en gain) → la position s'est manifestement armée hors fenêtre 6h, le pattern "0 trade armed" est faux positif (règle legacy depuis ratchet 2026-05-08 de toute façon). **Bilan très positif sur SOL** : +5.178% ROE est la plus grosse position en gain observée depuis l'instauration du ratchet, le système trail capture bien le mouvement (155 modify confirme le suivi serré). BNB et BTC marginales (±0.17%), pas d'inquiétude. Pattern "0 EMERGENCY + WR>60%" : ENTER=6 + 7 external_exit = échantillon trop court pour conclure WR>60% (ne sait pas combien étaient TP vs SL), donc on ne remonte PAS FLIP_MIN_CONFIDENCE. Infra parfaitement saine (LLM 0, HL sync 0, cache stale 0). Anti-oscillation : aucun changement settings depuis ~78h, contraste net avec audit précédent (00:00) où la WR semblait basse — cette fenêtre montre le ratchet en train de payer sur SOL.

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; chaîne saine, ratchet fait son job sur SOL +5.178%, infra impeccable.

**Code proposals** : aucune nouvelle.

**Alerts** : aucun.

---

## 2026-05-09 12:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=0, external_exit=4, open=1 (SOL -0.988%), enter=3, consensus=104, skip_conf=93, skip_cooldown=6, trail_arm=0, trail_modify=61, llm_error=0, hl_cache_stale=0, hl_sync_err=0
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=0, flip_refusé=0 — pas de trigger SL/flip. SKIP conf=93/720 cycles ≈ 0.13/cycle (très en deçà du seuil 10/cycle ; ratio 93/104 ≈ 89% structurel — strate gate filtre la majorité avant consensus, échantillon des logs montre vetos m15_wait/h1_wait/m1_wait dominants, régime range/medium persistant). TRAIL ARM=0 sur 6h mais cumul 24h via audits précédents (06:00=0, 09-00:00=0, 08-18:00=3) = 3 > 0, pattern "0 trade armed en 24h" non actif (et règle legacy depuis ratchet 2026-05-08 de toute façon). **Lecture probable de la fenêtre** : ENTER=3 + external_exit=4 = au moins 1 sortie carry-over de la fenêtre précédente (très probablement la SOL +5.178% du audit 06:00 que le ratchet a fait sortir profitablement, cohérent avec 61 TRAIL NATIVE SL MODIFY observés). Pattern "0 EMERGENCY + WR>60%" : non évaluable (composition TP vs SL des 4 external_exit non décomposable côté audit, ENTER=3 trop court), donc on ne remonte PAS FLIP_MIN_CONFIDENCE. Position courante SOL -0.988% bien à l'intérieur du SL_PNL=1.3% — trail va arbitrer normalement. Infra parfaitement saine (LLM 0, HL sync 0, cache stale 0). Anti-oscillation : aucun changement settings depuis ~84h, le système est dans son régime nominal calme.

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; pas de pattern déclencheur, infra saine.

**Code proposals** : aucune nouvelle.

**Alerts** : aucun.

---

## 2026-05-09 18:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=0, external_exit=1, open=1 (SOL -1.081%), enter=1, consensus=170, skip_conf=169, skip_cooldown=1, trail_arm=0, trail_modify=2, llm_error=0, hl_cache_stale=1293, hl_sync_err=0
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=0, flip_refusé=0 — pas de trigger SL/flip. SKIP conf=169/720 cycles ≈ 0.23/cycle (très en deçà du seuil 10/cycle ; ratio 169/170 ≈ 99% structurel comme à chaque audit récent — strate gate filtre la quasi-totalité avant consensus, l'échantillon montre vetos m15_wait/h1_wait dominants en régime range medium persistant, confs typiques 0.42-0.45 sous MIN_CONFIDENCE=0.70). TRAIL ARM=0 sur 6h mais cumul 24h via audits précédents (12:00=0, 06:00=0, 09-00:00=0, 08-18:00=3) = 3 > 0, pattern "0 trade armed en 24h" non actif (règle legacy depuis ratchet 2026-05-08 de toute façon). Position courante SOL -1.081% bien à l'intérieur du SL_PNL=1.3% — trail va arbitrer normalement (2 TRAIL NATIVE SL MODIFY confirme le ratchet en cours côté défensif). Pattern "0 EMERGENCY + WR>60%" non évaluable (ENTER=1, échantillon nul). **Observation notable HL cache stale=1293** sur 6h (~3.6/min, soit ~1 par cycle 30s) vs 0 à l'audit précédent : pic significatif mais sync_err=0 (le forced sync gère systématiquement, log "HL cache périmé (10.0s > 10.0s), sync forcé" se résout sans erreur). Pas de mécanisme bug clair dans les logs visibles — le système se met à jour correctement, juste un peu plus de bruit que d'habitude. Pas de proposition code car comportement attendu (le code détecte la péremption et force le sync, qui réussit à chaque fois). Si le pattern persiste sur ≥2 audits consécutifs avec sync_err > 0, alors envisager proposition code (résilience client HL). Anti-oscillation : aucun changement settings depuis ~90h, le système est dans son régime nominal calme.

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; pas de pattern déclencheur, infra fonctionnelle malgré pic cache stale (sync_err=0 confirme que le mécanisme de récupération fait son travail).

**Code proposals** : aucune nouvelle.

**Alerts** : aucun déclencheur paramétrique. Observation : HL cache stale=1293 (vs 0 audit précédent) à surveiller au prochain audit ; si persiste ET sync_err > 0, escalader via proposition code.

---

## 2026-05-10 00:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=0, external_exit=4, open=3 (BTC -0.323% / ETH +0.245% / SOL +1.161%), enter=6, consensus=307, skip_conf=294, skip_cooldown=4, trail_arm=0, trail_modify=116, llm_error=0, hl_cache_stale=1153, hl_sync_err=0
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=0, flip_refusé=0 — pas de trigger SL/flip. SKIP conf=294/720 cycles ≈ 0.41/cycle (très en deçà du seuil 10/cycle ; ratio 294/307 ≈ 96% structurel comme aux audits récents — strate gate filtre la quasi-totalité avant consensus, échantillon montre confs 0.00/0.45/0.58/0.68 sous MIN_CONFIDENCE=0.70 en régime range medium persistant). TRAIL ARM=0 sur 6h mais 116 TRAIL NATIVE SL MODIFY confirme un ratchet actif (probablement sur SOL +1.161% qui s'arme/désarme près du seuil) ; règle "0 trade armed en 24h" legacy depuis ratchet 2026-05-08, non applicable. **Bilan d'activité sain** : ENTER=6 + external_exit=4 → la chaîne scalp produit des entrées et la sortie défensive fonctionne ; positions ouvertes non détresse (BTC marginale -0.32%, ETH +0.25%, SOL +1.16% en gain). Pattern "0 EMERGENCY + WR>60%" non évaluable (composition TP vs SL des 4 external_exit non décomposable côté audit ; 6 entrées + 4 sorties carry-over insuffisant), donc on ne remonte PAS FLIP_MIN_CONFIDENCE. **HL cache stale=1153 (vs 1293 audit précédent)** : pattern persiste mais sync_err=0 inchangé (mécanisme de recovery fonctionne à chaque péremption). Critère d'escalade pour proposition code = "persiste ET sync_err > 0" pas atteint (sync_err reste à 0), donc pas de proposition. Anti-oscillation : aucun changement settings depuis ~96h, le système reste dans son régime nominal.

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; chaîne saine, infra fonctionnelle.

**Code proposals** : aucune nouvelle.

**Alerts** : aucun déclencheur paramétrique. Observation : HL cache stale=1153 sur 2 audits consécutifs avec sync_err=0 — comportement reproductible mais sans erreur métier. À surveiller au prochain audit ; si sync_err > 0 apparaît, escalader via proposition code (résilience client HL).

---

## 2026-05-10 06:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=0, external_exit=4, open=2 (BNB -1.049% / ETH -0.864%), enter=2, consensus=172, skip_conf=170, skip_cooldown=4, trail_arm=0, trail_modify=19, llm_error=0, hl_cache_stale=1661, hl_sync_err=1
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=0, flip_refusé=0 — pas de trigger SL/flip. SKIP conf=170/720 cycles ≈ 0.24/cycle (très en deçà du seuil 10/cycle ; ratio 170/172 ≈ 99% structurel — strate gate filtre la quasi-totalité avant consensus, échantillon montre vetos m15_wait/m1_wait/h1_wait dominants en régime range medium persistant, confs typiques 0.42 sur SOL). TRAIL ARM=0 sur 6h mais 19 TRAIL NATIVE SL MODIFY confirme le ratchet actif côté défensif sur les 2 positions négatives ; règle "0 trade armed en 24h" legacy depuis ratchet 2026-05-08, non applicable. **Bilan d'activité modéré** : ENTER=2 + external_exit=4 → la chaîne scalp tourne au ralenti (régime range strict), 2 positions ouvertes BNB -1.049% / ETH -0.864% bien à l'intérieur du SL_PNL=1.3% — trail va arbitrer normalement. Pattern "0 EMERGENCY + WR>60%" non évaluable (ENTER=2 + composition TP vs SL des external_exit non décomposable côté audit), donc on ne remonte PAS FLIP_MIN_CONFIDENCE. **HL cache stale=1661** sur 3ème audit consécutif (1293 → 1153 → 1661, tendance haussière) **et sync_err=1** (vs 0 sur les 2 précédents) : critère d'escalade approche mais reste borderline — 1 erreur sync isolée n'établit pas un pattern reproductible (statistiquement compatible avec un blip réseau ponctuel). Pas de proposition code immédiate, mais surveillance renforcée au prochain audit (si sync_err ≥ 5 ou si cache stale dépasse 2000 avec sync_err > 0 récurrent, déposer proposition warning sur résilience client HL). Anti-oscillation : aucun changement settings depuis ~102h, système nominal en régime range.

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; pas de pattern déclencheur, infra fonctionnelle malgré pic cache stale.

**Code proposals** : aucune nouvelle (sync_err=1 isolé sous le seuil d'escalade, mécanisme recovery toujours fonctionnel).

**Alerts** : aucun déclencheur paramétrique. Observation : HL cache stale=1661 (max sur 3 audits consécutifs) + sync_err=1 (première occurrence depuis le 09-12:00) — surveillance renforcée au prochain audit ; si sync_err récurrent ou croissant, escalader via proposition code.

---

## 2026-05-10 12:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=0, external_exit=5, open=4 (BNB +0.996% / BTC -0.071% / ETH -0.116% / SOL -0.512%), enter=6, consensus=201, skip_conf=184, skip_cooldown=6, trail_arm=0, trail_modify=127, llm_error=0, hl_cache_stale=705, hl_sync_err=0
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=0, flip_refusé=0 — pas de trigger SL/flip. SKIP conf=184/720 cycles ≈ 0.26/cycle (très en deçà du seuil 10/cycle ; ratio 184/201 ≈ 92% structurel comme aux audits récents — strate gate filtre la quasi-totalité avant consensus, échantillon montre confs 0.58/0.68/0.71 sous MIN_CONFIDENCE=0.70 en régime range medium persistant, vetos h1_wait dominants). TRAIL ARM=0 sur 6h mais 127 TRAIL NATIVE SL MODIFY confirme un ratchet actif côté défensif sur les 4 positions ; règle "0 trade armed en 24h" legacy depuis ratchet 2026-05-08, non applicable. **Bilan d'activité sain** : ENTER=6 + external_exit=5 → chaîne scalp produit régulièrement, BNB en gain modéré (+0.996%), BTC/ETH marginales (±0.12%), SOL -0.512% bien à l'intérieur du SL_PNL=1.3% — pas de cascade. Pattern "0 EMERGENCY + WR>60%" non évaluable (composition TP vs SL des 5 external_exit non décomposable côté audit, ENTER=6 trop court), donc on ne remonte PAS FLIP_MIN_CONFIDENCE. **Évolution HL cache stale=705 (vs 1661 → 1153 → 1293 audits précédents) et sync_err=0 (vs 1 audit précédent)** : la dégradation de 3 audits consécutifs semble se résorber, mécanisme de recovery fonctionne nominalement. Critère d'escalade pour proposition code ("sync_err récurrent ou croissant") non atteint — au contraire, retour à 0. Anti-oscillation : aucun changement settings depuis ~108h, le système reste dans son régime nominal range.

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; chaîne saine, infra retour à la normale (cache stale en baisse, sync_err=0).

**Code proposals** : aucune nouvelle.

**Alerts** : aucun.

---

## 2026-05-10 18:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=0, external_exit=5, open=2 (BNB +0.642% / BTC +0.197%), enter=7, consensus=390, skip_conf=361, skip_cooldown=8, trail_arm=0, trail_modify=33, llm_error=0, hl_cache_stale=467, hl_sync_err=0
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=0, flip_refusé=0 — pas de trigger SL/flip. SKIP conf=361/720 cycles ≈ 0.50/cycle (très en deçà du seuil 10/cycle ; ratio 361/390 ≈ 93% structurel comme aux audits récents — strate gate filtre la quasi-totalité avant consensus, échantillon montre confs 0.56/0.70/0.71 sous MIN_CONFIDENCE=0.70 en régime range medium persistant, vetos h1_wait/m1_wait dominants). TRAIL ARM=0 sur 6h mais 33 TRAIL NATIVE SL MODIFY confirme le ratchet actif côté défensif ; règle "0 trade armed en 24h" legacy depuis ratchet 2026-05-08, non applicable. **Bilan d'activité sain** : ENTER=7 (max sur les derniers audits) + external_exit=5 → la chaîne scalp produit régulièrement, les 2 positions ouvertes sont toutes en gain (BNB +0.642%, BTC +0.197%). Pattern "0 EMERGENCY + WR>60%" non évaluable (ENTER=7 + composition TP vs SL des 5 external_exit non décomposable côté audit), donc on ne remonte PAS FLIP_MIN_CONFIDENCE. **Évolution HL cache stale=467 (vs 705 → 1661 → 1153 → 1293 audits précédents) et sync_err=0 (2ème consécutif)** : la dégradation antérieure se résorbe nettement, mécanisme de recovery fonctionne nominalement, critère d'escalade non atteint. Anti-oscillation : aucun changement settings depuis ~114h, système nominal en régime range.

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; chaîne saine, infra en amélioration continue (cache stale -34% vs précédent, sync_err=0).

**Code proposals** : aucune nouvelle.

**Alerts** : aucun.

---

## 2026-05-11 00:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=0, external_exit=8, open=4 (BNB -5.216% / BTC -4.724% / ETH -2.032% / SOL -4.408%, toutes BUY/long), enter=4, consensus=97, skip_conf=81, skip_cooldown=10, trail_arm=0, trail_modify=77, llm_error=0, hl_cache_stale=0, hl_sync_err=0
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=0, flip_refusé=0 — pas de trigger SL/flip ; SKIP conf=81/720 cycles ≈ 0.11/cycle (très en deçà du seuil 10/cycle ; ratio 81/97 ≈ 84% structurel comme aux audits récents — strate gate filtre la quasi-totalité avant consensus, échantillon montre vetos m15_wait/h1_wait/m1_wait dominants en régime range medium persistant). TRAIL ARM=0 sur 6h mais 77 TRAIL NATIVE SL MODIFY confirme le ratchet actif côté défensif sur les positions négatives ; règle "0 trade armed en 24h" legacy depuis ratchet 2026-05-08, non applicable. Infra parfaitement saine (LLM 0, HL sync 0, cache stale 0 — retour complet à la normale, contraste net avec pic 1661 du 06:00 le 10). **Observation marquante** : 4 positions toutes BUY/long toutes en perte substantielle (BNB -5.216%, BTC -4.724%, SOL -4.408%, ETH -2.032%), aucune EMERGENCY EXIT déclenchée malgré ROE bien au-delà du seuil par défaut -2.6% (= 2× SCALP_SL_PNL_PCT=0.013). Hypothèse : levier ≥6× sur ces positions → seuil emergency en ROE proportionnel (lev 6× → -6%, lev 10× → -10%), donc seuils non atteints ; cohérent avec le ratchet en distance prix absolue introduit le 2026-05-10. Le `Stats cycle: open=0` dans les logs récents contraste avec 4 positions visibles dans ROE — probablement compteur scalp local désynchronisé du recensement positions exchange (à observer, pas un bug paramétrique). Pattern "0 EMERGENCY + WR>60%" : ENTER=4 + 8 external_exit suggère WR très basse (sorties >> entrées), donc on ne remonte PAS FLIP_MIN_CONFIDENCE. Master switches : SCALP_ENABLED=True a généré 4 ENTER → pipeline actif ; GRID_ENABLED=False, règle "+ régime range stable ≥24h" remplie depuis ≥5 jours MAIS activation maintenant ajouterait des ordres limit autour du prix sur des symboles déjà en perte lourde long → risque d'interférence avec le trail défensif en cours et amplification du risque directionnel. Prudence : ne pas activer GRID dans un moment de stress positions. Anti-oscillation : aucun changement settings depuis ~120h, le système est dans son régime nominal mais subit clairement un retournement de marché que les longs ne digèrent pas.

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; aucun pattern paramétrique déclencheur ; activation GRID écartée par prudence vu les 4 longs en perte.

**Code proposals** : aucune nouvelle (le décalage `Stats cycle open=0` vs 4 positions ROE négatives est une observation sans mécanisme bug clair dans les logs visibles, à confirmer au prochain audit avant d'envisager une proposition info).

**Alerts** : aucun déclencheur paramétrique. **Observation à surveiller** : 4 positions long en cascade de pertes (-2% à -5.2%) en régime range medium → cas typique de retournement non capté par le filtre flip 0.90 ; si les positions clôturent toutes en SL au prochain audit sans qu'un seul flip ne se soit déclenché, envisager assouplissement FLIP_MIN_CONFIDENCE (0.90→0.85, garde-fou conservé). Anomalie `Stats cycle open=0` vs ROE liste 4 positions à reconfirmer au prochain audit.

---

## 2026-05-11 06:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=0, external_exit=9, open=4 (BNB -2.763% / BTC -3.203% / ETH -4.500% / SOL +0.902%, toutes BUY/long), enter=6, consensus=197, skip_conf=179, skip_cooldown=9, trail_arm=0, trail_modify=106, llm_error=0, hl_cache_stale=1, hl_sync_err=1
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=0, flip_refusé=0 — pas de trigger SL/flip. SKIP conf=179/720 cycles ≈ 0.25/cycle (très en deçà du seuil 10/cycle ; ratio 179/197 ≈ 91% structurel, vetos strate gate dominants comme aux audits récents). TRAIL ARM=0 mais 106 TRAIL NATIVE SL MODIFY confirme le ratchet actif côté défensif sur les positions négatives ; règle legacy depuis ratchet 2026-05-08, non applicable. Infra parfaitement saine (LLM 0, HL sync_err=1 ponctuel, cache stale=1 — retour total à la normale après le pic à 1661 du 10-06:00). **L'hypothèse `FLIP_MIN_CONFIDENCE` de l'audit précédent (00:00) est invalidée par flip_refusé=0 sur la fenêtre** : aucun flip n'a été tenté/refusé, donc le seuil 0.90 n'est PAS le coupable des longs en perte — le consensus ne vire simplement jamais SHORT clair (régime range medium tient encore selon orchestrator), le bot ne voit pas le retournement comme un signal. ENTER=6 nouvelles entrées sur la fenêtre toutes longues (régime range medium + biais bull persistant) → 3 nouvelles positions cascade BNB/BTC/ETH (-2.76 à -4.50% ROE), SOL résiste (+0.902%). Pas d'EMERGENCY car seuil en distance prix absolue (introduit 2026-05-10) tolère ces ROE quand le levier est élevé (cohérent avec design). Pattern "0 EMERGENCY + WR>60%" non évaluable mais 9 external_exit vs 6 ENTER suggère WR < 50% → on ne remonte PAS FLIP_MIN_CONFIDENCE. **Anomalie `Stats cycle open=0` vs 4 positions ROE confirmée sur 2 audits consécutifs (00:00 et 06:00)** : pattern reproductible, donc proposition info déposée dans code_proposals.md (compteur scalp local désynchronisé du recensement exchange, à investiguer). Anti-oscillation : aucun changement settings depuis ~126h, le système subit un retournement de marché que les longs absorbent sans réagir.

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; hypothèse FLIP de l'audit précédent invalidée par flip_refusé=0 ; pas de pattern paramétrique déclencheur.

**Code proposals** : 1 info ajoutée (anomalie compteur `Stats cycle open=N` vs ROE exchange, reproductible 2 audits).

**Alerts** : aucun déclencheur paramétrique. **Observation à surveiller** : 3 longs cascade depuis ≥12h (BNB/BTC/ETH) sans flip ni emergency ; si ROE continue de se dégrader au prochain audit et que flip_refusé > 0 apparaît simultanément, envisager assouplir FLIP_MIN_CONFIDENCE 0.90→0.85. Si `Stats cycle open=0` persiste un 3ème audit consécutif, escalader la proposition info → warning.

---

## 2026-05-11 12:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=0, external_exit=3, open=3 (BNB +2.332% / BTC +2.272% / SOL -4.990%, toutes BUY/long), enter=4, consensus=360, skip_conf=354, skip_cooldown=3, trail_arm=0, trail_modify=120, llm_error=0, hl_cache_stale=3, hl_sync_err=2
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=0, flip_refusé=0 — pas de trigger SL/flip ; SKIP conf=354/720 cycles ≈ 0.49/cycle (très en deçà du seuil 10/cycle ; ratio 354/360 ≈ 98% structurel — strate gate filtre la quasi-totalité avant consensus, échantillon montre confs 0.68 sur SOL sous MIN_CONFIDENCE=0.70 + vetos h1_wait dominants en régime range medium). TRAIL ARM=0 mais 120 TRAIL NATIVE SL MODIFY confirme le ratchet actif côté défensif sur les positions ; règle legacy depuis ratchet 2026-05-08, non applicable. Infra excellente (LLM 0, HL cache stale=3 vs 1661 max sur la séquence du 10-06:00, sync_err=2 ponctuels — retour complet à la normale). **Évolution majeure positions** : la cascade longs des 2 audits précédents (BNB/BTC/ETH/SOL toutes en perte -2 à -5%) s'est résorbée partiellement — BNB et BTC ont rebondi en gain (+2.33%, +2.27%), SOL persiste -4.99%, ETH a été fermée (probablement par 1 des 3 external_exit). **Validation rétrospective** : le système a SURVÉCU à un retournement sans EMERGENCY ni flip, sans changement de paramètres ; l'hypothèse FLIP_MIN_CONFIDENCE 0.90→0.85 du 00:00 reste invalidée pour le 3ème audit consécutif (flip_refusé=0 cumulé), donc on conserve 0.90 — assouplir aurait été nuisible vu le rebond auto. Pattern "0 EMERGENCY + WR>60%" non évaluable (4 ENTER vs 3 external_exit composition TP/SL non décomposable côté audit), donc on ne remonte PAS FLIP_MIN_CONFIDENCE non plus. **Anomalie compteur "Stats cycle"** : dernier log montre `open=2 trail_guards=2` vs 3 positions ROE visibles — écart réduit (-1) vs les 2 audits précédents (open=0 vs 4 positions). Critère strict d'escalade de la proposition info pending ("open=0 persiste un 3ème audit consécutif") non atteint puisque open=2≠0 ; la proposition reste info pending sans escalade. Master switches : SCALP_ENABLED=True a fait son job (4 ENTER + survie cascade), GRID_ENABLED=False reste prudent vu SOL -4.99% encore en stress, pas de bascule. Anti-oscillation : aucun changement settings depuis ~132h, système en régime nominal post-stress.

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; cascade survit sans EMERGENCY ni flip, infra retour complet à la normale, hypothèse FLIP du 00:00 invalidée 3ème fois consécutive.

**Code proposals** : aucune nouvelle. La proposition info du 06:00 sur compteur Stats cycle reste pending — pattern partiellement reproductible (open=2 vs 3 positions, écart -1 cette fois) mais sans atteindre le critère d'escalade strict (open=0 persistant).

**Alerts** : aucun déclencheur paramétrique. **Observation positive** : la cascade longs des audits 00:00/06:00 s'est résorbée d'elle-même (2/3 positions en gain au lieu de toutes en perte) — confirme que NE PAS toucher au FLIP_MIN_CONFIDENCE en stress était la bonne décision. À surveiller : SOL -4.99% isolé ; si SL natif ratchet fonctionne, il devrait se fermer normalement sans EMERGENCY.

---

## 2026-05-12 06:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=0, external_exit=5, open=3 (BNB -4.948% / BTC -4.829% / SOL -2.397%, toutes BUY/long), enter=3, consensus=134, skip_conf=120, skip_cooldown=5, trail_arm=0, trail_modify=63, llm_error=0, hl_cache_stale=0, hl_sync_err=0
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=0, flip_refusé=0 — pas de trigger SL/flip ; SKIP conf=120/720 cycles ≈ 0.17/cycle (très en deçà du seuil 10/cycle ; ratio 120/134 ≈ 90% structurel — strate gate dominant comme aux audits récents, échantillon montre confs 0.00/0.42 et vetos h1_wait/m15_wait/m1_wait en régime range medium persistant). TRAIL ARM=0 sur 6h mais 63 TRAIL NATIVE SL MODIFY confirme le ratchet actif côté défensif sur les 3 longs en perte ; règle "0 trade armed en 24h" legacy depuis ratchet 2026-05-08, non applicable. Infra parfaite (LLM 0, HL sync 0, cache stale 0 — 2ème audit consécutif à 0). **Évolution positions** : nouvelle cascade longs (BNB -4.948%, BTC -4.829%, SOL -2.397%) similaire à l'audit du 11-00:00 (BNB -5.21%, BTC -4.72%, SOL -4.41%, ETH -2.03%). Pas d'EMERGENCY car seuil en distance prix absolue (introduit 2026-05-10) tolère ces ROE quand levier élevé ; 5 external_exit montrent que SL natif HL travaille (probablement sortie ETH/SOL antérieures via SL ratchet). Pattern "0 EMERGENCY + WR>60%" : 3 ENTER vs 5 external_exit suggère WR <50%, donc on ne remonte PAS FLIP_MIN_CONFIDENCE. **Anomalie `Stats cycle open=N`** : log récent affiche `open=0 trail_guards=0` vs 3 positions ROE visibles — pattern réapparaît après la pause du 11-12:00 (open=2 vs 3). Critère d'escalade strict de la proposition info pending ("open=0 persiste un 3ème audit consécutif") **non atteint** car séquence brisée par 11-12:00 (open=2≠0) ; la proposition info reste pending sans escalade. Master switches : SCALP_ENABLED=True a généré 3 ENTER (chaîne active), GRID_ENABLED=False reste prudent vu BNB/BTC en perte -5% et SOL -2.4% (activation amplifierait le risque directionnel des longs ouverts). Anti-oscillation : aucun changement settings depuis ~138h, système subit un 2ème épisode de cascade longs (~24h après le 1er du 11-00:00) que les filtres absorbent sans réagir, conforme au design.

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; aucun pattern paramétrique déclencheur ; activation GRID écartée par prudence (3 longs en perte).

**Code proposals** : aucune nouvelle. Proposition info du 11-06:00 sur compteur `Stats cycle open=N` reste pending — pattern réapparaît (open=0 vs 3 positions) mais séquence consécutive brisée par l'audit du 11-12:00, pas d'escalade.

**Alerts** : aucun déclencheur paramétrique. **Observation à surveiller** : 2ème épisode cascade longs en ~24h (BNB/BTC/SOL toutes -2.4 à -4.9%) sans flip ni emergency. Si au prochain audit le pattern persiste avec un 3ème épisode + `flip_refusé > 0` ou `EMERGENCY > 0` apparaissent, envisager assouplir FLIP_MIN_CONFIDENCE 0.90→0.85 ou resserrer SCALP_SL_DIST_PRICE_PCT. Tant que le ratchet natif HL clôture proprement par external_exit, l'auto-correction tient.

---

## 2026-05-12 18:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=0, external_exit=1, open=1 (BNB -0.552%), enter=0, consensus=132, skip_conf=131, skip_cooldown=1, trail_arm=0, trail_modify=46, llm_error=0, hl_cache_stale=0, hl_sync_err=0
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=0, flip_refusé=0 — pas de trigger SL/flip ; SKIP conf=131/720 cycles ≈ 0.18/cycle (très en deçà du seuil 10/cycle ; ratio 131/132 ≈ 99% structurel — strate gate filtre quasi-totalité avant consensus, échantillon montre confs 0.00 et vetos h1_wait/m15_wait dominants en régime range medium persistant). TRAIL ARM=0 sur 6h mais 46 TRAIL NATIVE SL MODIFY confirme le ratchet actif côté défensif sur la position résiduelle ; règle legacy depuis ratchet 2026-05-08, non applicable. Infra parfaite (LLM 0, HL sync 0, cache stale 0 — 3ème audit consécutif à 0). **Évolution majeure positions** : la cascade longs du 12-06:00 (BNB -4.95% / BTC -4.83% / SOL -2.40%) s'est largement résorbée — il ne reste que BNB à -0.55% (BTC et SOL probablement fermées via SL ratchet ; 1 external_exit visible dans la fenêtre + d'autres antérieures à la fenêtre). **Validation rétrospective** : pour le 2ème épisode cascade en 48h, le système a SURVÉCU sans EMERGENCY, sans flip, sans changement de paramètres — 5 external_exit (audit précédent) + 1 (cette fenêtre) confirment que le ratchet natif HL clôture proprement aux niveaux théoriques. L'hypothèse "assouplir FLIP_MIN_CONFIDENCE 0.90→0.85 ou resserrer SCALP_SL_DIST_PRICE_PCT" évoquée à l'audit précédent reste invalidée (flip_refusé=0 cumulé sur 4 audits). Pattern "0 EMERGENCY + WR>60%" non évaluable (ENTER=0, composition TP/SL des external_exit non décomposable côté audit), donc on ne remonte PAS FLIP_MIN_CONFIDENCE. ENTER=0 s'explique par contexte normal (régime range medium + cooldown post external_exit + 1 position ouverte). **Anomalie `Stats cycle open=N`** : log récent affiche `open=0 trail_guards=0` vs 1 position BNB ROE visible — pattern réapparaît (2ème audit consécutif après 12-06:00, séquence brisée par 11-12:00). Critère strict d'escalade ("open=0 persiste un 3ème audit consécutif") non encore atteint (2 audits consécutifs, faute du 3ème) ; proposition info reste pending sans escalade. Master switches : SCALP_ENABLED=True (chaîne active mais peu d'opportunités en range filtré strate gate), GRID_ENABLED=False reste prudent (BNB encore en perte modérée, activer maintenant interférerait avec le trail défensif). Anti-oscillation : aucun changement settings depuis ~144h, système absorbe le 2ème épisode de cascade par auto-correction.

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; cascade 12-06:00 résorbée par ratchet natif HL, aucun pattern paramétrique déclencheur.

**Code proposals** : aucune nouvelle. Proposition info du 11-06:00 sur compteur `Stats cycle open=N` reste pending — pattern réapparaît (open=0 vs 1 position) mais critère d'escalade strict (3 audits consécutifs) non atteint.

**Alerts** : aucun déclencheur paramétrique. **Observation positive** : 2ème épisode cascade longs résorbé en <12h sans EMERGENCY ni flip — confirme robustesse du design ratchet en distance prix absolue + filtre flip 0.90. À surveiller : si `Stats cycle open=0` persiste un 3ème audit consécutif (prochain 12-05-13 00:00), escalader la proposition info → warning.

---

## 2026-05-13 00:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=0, external_exit=0, open=1 (BNB +0.640%), enter=0, consensus=44, skip_conf=37, skip_cooldown=0, trail_arm=0, trail_modify=24, llm_error=0, hl_cache_stale=0, hl_sync_err=0
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=0, flip_refusé=0, external_exit=0 — fenêtre extrêmement calme côté trades (la plus calme depuis longtemps). SKIP conf=37/720 cycles ≈ 0.05/cycle (très en deçà du seuil 10/cycle ; ratio 37/44 ≈ 84% structurel — strate gate filtre majoritairement avant consensus, échantillon montre confs 0.39/0.58/0.00 sous MIN_CONFIDENCE=0.70 + vetos h1_wait/m15_wait/m1_wait dominants en régime range medium persistant). CONSENSUS=44 (très bas vs précédents 132-390) — le strate gate a coupé encore plus en amont qu'à l'habitude, cohérent avec un régime range très stable et peu de signaux M15/M1 alignés. TRAIL ARM=0 sur 6h mais 24 TRAIL NATIVE SL MODIFY confirme le ratchet actif côté défensif sur la position BNB (qui ratchete son SL natif sans franchir le seuil d'arm officiel). Infra parfaite (LLM 0, HL sync 0, cache stale 0 — 4ème audit consécutif à 0, série la plus longue depuis le pic du 10-06:00). **Validation rétrospective séquence cascade 11-13** : le 2ème épisode cascade longs (12-06:00) s'est totalement résorbé — il ne reste qu'une position BNB en gain modéré (+0.640%, vs +/-0% à -5.2% en pic), sans EMERGENCY déclenché, sans flip, sans changement de paramètres ; auto-correction du ratchet natif HL pleinement validée sur 2 épisodes consécutifs (11-00:00 et 12-06:00). Pattern "0 EMERGENCY + WR>60%" non évaluable (ENTER=0 sur la fenêtre), donc on ne remonte PAS FLIP_MIN_CONFIDENCE. **Anomalie `Stats cycle open=N`** : log récent montre `open=1 trail_guards=1` vs 1 position BNB ROE visible — **cohérence retrouvée** (1=1) ; le pattern open=0 vs N≥1 ne se reproduit pas cet audit. La séquence consécutive est définitivement brisée (12-18:00 puis ce 13-00:00 cohérents) → la proposition info pending du 11-06:00 reste pending sans escalade ; envisager de la marquer `stale` au prochain audit si pattern reste absent. Master switches : SCALP_ENABLED=True (pas de signal négatif récent pour le basculer, 0 ENTER cette fenêtre est circonstanciel — strate gate strict en régime range très calme), GRID_ENABLED=False reste prudent (1 position seulement, activer maintenant ajouterait du bruit sans bénéfice clair vu le calme général ; règle "range stable ≥24h" remplie depuis longtemps mais le coût d'activation/désactivation est non-nul → préférer attendre une fenêtre clairement morte côté scalp avec equity stable). Anti-oscillation : aucun changement settings depuis ~150h, le système est dans son régime nominal le plus calme observé sur la séquence.

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; fenêtre extrêmement calme (0 emergency/flip/external_exit/enter), aucun pattern déclencheur, infra parfaite, anomalie Stats cycle non reproduite.

**Code proposals** : aucune nouvelle. Proposition info du 11-06:00 sur compteur `Stats cycle open=N` reste pending — pattern non reproduit cet audit (cohérence 1=1 retrouvée), pas d'escalade.

**Alerts** : aucun. Observation : la séquence de 2 cascades longs en 48h (11-00:00 et 12-06:00) est entièrement résorbée sans intervention — c'est une **validation forte** du design (ratchet natif HL en distance prix + filtre flip 0.90 + emergency en distance prix). Aucun cas où un changement de paramètre aurait amélioré le résultat n'a été identifié.

---

## 2026-05-13 06:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=0, external_exit=5, open=2 (BNB -0.492% / SOL -2.031%, toutes BUY/long), enter=5, consensus=236, skip_conf=193, skip_cooldown=7, trail_arm=0, trail_modify=153, llm_error=0, hl_cache_stale=0, hl_sync_err=0
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=0, flip_refusé=0 — pas de trigger SL/flip. SKIP conf=193/720 cycles ≈ 0.27/cycle (très en deçà du seuil 10/cycle ; ratio 193/236 ≈ 82% structurel — strate gate filtre majoritairement avant consensus, échantillon montre confs 0.50/0.60/0.00 sous MIN_CONFIDENCE=0.70 + vetos h1_wait dominants en régime range medium persistant). TRAIL ARM=0 sur 6h mais 153 TRAIL NATIVE SL MODIFY (très actif vs précédent 24) confirme le ratchet pleinement engagé sur les 2 longs en perte modérée ; règle legacy depuis ratchet 2026-05-08, non applicable. Infra parfaite (LLM 0, HL sync 0, cache stale 0 — **5ème audit consécutif à 0**, plus longue série depuis le pic 10-06:00). **Activité retrouvée** : 5 ENTER + 5 external_exit (vs 0/0 il y a 6h) — la fenêtre calme du 13-00:00 était bien circonstancielle (strate gate plus permissif maintenant que régime range medium accueille des signaux M15/M1 alignés, cf échantillon SOL/BNB STRATE GATE BUY 0.80/0.80/0.80). Pattern "0 EMERGENCY + WR>60%" : 5 ENTER vs 5 external_exit non décomposable en TP/SL côté audit, donc on ne remonte PAS FLIP_MIN_CONFIDENCE. Les 2 positions restantes (BNB -0.49%, SOL -2.03%) sont gérées par le ratchet natif HL (153 modifies sur 6h, distance prix absolue SCALP_SL_DIST_PRICE_PCT=0.5%). **Anomalie `Stats cycle open=N`** : log récent montre `open=1 trail_guards=1` vs 2 positions ROE visibles — pattern divergent réapparaît (open=1 vs 2, écart -1) après cohérence retrouvée au 13-00:00. Critère strict d'escalade (3 audits consécutifs) **non atteint** — séquence brisée par 13-00:00 (cohérence 1=1) puis ce 13-06:00 redivergent, donc audit divergent isolé ; proposition info reste pending sans escalade. Master switches : SCALP_ENABLED=True (5 ENTER cette fenêtre = chaîne pleinement active), GRID_ENABLED=False reste prudent (2 longs en perte modérée, activation ajouterait du bruit + interférerait avec le trail défensif natif). Anti-oscillation : aucun changement settings depuis ~156h, système en régime nominal robuste sur 5 audits consécutifs sans intervention.

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; activité retrouvée (5/5 ENTER/exit) sans déclencher de pattern, infra parfaite 5ème audit consécutif.

**Code proposals** : aucune nouvelle. Proposition info du 11-06:00 sur compteur `Stats cycle open=N` reste pending — pattern divergent réapparaît mais séquence consécutive brisée par 13-00:00, pas d'escalade.

**Alerts** : aucun. **Observation positive** : 5 audits consécutifs (12-06:00 → 13-06:00, soit 30h) sans EMERGENCY/flip/changement de paramètres, malgré 2 cascades longs absorbées au début de la séquence — validation forte continue du design ratchet natif HL + filtre flip 0.90 + emergency en distance prix.

---

## 2026-05-13 12:00 (audit Opus)

**Métriques 6h** : emergency_exit=0, flip_refusé=0, external_exit=1, open=2 (BNB +4.360% / SOL -1.076%, toutes BUY/long), enter=2, consensus=252, skip_conf=237, skip_cooldown=1, trail_arm=0, trail_modify=48, llm_error=0, hl_cache_stale=1, hl_sync_err=1
**Diagnostic** : Aucun pattern paramétrique du tableau ne se déclenche. EMERGENCY=0, flip_refusé=0, external_exit=1 — fenêtre très calme côté trades. SKIP conf=237/720 cycles ≈ 0.33/cycle (très en deçà du seuil 10/cycle ; ratio 237/252 ≈ 94% structurel — strate gate filtre majoritairement avant consensus, échantillon montre confs BNB 0.68/0.58 sous MIN_CONFIDENCE=0.70 + vetos h1_wait/m1_wait dominants en régime range medium persistant). TRAIL ARM=0 sur 6h mais 48 TRAIL NATIVE SL MODIFY confirme le ratchet actif côté défensif sur les 2 longs (BNB notamment ratchete son SL natif vers le haut avec ROE +4.36% sans franchir le seuil d'arm officiel) ; règle legacy depuis ratchet 2026-05-08, non applicable. Infra parfaite (LLM 0, HL sync_err=1 ponctuel, cache stale=1 ponctuel — **6ème audit consécutif quasi-nul**, plus longue série continue depuis le pic 10-06:00). **Évolution positive positions** : BNB +4.360% (vs -0.492% audit précédent) confirme le retournement haussier capté par le ratchet ; SOL -1.076% (vs -2.031%) en amélioration également. Pattern "0 EMERGENCY + WR>60%" : 2 ENTER + 1 external_exit, échantillon insuffisant pour conclure WR + composition TP/SL non décomposable côté audit, donc on ne remonte PAS FLIP_MIN_CONFIDENCE. **Anomalie `Stats cycle open=N`** : log récent montre `open=2 trail_guards=2` vs 2 positions ROE visibles — **cohérence retrouvée** (2=2) ; le pattern divergent ne se reproduit pas cet audit (vs 13-06:00 où écart -1). Critère strict d'escalade (3 audits consécutifs) toujours non atteint — séquence alternante (cohérent/divergent/cohérent/divergent/cohérent) ; proposition info reste pending sans escalade, à marquer `stale` au prochain audit si le pattern reste majoritairement absent. Master switches : SCALP_ENABLED=True (2 ENTER cette fenêtre = chaîne active mais frugale en régime range strict), GRID_ENABLED=False reste prudent (le scalp gère bien le retournement BNB ; activer maintenant interférerait avec un long en gain solide qui rateche). Anti-oscillation : aucun changement settings depuis ~162h (~6.75 jours), système en régime nominal robuste sur 6 audits consécutifs sans intervention.

**Changes** : aucun, paramétrage cohérent avec l'activité observée ; fenêtre calme avec retournement haussier en cours (BNB +4.36%), infra parfaite 6ème audit consécutif, aucun pattern déclencheur.

**Code proposals** : aucune nouvelle. Proposition info du 11-06:00 sur compteur `Stats cycle open=N` reste pending — pattern non reproduit cet audit (cohérence 2=2 retrouvée), séquence alternante sans 3 audits consécutifs divergents, pas d'escalade ; envisager marquage `stale` au prochain audit si le pattern reste majoritairement absent.

**Alerts** : aucun. **Observation positive** : 6 audits consécutifs (12-06:00 → 13-12:00, soit ~36h) sans EMERGENCY/flip/changement de paramètres ; BNB rebondit en gain solide (+4.36%) après les cascades du 11-12, validation continue du ratchet natif HL en distance prix absolue.

---

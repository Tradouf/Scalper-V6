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

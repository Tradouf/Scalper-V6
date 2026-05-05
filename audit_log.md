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

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

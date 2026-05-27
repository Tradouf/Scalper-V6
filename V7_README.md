# SalleDesMarches V7 — Allocation dynamique multi-stratégies

**Branche** : `v7-allocation`
**Statut** : MVP en développement (Phase P-1, démarrée 2026-05-27)
**V6 prod** : `~/SalleDesMarches_fixed/` (main), reste live pendant tout le développement V7

---

## Architecture cible

Refonte du bot vers une couche d'allocation dynamique qui pondère les stratégies selon le régime de marché détecté et la performance récente.

**Stratégies MVP** (3, déterministes, aucun LLM) :
- **Grid** (portage V6.2) — performant en RANGE
- **Mean Reversion** (portage V6.2) — performant en RANGE
- **Momentum** (from scratch) — performant en TREND

**Couches** :
1. Détecteur de régime probabiliste (ADX/Hurst/vol/autocorr → softmax 4 régimes)
2. Agents de stratégie (Protocol `StrategyAgent`)
3. Allocateur (matrice B × score performance × vol-targeting)
4. Risk Manager (caps + kill-switch DD)
5. Execution Engine (reconcile + bande non-trade)
6. Backtester walk-forward (coûts HL réels)
7. Monitoring + audit Opus 6h adapté V7

## Plan par phases

| Phase | Contenu | Statut |
|---|---|---|
| P-1 | Préparation : worktree, backfill historique HL, inventaire V6 | en cours |
| P0 | Fondations : core/types, interfaces, pydantic config, pytest | pending |
| P1 | Détecteur régime probabiliste | pending |
| P2a | Grid → StrategyAgent | pending |
| P2b | MR → StrategyAgent | pending |
| P2c | Momentum from scratch | pending |
| P3 | Allocateur + Performance scoring | pending |
| P4 | Risk Manager | pending |
| P5 | Backtester walk-forward (go/no-go) | pending |
| P6 | Execution Engine + Paper | pending |
| P7 | Paper parallèle 7j vs V6 live | pending |
| P8 | Cutover live (merge dans main) | pending |
| P10 | Monitoring + audit Opus V7 | pending |

## Pendant le développement

- **V6 prod tourne sur `~/SalleDesMarches_fixed/`** branche `main`. Continue de tourner sans interruption.
- **V7 dev sur `~/SalleDesMarches_v7/`** branche `v7-allocation`. Code nouveau, paper trading uniquement.
- **Crons audit Opus 6h** : restent pointés sur V6 jusqu'au cutover.
- **systemd sdm-orderflow.service** : alimente `~/SalleDesMarches_fixed/memory/orderflow.db`, V7 peut lire en read-only.
- **Dashboard V7 paper** : port 8082 (V6 reste sur 8081).

## Au cutover (P8)

Stop V6 → merge `v7-allocation` dans `main` → restart bot depuis le même path V6.
Le worktree V7 sera supprimé après merge réussi (les fichiers V7 deviendront le `main` officiel).

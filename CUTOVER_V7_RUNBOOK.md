# Cutover V6 → V7 — Runbook

## Pre-checks (automatique)

```bash
cd ~/SalleDesMarches_v7
bash scripts/cutover_precheck.sh
```

Exit 0 + `=== PRE-CHECK OK ===` requis avant tout cutover. Le script vérifie :

- pytest 174/174
- branche `v7-allocation`, code source clean
- composants P8 présents (`hyperliquid_write_adapter`, `boot_reconciler`, `emergency_exit`)
- `paper_mode` actuel dans `config/allocation.yaml`
- `.env` + `HL_ACCOUNT_ADDRESS`
- V6 prod up/down, V7 up/down
- `order_registry.json`, OHLC accumulé

## Séquence de cutover

### 1. Stop V6 prod

```bash
cd ~/SalleDesMarches_fixed
bash start_sdm.sh stop   # ou : kill -15 $(cat sdm.pid)
```

Décision : laisser les positions ouvertes côté HL ou les fermer manuellement avant.
**Le BootReconciler V7 récupère les positions HL au start** — donc laisser ouvertes
= V7 les hérite. Fermer = V7 part neutre.

### 2. Basculer V7 en live

```bash
cd ~/SalleDesMarches_v7
sed -i 's/paper_mode: true/paper_mode: false/' config/allocation.yaml
```

### 3. Démarrer V7

```bash
bash scripts/bot.sh restart
```

### 4. Surveillance immédiate

```bash
bash scripts/bot.sh logs
# Repérer dans l'ordre :
#   "V7 config chargée. ... paper=False"
#   "Mode LIVE : HyperliquidWriteAdapter instancié — vrais ordres HL."
#   "BootReconciler résumé: positions=N equity=$X.XX orders=M ghosts=G orphans=O errors=E"
#   "V7 boucle démarrée (interval=30s)"
#   premier "tick #1 regime=... orders=... fills=..."
```

Surveillance les 30 premières minutes :
- Le tick #1 ne doit pas générer d'ordres erratiques (BootReconciler doit avoir bien sync).
- `equity` ≈ ce que V6 affichait avant stop (compte unifié, spot USDC).
- `EmergencyExit` doit logger en silence (positions héritées probablement pas en zone -2.2%).
- Aucun `EMERGENCY EXIT (orphan)` en cascade au start (si oui → bug Fix #8 grâce ou BootReconciler manqué).

## Rollback (si KO)

```bash
# 1. Stop V7
cd ~/SalleDesMarches_v7
bash scripts/bot.sh stop

# 2. Revenir paper
sed -i 's/paper_mode: false/paper_mode: true/' config/allocation.yaml

# 3. Restart V6 prod
cd ~/SalleDesMarches_fixed
bash start_sdm.sh
```

V6 reprendra les positions HL (son code de reconcile était déjà robuste avant cutover).

## Post-cutover — flags PROTOTYPES restants à valider

**Fix 9 et Fix 10 sont portés mais flags OFF.** Activation conditionnée à
validation backtest sur données accumulées.

### Fix 9 — Trail régime-gaté

Condition : ≥ 14 jours de 1m HL accumulés dans `data/ohlc_1m/`. Backtest
à rejouer (port V6 `backtest/backtest_regime_trail.py` → V7).

Activation :
```yaml
risk:
  regime_gated_trail: true
```

### Fix 10 — Haut levier exempt + cap

Condition : observer le net réalisé HYPE sur ≥ 7 jours en live. Si HYPE
continue à se faire EMERGENCY EXIT en boucle :
```yaml
risk:
  high_lev_emergency_exempt: true   # Knob A : laisse respirer haut levier
  # OU
  leverage_cap_enabled: true        # Knob B : cap entrée (TODO V7 : setup HL par symbol)
```

## Architecture rappel (composants P8)

| Composant | Fichier | Rôle |
|---|---|---|
| Write adapter | `execution/hyperliquid_write_adapter.py` | place/cancel/get_open_orders + Fix #2 streak |
| Boot reconciler | `execution/boot_reconciler.py` | sync positions+orders HL au start |
| Emergency exit | `risk/emergency_exit.py` | force-close ROE + Fix #8 grâce orpheline + Fix 10 helper |
| Dust filter | `execution/engine.py` (DUST_NOTIONAL_USD) | Fix #4 |
| PlaceResult | `strategies/grid_engine.py` | Fix #7 (déjà déployé) |
| HL cache backoff | `execution/hyperliquid_adapter.py` | Fix #6 (déjà déployé) |
| Entry regime | `regime/entry_regime.py` | Fix #9 helper (flag OFF) |

## Commits de référence

```
c23080d V7 P8 étapes G+H : ports Fix 9 et Fix 10 (PROTOTYPES, flags OFF)
54bc8ab V7 P8 étape E : EmergencyExitManager + Fix #8
b0a5a31 V7 P8 étape D : port Fix #4 (dust position filter)
219d550 V7 P8 étape C : port Fix #2 (open_orders empty streak)
9c12ead V7 P8 étape B : BootReconciler pour cutover live
2297898 V7 P8 étape A : HyperliquidWriteAdapter + wiring live mode
```

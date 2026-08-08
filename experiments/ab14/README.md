# A/B 14 jours — llmbot vs simplebot (paper)

Protocole contrôlé : univers fixe, equity 200 $, fees alignés, states isolés.
**Ne touche pas** au simplebot LIVE (wallet HL2, `simplebot/state/`).

## Décision J+14 (rappel)

| Verdict | Conditions |
|---------|------------|
| **GAGNE** (B) | PnL_B > PnL_A **et** maxDD_B ≤ maxDD_A+2pts **et** PF_B≥1.3 **et** ≥20 trades B **et** LocalAI uptime ≥90% |
| **ÉQUIVALENT** | \|ΔPnL\| < 3 $ et DD comparable |
| **PERD** | sinon → entry LLM abandonnée (veto news only) |

Stop anticipé : equity ≤ 160 $ (−20 %) sur un bras.

## Setup J0

```bash
cd /home/francois/Scalper-V6
bash experiments/ab14/setup_j0.sh
bash experiments/ab14/start_paper.sh
```

Vérifs :

```bash
tail -f experiments/ab14/logs/simplebot_paper.log
tail -f experiments/ab14/logs/llmbot_paper.log
# doit afficher state=.../experiments/ab14/... et DRY-RUN
pgrep -af "simplebot.run|llmbot.run"
# le LIVE simplebot garde son lock dans simplebot/state/
```

## Daily

```bash
bash experiments/ab14/snapshot_daily.sh
# optionnel cron 00:05 UTC :
# 5 0 * * * cd /home/francois/Scalper-V6 && bash experiments/ab14/snapshot_daily.sh
```

## Stop (paper only)

```bash
bash experiments/ab14/stop_paper.sh
```

## Fichiers

| Path | Rôle |
|------|------|
| `../ab14_llm_vs_simple.env` | Params harmonisés |
| `simplebot_state/` | State + best_params figés bras A |
| `llmbot_state/` | State + decisions bras B |
| `logs/` | stdout des 2 process + pidfiles |
| `report/metrics.csv` | Série quotidienne |
| `daily/day_XX.md` | Journal |
| `report/final_j14.md` | À rédiger à J14 |

## Env clés

- `SIMPLEBOT_STATE_DIR` / `LLMBOT_STATE_DIR` → isolation lock + state
- `*_DRY_RUN=1` obligatoire
- `*_PAPER_START_EQUITY=200`

## Midterm J7 / Final J14

Remplir `report/midterm_j7.md` et `report/final_j14.md` avec le tableau primary metrics
(net, maxDD, PF, expectancy, #trades, WR).

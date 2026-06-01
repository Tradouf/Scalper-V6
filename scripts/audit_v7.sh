#!/usr/bin/env bash
# Audit périodique V7 via Claude Opus en headless.
# Usage : ./scripts/audit_v7.sh [HOURS]   (default 6)
# Cron  : 0 */6 * * * cd /home/francois/SalleDesMarches_v7 && ./scripts/audit_v7.sh >> audit_history/cron.log 2>&1

set -euo pipefail

HOURS="${1:-6}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PROMPT_FILE="$REPO/scripts/audit_prompt_v7.md"
TS="$(date '+%Y-%m-%d_%H-%M-%S')"
RUN_LOG="$REPO/audit_history/run_${TS}.log"
METRICS_FILE="$REPO/audit_history/metrics_${TS}.md"
mkdir -p "$REPO/audit_history"

# Fichiers de sortie de l'audit (créés s'ils n'existent pas).
touch "$REPO/audit_log_v7.md" "$REPO/code_proposals_v7.md"

# 1) Pré-agrégation des métriques (déterministe, gratuit).
bash "$REPO/scripts/audit_metrics_v7.sh" "$HOURS" > "$METRICS_FILE"

# 2) Prompt final = prompt V7 + métriques.
FULL_PROMPT="$(cat "$PROMPT_FILE")

---

$(cat "$METRICS_FILE")"

# 3) Outils whitelist stricts : pas de Bash arbitraire, Edit limité par le prompt.
ALLOWED_TOOLS=(
    "Read"
    "Edit"
    "Bash(git add config/allocation.yaml)"
    "Bash(git add audit_log_v7.md)"
    "Bash(git add code_proposals_v7.md)"
    "Bash(git commit:*)"
    "Bash(git status)"
    "Bash(git diff:*)"
    "Bash(git log:*)"
    "Bash(date)"
)
TOOLS_ARG="${ALLOWED_TOOLS[*]}"

echo "=== AUDIT V7 START $TS (window=${HOURS}h) ===" | tee -a "$RUN_LOG"
OLD_HEAD="$(git rev-parse HEAD)"

claude -p \
    --model opus \
    --max-budget-usd 2.00 \
    --allowedTools $TOOLS_ARG \
    --append-system-prompt "Tu es en mode audit autonome non-interactif V7. Tu peux Edit UNIQUEMENT : config/allocation.yaml, audit_log_v7.md, code_proposals_v7.md. Tu ne touches PAS au code Python. Tu ne lances pas le bot. Réponse finale courte." \
    "$FULL_PROMPT" \
    2>&1 | tee -a "$RUN_LOG"

EXIT_CODE=${PIPESTATUS[0]}
echo "=== AUDIT V7 END $TS exit=$EXIT_CODE ===" | tee -a "$RUN_LOG"

# 4) Restart conditionnel si allocation.yaml a changé (anti-flap 30 min).
NEW_HEAD="$(git rev-parse HEAD)"
ANTI_FLAP_FILE="$REPO/audit_history/last_restart.ts"
ANTI_FLAP_SEC=1800

if [[ "$OLD_HEAD" != "$NEW_HEAD" ]]; then
    if git diff --name-only "$OLD_HEAD" "$NEW_HEAD" | grep -q '^config/allocation\.yaml$'; then
        echo "[AUDIT] allocation.yaml modifié ($OLD_HEAD → $NEW_HEAD)" | tee -a "$RUN_LOG"
        if ! "$REPO/scripts/bot.sh" status > /dev/null 2>&1; then
            echo "[AUDIT] Bot non actif — pas de restart auto" | tee -a "$RUN_LOG"
        else
            now=$(date +%s); last=0
            [[ -f "$ANTI_FLAP_FILE" ]] && last=$(cat "$ANTI_FLAP_FILE" 2>/dev/null || echo 0)
            age=$(( now - last ))
            if [[ $age -lt $ANTI_FLAP_SEC ]]; then
                echo "[AUDIT] Anti-flap: dernier restart il y a $((age/60)) min, skip" | tee -a "$RUN_LOG"
            else
                echo "[AUDIT] Restart bot via bot.sh restart..." | tee -a "$RUN_LOG"
                "$REPO/scripts/bot.sh" restart 2>&1 | tee -a "$RUN_LOG"
                echo "$now" > "$ANTI_FLAP_FILE"
            fi
        fi
    else
        echo "[AUDIT] Commit sans changement allocation.yaml — pas de restart" | tee -a "$RUN_LOG"
    fi
fi

exit "$EXIT_CODE"

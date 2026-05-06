#!/usr/bin/env bash
# Audit périodique du bot via Claude Opus en headless.
# Usage: ./scripts/audit.sh [HOURS]   (default 6)
# Cron suggéré: 0 */6 * * * cd /home/francois/SalleDesMarches_fixed && ./scripts/audit.sh >> audit_history/cron.log 2>&1

set -euo pipefail

HOURS="${1:-6}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PROMPT_FILE="$REPO/scripts/audit_prompt.md"
TS="$(date '+%Y-%m-%d_%H-%M-%S')"
RUN_LOG="$REPO/audit_history/run_${TS}.log"
METRICS_FILE="$REPO/audit_history/metrics_${TS}.md"

mkdir -p "$REPO/audit_history"

# 1) Pré-agrégation des métriques en bash (gratuit, déterministe)
bash "$REPO/scripts/audit_metrics.sh" "$HOURS" > "$METRICS_FILE"

# 2) Construit le prompt final = audit_prompt.md + métriques pré-calculées
FULL_PROMPT="$(cat "$PROMPT_FILE")

---

$(cat "$METRICS_FILE")"

# 3) Outils whitelist stricts. Pas d'accès Bash sauf git, ni Write.
# Edit n'est autorisé qu'implicitement par le prompt qui dit "config/settings.py uniquement".
ALLOWED_TOOLS=(
    "Read"
    "Edit"
    "Bash(git add config/settings.py)"
    "Bash(git add audit_log.md)"
    "Bash(git add code_proposals.md)"
    "Bash(git commit:*)"
    "Bash(git status)"
    "Bash(git diff:*)"
    "Bash(git log:*)"
    "Bash(date)"
)
TOOLS_ARG="${ALLOWED_TOOLS[*]}"

echo "=== AUDIT START $TS ===" | tee -a "$RUN_LOG"

# Capture le HEAD avant l'audit, pour détecter ensuite si Claude a commité
# un changement de settings (et donc s'il faut redémarrer le bot).
OLD_HEAD="$(git rev-parse HEAD)"

# Budget cap réaliste : 2$ par audit. Avec pré-agrégation, l'audit consomme
# typiquement 0.30-0.80$ — la marge couvre les cas où Claude veut explorer.
# 4 audits/jour = 8$/jour max.
claude -p \
    --model opus \
    --max-budget-usd 2.00 \
    --allowedTools $TOOLS_ARG \
    --append-system-prompt "Tu es en mode audit autonome non-interactif. Tu peux Edit UNIQUEMENT : config/settings.py, audit_log.md, code_proposals.md. Tu ne touches PAS au code Python. Tu ne lances pas le bot. Réponse finale courte." \
    "$FULL_PROMPT" \
    2>&1 | tee -a "$RUN_LOG"

EXIT_CODE=${PIPESTATUS[0]}
echo "=== AUDIT END $TS exit=$EXIT_CODE ===" | tee -a "$RUN_LOG"

# ── Restart conditionnel du bot si settings.py a été modifié ──
# Garde-fous :
#   1) Restart UNIQUEMENT si Claude a commité une modif de config/settings.py
#   2) Anti-flap : refuse si un précédent restart a eu lieu il y a moins de 30 min
#   3) Skip si le bot n'est pas actif (l'utilisateur l'a peut-être stoppé exprès)
NEW_HEAD="$(git rev-parse HEAD)"
ANTI_FLAP_FILE="$REPO/audit_history/last_restart.ts"
ANTI_FLAP_SEC=1800   # 30 min

if [[ "$OLD_HEAD" != "$NEW_HEAD" ]]; then
    if git diff --name-only "$OLD_HEAD" "$NEW_HEAD" | grep -q '^config/settings\.py$'; then
        echo "=== AUDIT: settings.py modifié (HEAD $OLD_HEAD → $NEW_HEAD) ===" | tee -a "$RUN_LOG"

        if ! "$REPO/scripts/bot.sh" status > /dev/null 2>&1; then
            echo "[AUDIT] Bot non actif — pas de restart auto" | tee -a "$RUN_LOG"
        else
            now=$(date +%s)
            last=0
            [[ -f "$ANTI_FLAP_FILE" ]] && last=$(cat "$ANTI_FLAP_FILE" 2>/dev/null || echo 0)
            age=$(( now - last ))
            if [[ $age -lt $ANTI_FLAP_SEC ]]; then
                wait_min=$(( (ANTI_FLAP_SEC - age) / 60 ))
                echo "[AUDIT] Anti-flap: dernier restart il y a $((age/60)) min, skip (cool-down ${wait_min} min)" | tee -a "$RUN_LOG"
            else
                echo "[AUDIT] Restart bot via bot.sh restart..." | tee -a "$RUN_LOG"
                "$REPO/scripts/bot.sh" restart 2>&1 | tee -a "$RUN_LOG"
                echo "$now" > "$ANTI_FLAP_FILE"
            fi
        fi
    else
        echo "[AUDIT] Commit sans changement settings (audit_log/code_proposals only) — pas de restart" | tee -a "$RUN_LOG"
    fi
fi

exit "$EXIT_CODE"

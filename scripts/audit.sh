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
    "Bash(git commit:*)"
    "Bash(git status)"
    "Bash(git diff:*)"
    "Bash(git log:*)"
    "Bash(date)"
)
TOOLS_ARG="${ALLOWED_TOOLS[*]}"

echo "=== AUDIT START $TS ===" | tee -a "$RUN_LOG"

# Budget cap réaliste : 2$ par audit. Avec pré-agrégation, l'audit consomme
# typiquement 0.30-0.80$ — la marge couvre les cas où Claude veut explorer.
# 4 audits/jour = 8$/jour max.
claude -p \
    --model opus \
    --max-budget-usd 2.00 \
    --allowedTools $TOOLS_ARG \
    --append-system-prompt "Tu es en mode audit autonome non-interactif. Ne modifie QUE config/settings.py et audit_log.md. Ne lance pas le bot, ne fais rien d'autre. Réponse finale courte." \
    "$FULL_PROMPT" \
    2>&1 | tee -a "$RUN_LOG"

EXIT_CODE=${PIPESTATUS[0]}
echo "=== AUDIT END $TS exit=$EXIT_CODE ===" | tee -a "$RUN_LOG"
exit "$EXIT_CODE"

#!/usr/bin/env bash
# Pré-agrégation des métriques sur les N dernières heures de logs.
# Sortie : bloc Markdown injecté dans le prompt Opus (réduit drastiquement
# la consommation de tokens vs envoi des logs bruts).
#
# Usage: ./scripts/audit_metrics.sh [HOURS]
# Default: 6 heures

set -euo pipefail

HOURS="${1:-6}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$REPO/logs/sdm.log"

if [[ ! -f "$LOG" ]]; then
    echo "ERREUR: $LOG introuvable" >&2
    exit 1
fi

# Construit un pattern grep pour les heures pertinentes (HH du log).
# Le log utilise HH:MM:SS sans date — on prend juste les N dernières heures
# basées sur l'heure courante.
NOW_H=$(date +%H | sed 's/^0//')
HOURS_LIST=()
for ((i=0; i<HOURS; i++)); do
    H=$(( (NOW_H - i + 24) % 24 ))
    HOURS_LIST+=("$(printf '%02d:' "$H")")
done
PATTERN=$(IFS='|'; echo "${HOURS_LIST[*]}")

# Extrait la fenêtre temporelle (mais on est conservateur : on prend les N*450 lignes max
# pour couvrir N×6h en moyenne, ça rate rarement)
LINES=$(( HOURS * 3000 ))
WINDOW=$(tail -n "$LINES" "$LOG" | grep -E "^($PATTERN)" || true)

# Si le filtre par heure ne donne rien (changement de jour), prendre les LINES dernières
if [[ -z "$WINDOW" ]]; then
    WINDOW=$(tail -n "$LINES" "$LOG")
fi

# DÉDUP : un bug de double-handler peut faire apparaître chaque ligne en double
# (cf. incident 2026-05-05). On dédupe les lignes consécutives identiques pour
# que les compteurs soient justes.
WINDOW=$(echo "$WINDOW" | awk '!seen[$0]++')

count() {
    echo "$WINDOW" | grep -cE "$1" || true
}

cnt_emergency=$(count "EMERGENCY EXIT")
cnt_flip_refused=$(count "flip refusé")
cnt_external_exit=$(count "external_exit")
cnt_skip_conf=$(count "conf trop faible")
cnt_skip_cooldown=$(count "cooldown")
cnt_skip_contradict=$(count "signal contradictoire")
cnt_skip_blocked_hour=$(count "heure bloquée")
cnt_trail_armed=$(count "TRAIL ARM ")
cnt_native_sl_modify=$(count "TRAIL NATIVE SL MODIFY")
cnt_recovery_fallback=$(count "fallback SL serré")
cnt_recovery_abandon=$(count "abandon placement")
cnt_llm_error=$(count "LLM error")
cnt_hl_sync_error=$(count "HL sync.*error")
cnt_hl_cache_stale=$(count "HL cache périmé")
cnt_enter=$(count "\[LIVE\] ENTER")
cnt_consensus=$(count "CONSENSUS")
cnt_signal=$(count "external_exit\|EMERGENCY")

# État courant des positions ouvertes (dernier Stats cycle)
last_stats=$(echo "$WINDOW" | grep "Stats cycle" | tail -1 | sed 's/.*Stats cycle: //' || true)

# Dernier ROE par symbole observé en TRAIL
roe_table=$(echo "$WINDOW" | grep -E "TRAIL (SELL|BUY) " | awk '
{
    for (i=1; i<=NF; i++) {
        if ($i == "SELL" || $i == "BUY") { side=$i; sym=$(i+1); }
        if ($i ~ /^ROE=/) { roe=$i }
    }
    last[sym] = side " " roe
}
END {
    for (s in last) print "  " s ": " last[s]
}' | sort)

cat <<EOF
## Métriques agrégées sur les ${HOURS}h écoulées

| Compteur | Valeur |
|---|---|
| EMERGENCY EXIT (force close) | $cnt_emergency |
| flip refusé | $cnt_flip_refused |
| external_exit (SL exchange déclenché) | $cnt_external_exit |
| recovery fallback SL serré | $cnt_recovery_fallback |
| recovery abandon (=position non protégée) | $cnt_recovery_abandon |
| TRAIL ARM (passage en gain) | $cnt_trail_armed |
| TRAIL NATIVE SL MODIFY | $cnt_native_sl_modify |
| ENTER (nouvelles positions ouvertes) | $cnt_enter |
| CONSENSUS calculé | $cnt_consensus |
| SKIP conf trop faible | $cnt_skip_conf |
| SKIP cooldown | $cnt_skip_cooldown |
| SKIP signal contradictoire | $cnt_skip_contradict |
| SKIP heure bloquée | $cnt_skip_blocked_hour |
| LLM error | $cnt_llm_error |
| HL sync error | $cnt_hl_sync_error |
| HL cache périmé | $cnt_hl_cache_stale |

**Dernier Stats cycle** : ${last_stats:-N/A}

**ROE actuel par position** :
${roe_table:-(aucune position trail-monitorée)}

EOF

# Inclure les 30 dernières lignes "intéressantes" (pas trail/feature/regime bruyants)
# pour donner un échantillon textuel à Claude — limite stricte vs tout le log.
echo "## Échantillon des derniers événements (max 30 lignes pertinentes)"
echo '```'
echo "$WINDOW" | grep -vE "TRAIL (SELL|BUY) |FEATURE_ENGINE|REGIME_ENGINE|TECH FEATURES|ORDERBOOK FEATURES|FEATURE PIPELINE|REGIME PIPELINE" | tail -30
echo '```'

#!/usr/bin/env bash
# Pré-agrégation des métriques V7 sur les N dernières heures de logs/v7.log.
# Sortie : bloc Markdown injecté dans le prompt Opus (réduit la conso tokens vs
# envoi des logs bruts). Le log V7 a des timestamps complets "YYYY-MM-DD HH:MM:SS"
# et CHAQUE ligne est dupliquée (2 handlers) → on déduplique.
#
# Usage: ./scripts/audit_metrics_v7.sh [HOURS]   (default 6)

set -euo pipefail

HOURS="${1:-6}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$REPO/logs/v7.log"

if [[ ! -f "$LOG" ]]; then
    echo "ERREUR: $LOG introuvable" >&2
    exit 1
fi

# Cutoff = maintenant - N heures, au format "YYYY-MM-DD HH:MM:SS".
CUTOFF="$(date -d "-${HOURS} hours" '+%Y-%m-%d %H:%M:%S')"

# Fenêtre : lignes dont le timestamp >= CUTOFF, dédupliquées.
WINDOW="$(awk -v cut="$CUTOFF" '
    { ts = substr($0, 1, 19) }
    ts >= cut { if (!seen[$0]++) print }
' "$LOG")"

# Fallback si vide (rotation/format) : derniers ~4000 lignes dédupliquées.
if [[ -z "$WINDOW" ]]; then
    WINDOW="$(tail -n $((HOURS * 4000)) "$LOG" | awk '!seen[$0]++')"
fi

count() { echo "$WINDOW" | grep -cE "$1" || true; }
last()  { echo "$WINDOW" | grep -E "$1" | tail -n "${2:-3}" | sed -E 's/^(.{110}).*/\1/' || true; }

EQ_FIRST="$(echo "$WINDOW" | grep -oE 'equity=\$[0-9.]+' | head -1 | tr -d 'equity=$')"
EQ_LAST="$(echo "$WINDOW" | grep -oE 'equity=\$[0-9.]+' | tail -1 | tr -d 'equity=$')"

cat <<EOF
# Métriques V7 pré-calculées — fenêtre ${HOURS}h (depuis ${CUTOFF})

## Résultats
- Equity : début=\$${EQ_FIRST:-?}  fin=\$${EQ_LAST:-?}
- Ticks analytiques : $(count "v7.main — tick #")
- Régime (distribution) :
$(echo "$WINDOW" | grep -oE 'regime=[a-z_]+' | sort | uniq -c | sort -rn | sed 's/^/    /')

## Grille (pathologies)
- TP impossible (szi=0) → frozen : **$(count "TP .* impossible \(szi=0")**
- Niveaux abandonnés (frozen>timeout → done) : **$(count "frozen >.*→ done")**
- Reduce-only rejets : $(count "Reduce only")
- DRIFT détectés : $(count "DRIFT détecté")  | BREAKOUT : $(count "BREAKOUT")
- Activations grille : $(count "Grid ACTIVATED")  | Désactivations : $(count "Grid DEACTIVATED")
- Doublons re-posés (health_check re-pose) : $(count "health_check.*re-pose")

## Directionnel / risque
- EMERGENCY EXIT : **$(count "EMERGENCY EXIT")**  (par symbole :)
$(echo "$WINDOW" | grep -E "EMERGENCY EXIT" | grep -oE "EXIT( \(orphan\))? [A-Z]+" | awk '{print $NF}' | sort | uniq -c | sort -rn | sed 's/^/    /')
- Whipsaw (paires tick ouvre→ferme, sig_act tombe à 0/N avec orders>0) : indicatif
$(echo "$WINDOW" | grep -E "tick #" | grep -vE "orders=0 fills=0" | tail -4 | sed -E 's/^(.{95}).*/    /')

## Erreurs (types sur la fenêtre)
$(echo "$WINDOW" | grep -iE "error|exception" | grep -oE "[A-Za-z]*([Ee]rror|[Ee]xception)" | sort | uniq -c | sort -rn | head -8 | sed 's/^/    /')
- Échantillon récent :
$(last "error|exception" 3 | sed 's/^/    /')
EOF

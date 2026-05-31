#!/usr/bin/env bash
# Pre-check cutover V6 → V7. Lecture seule, ne touche rien.
# Usage : bash scripts/cutover_precheck.sh
#
# Vérifie que tout est prêt avant de :
#   1. Stop V6 (manuel)
#   2. Basculer config/allocation.yaml paper_mode: true → false
#   3. Démarrer V7 live (BootReconciler récupère positions HL héritées)
#
# Exit codes : 0 = GO, 1 = NOGO.

set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()  { echo -e "${GREEN}✓${NC} $1"; }
ko()  { echo -e "${RED}✗${NC} $1"; FAILS=$((FAILS+1)); }
warn(){ echo -e "${YELLOW}!${NC} $1"; }

FAILS=0

echo "=== Cutover V6 → V7 pre-check ==="
echo ""

# 1. Tests V7
if [[ -d "$REPO/.venv" ]]; then
    source "$REPO/.venv/bin/activate"
elif [[ -d "$REPO/../SalleDesMarches_fixed/.venv" ]]; then
    source "$REPO/../SalleDesMarches_fixed/.venv/bin/activate"
fi
if python3 -m pytest tests/ -q > /tmp/cutover_pytest.log 2>&1; then
    n=$(grep -oE "[0-9]+ passed" /tmp/cutover_pytest.log | head -1)
    ok "pytest : $n"
else
    ko "pytest échoué (voir /tmp/cutover_pytest.log)"
fi

# 2. Branche + commit récent
branch=$(git rev-parse --abbrev-ref HEAD)
if [[ "$branch" == "v7-allocation" ]]; then
    ok "branche=v7-allocation"
else
    warn "branche=$branch (attendu v7-allocation)"
fi

# 3. Pas de modifications non commitées critiques
dirty=$(git status -s -- main.py 'execution/*.py' 'risk/*.py' 'strategies/*.py' 'core/config.py' | wc -l)
if [[ "$dirty" -eq 0 ]]; then
    ok "code source clean (pas de modif non commitées)"
else
    ko "$dirty fichiers source modifiés non-commités"
    git status -s -- main.py 'execution/*.py' 'risk/*.py' 'strategies/*.py' 'core/config.py'
fi

# 4. Présence des composants P8
for f in execution/hyperliquid_write_adapter.py execution/boot_reconciler.py risk/emergency_exit.py; do
    if [[ -f "$f" ]]; then
        ok "présent : $f"
    else
        ko "manquant : $f"
    fi
done

# 5. Config V7 — paper_mode actuel
pm=$(awk '/^execution:/{f=1} f && /paper_mode:/{print $2; exit}' config/allocation.yaml)
echo "  → config/allocation.yaml paper_mode = $pm"
if [[ "$pm" == "false" ]]; then
    warn "paper_mode=false DÉJÀ — V7 démarrera en live au prochain start"
elif [[ "$pm" == "true" ]]; then
    ok "paper_mode=true (cutover = passer à false, puis ./scripts/bot.sh restart)"
else
    ko "paper_mode introuvable ou invalide"
fi

# 6. .env présent + HL_ACCOUNT_ADDRESS
if [[ -L "$REPO/.env" ]] || [[ -f "$REPO/.env" ]]; then
    if grep -q "^HL_ACCOUNT_ADDRESS=0x" .env 2>/dev/null; then
        ok ".env présent avec HL_ACCOUNT_ADDRESS"
    else
        ko ".env trouvé mais HL_ACCOUNT_ADDRESS absent/vide"
    fi
else
    ko ".env manquant"
fi

# 7. V6 prod : status (sera arrêté manuellement)
V6="/home/francois/SalleDesMarches_fixed"
if [[ -d "$V6" ]]; then
    if pgrep -f "main_v6" > /dev/null 2>&1; then
        warn "V6 prod TOURNE — penser à le stop AVANT de démarrer V7 live"
    else
        ok "V6 prod stoppé"
    fi
else
    warn "$V6 absent — V6 prod déjà démissionné ?"
fi

# 8. V7 actuel : status
if pgrep -af "v7.*main.py" > /dev/null 2>&1; then
    pid=$(pgrep -f "v7.*main.py" | head -1)
    warn "V7 tourne (PID=$pid) — un restart sera nécessaire pour charger la config live"
else
    ok "V7 actuel arrêté (prêt à démarrer en live)"
fi

# 9. Espace disque memory/
mem_size=$(du -sm memory/ 2>/dev/null | awk '{print $1}')
ok "memory/ : ${mem_size}MB"

# 10. Order registry size
if [[ -f memory/order_registry.json ]]; then
    n_records=$(python3 -c "import json; print(len(json.load(open('memory/order_registry.json')).get('records', [])))" 2>/dev/null)
    echo "  → order_registry.json : $n_records records (BootReconciler reconciliera au start)"
fi

# 11. OHLC collector accumulation
n_parquets=$(ls data/ohlc_1m/*.parquet 2>/dev/null | wc -l)
size=$(du -sm data/ohlc_1m/ 2>/dev/null | awk '{print $1}')
ok "OHLC 1m : $n_parquets symboles, ${size}MB accumulés"

echo ""
if [[ "$FAILS" -eq 0 ]]; then
    echo -e "${GREEN}=== PRE-CHECK OK ($FAILS échec) — GO cutover ===${NC}"
    echo ""
    echo "Séquence de cutover (manuelle) :"
    echo "  1. Stop V6 :              cd $V6 && bash start_sdm.sh stop   (ou kill -15 \$(cat $V6/sdm.pid))"
    echo "  2. Edit V7 config :       sed -i 's/paper_mode: true/paper_mode: false/' config/allocation.yaml"
    echo "  3. Restart V7 :           bash scripts/bot.sh restart"
    echo "  4. Surveiller :           bash scripts/bot.sh logs   # cherche 'BootReconciler résumé:'"
    echo "  5. Rollback si KO :       reverse 2+3, puis restart V6"
    exit 0
else
    echo -e "${RED}=== PRE-CHECK NOGO ($FAILS échec) — corriger avant cutover ===${NC}"
    exit 1
fi

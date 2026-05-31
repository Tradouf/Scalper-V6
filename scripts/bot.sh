#!/usr/bin/env bash
# bot.sh V7 — helper start/stop/status/restart/logs pour main.py V7.
# Usage : ./scripts/bot.sh {start|stop|restart|status|logs}
#
# V7 paper trading par défaut. Pas de dépendance LocalAI (pas de LLM).
# V6 prod continue de tourner sur ~/SalleDesMarches_fixed/ branche main.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PID_FILE="$REPO/logs/v7.pid"
LOG_FILE="$REPO/logs/v7.log"

cmd_status() {
    if [[ -f "$PID_FILE" ]]; then
        PID=$(cat "$PID_FILE")
        if ps -p "$PID" > /dev/null 2>&1; then
            UPTIME=$(ps -o etime= -p "$PID" | tr -d ' ')
            echo "✅ V7 actif PID=$PID uptime=$UPTIME"
            return 0
        else
            echo "⚠️ PID file orphelin (PID=$PID mort) — nettoyé"
            rm -f "$PID_FILE"
            return 1
        fi
    else
        if pgrep -f "v7.*main.py" > /dev/null; then
            echo "⚠️ V7 tourne SANS PID file :"
            pgrep -af "v7.*main.py"
            return 2
        fi
        echo "⏸️ V7 arrêté"
        return 1
    fi
}

cmd_start() {
    if cmd_status > /dev/null 2>&1; then
        echo "❌ V7 déjà actif :"
        cmd_status
        return 1
    fi
    rm -f "$PID_FILE"
    if [[ -d "$REPO/.venv" ]]; then
        source "$REPO/.venv/bin/activate"
    elif [[ -d "$REPO/../SalleDesMarches_fixed/.venv" ]]; then
        source "$REPO/../SalleDesMarches_fixed/.venv/bin/activate"
    else
        echo "❌ Aucun venv trouvé"
        return 1
    fi
    # Source .env (HL_PRIVATE_KEY etc.) — requis en live mode pour HyperliquidWriteAdapter.
    # En paper, le wallet n'est pas utilisé mais l'export est sans danger.
    if [[ -f "$REPO/.env" ]] || [[ -L "$REPO/.env" ]]; then
        set -a
        source "$REPO/.env"
        set +a
    fi
    nohup python3 main.py >> "$LOG_FILE" 2>&1 < /dev/null &
    PID=$!
    disown
    echo "$PID" > "$PID_FILE"
    sleep 2
    cmd_status
}

cmd_stop() {
    if [[ -f "$PID_FILE" ]]; then
        PID=$(cat "$PID_FILE")
        if ps -p "$PID" > /dev/null 2>&1; then
            echo "⏹️ Stop PID=$PID (SIGTERM, attente arrêt propre)..."
            kill -15 "$PID"
            for i in {1..15}; do
                if ! ps -p "$PID" > /dev/null 2>&1; then
                    echo "✅ V7 arrêté en ${i}s"
                    rm -f "$PID_FILE"
                    return 0
                fi
                sleep 1
            done
            echo "⚠️ SIGTERM ignoré après 15s, escalade SIGKILL"
            kill -9 "$PID"
            rm -f "$PID_FILE"
        fi
    fi
    pkill -f "v7.*main.py" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "✅ Tout arrêté"
}

cmd_restart() {
    cmd_stop
    sleep 1
    cmd_start
}

cmd_logs() {
    tail -f "$LOG_FILE"
}

case "${1:-status}" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_restart ;;
    status)  cmd_status ;;
    logs)    cmd_logs ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs}"
        exit 1
        ;;
esac

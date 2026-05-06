#!/usr/bin/env bash
# Helper start/stop/status/restart pour le bot SalleDesMarches.
# Usage: ./scripts/bot.sh {start|stop|restart|status|logs}

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PID_FILE="$REPO/logs/sdm.pid"
LOG_FILE="$REPO/logs/sdm.log"

cmd_status() {
    if [[ -f "$PID_FILE" ]]; then
        PID=$(cat "$PID_FILE")
        if ps -p "$PID" > /dev/null 2>&1; then
            UPTIME=$(ps -o etime= -p "$PID" | tr -d ' ')
            echo "✅ Bot actif PID=$PID uptime=$UPTIME"
            return 0
        else
            echo "⚠️ PID file orphelin (PID=$PID mort) — nettoyé"
            rm -f "$PID_FILE"
            return 1
        fi
    else
        # Double check au cas où le PID file aurait disparu
        if pgrep -f "python3 main_v6.py" > /dev/null; then
            echo "⚠️ Bot tourne SANS PID file (instance ancienne ?) :"
            pgrep -af "python3 main_v6.py"
            return 2
        fi
        echo "⏸️ Bot arrêté"
        return 1
    fi
}

cmd_start() {
    if cmd_status > /dev/null 2>&1; then
        echo "❌ Bot déjà actif :"
        cmd_status
        return 1
    fi
    rm -f "$PID_FILE"
    source .venv/bin/activate
    set -a; source .env; set +a
    nohup python3 main_v6.py >> "$LOG_FILE" 2>&1 < /dev/null &
    disown
    sleep 3
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
                    echo "✅ Bot arrêté en ${i}s"
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
    # Cleanup tout reste éventuel
    pkill -f "python3 main_v6.py" 2>/dev/null || true
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

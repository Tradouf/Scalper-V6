#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
API pour connecter SalleDesMarches V5 au dashboard HTML.

- Expose /api/state avec toutes les infos pour le dashboard
- Expose /api/trades pour l'historique des trades
- Expose /api/control pour quelques actions simples (pause, resume, flat)
"""

import threading
import time
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request

from config import settings as SETTINGS
from memory.sharedmemory import SharedMemory
from exchanges.hyperliquid import HyperliquidExchangeClient
from mainv5 import SalleDesMarchesV5  # classe existante dans ton code


# ----------------------------------------------------------------------
# Initialisation bot + mémoire
# ----------------------------------------------------------------------

memory = SharedMemory(SETTINGS.SHAREDMEMORY_PATH)
exchange = HyperliquidExchangeClient(
    wallet_path=SETTINGS.HL_WALLET_PATH,
    network="MAINNET",
)

bot = SalleDesMarchesV5(
    memory=memory,
    exchange=exchange,
    settings=SETTINGS,
)

# Flag simple pour contrôler la boucle
RUN_FLAG = {"running": True}

def bot_loop() -> None:
    """Boucle principale du bot, contrôlée par RUN_FLAG."""
    while True:
        if RUN_FLAG["running"]:
            try:
                bot.run_cycle_once()  # tu ajoutes une méthode qui fait UN cycle
            except Exception as e:
                # log minimal pour éviter que le thread meure en silence
                print(f"[BOT] Erreur cycle: {e}")
        time.sleep(SETTINGS.SCAN_INTERVAL_SEC)


# ----------------------------------------------------------------------
# Flask app
# ----------------------------------------------------------------------

app = Flask(__name__)


def _safe_get_memory() -> Dict[str, Any]:
    """
    Compacte la mémoire SharedMemory dans un dict propre pour le front.
    À adapter suivant ce que tu stockes réellement dans sharedmemory.json.
    """
    data: Dict[str, Any] = {}

    try:
        data["equity"] = memory.get_equity()
    except Exception:
        data["equity"] = None

    try:
        data["open_positions"] = memory.get_open_positions()
    except Exception:
        data["open_positions"] = []

    try:
        data["trades"] = memory.get_trades(limit=100)
    except Exception:
        data["trades"] = []

    try:
        regime = memory.get_regime()
    except Exception:
        regime = {}

    data["regime"] = regime

    try:
        stats = memory.get_stats()
    except Exception:
        stats = {}

    data["stats"] = stats

    return data


@app.route("/api/state", methods=["GET"])
def api_state() -> Any:
    """
    État global pour alimenter les KPI, la vue engine, etc.
    """
    payload = _safe_get_memory()
    payload["bot_running"] = RUN_FLAG["running"]
    return jsonify(payload)


@app.route("/api/trades", methods=["GET"])
def api_trades() -> Any:
    """
    Retourne l’historique des trades (pour table + graphiques).
    Paramètres optionnels: ?symbol=BTC&limit=200
    """
    symbol = request.args.get("symbol")
    limit_str = request.args.get("limit", "200")

    try:
        limit = max(1, min(int(limit_str), 1000))
    except ValueError:
        limit = 200

    try:
        trades: List[Dict[str, Any]] = memory.get_trades(limit=limit)
    except Exception:
        trades = []

    if symbol:
        trades = [t for t in trades if t.get("symbol") == symbol]

    return jsonify({"trades": trades})


@app.route("/api/control", methods=["POST"])
def api_control() -> Any:
    """
    Petites commandes pour le bot depuis le dashboard.

    JSON attendu:
      { "action": "pause" }
      { "action": "resume" }
      { "action": "flat" }          # ferme toutes les positions
    """
    data = request.get_json(force=True, silent=True) or {}
    action = str(data.get("action", "")).lower().strip()

    if action == "pause":
        RUN_FLAG["running"] = False
        return jsonify({"ok": True, "running": False})

    if action == "resume":
        RUN_FLAG["running"] = True
        return jsonify({"ok": True, "running": True})

    if action == "flat":
        try:
            bot.close_all_positions(reason="dashboard_flat")
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": False, "error": "action inconnue"}), 400


# ----------------------------------------------------------------------
# Démarrage
# ----------------------------------------------------------------------

def start_bot_thread() -> None:
    t = threading.Thread(target=bot_loop, name="SDM-BotLoop", daemon=True)
    t.start()


if __name__ == "__main__":
    # On lance le bot en thread séparé
    start_bot_thread()

    # Et on lance l’API pour le dashboard
    app.run(host="0.0.0.0", port=8081, debug=False)

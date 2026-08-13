#!/usr/bin/env bash
# Collecte hebdomadaire des bougies Hyperliquid NATIVES dans l'archive locale.
#
# Pourquoi ce script existe : `candleSnapshot` ne rend que ses 5000 dernières
# bougies, soit 52 jours en 15m. Le passé au-delà est définitivement perdu, mais
# le présent peut être conservé — à condition de passer avant que la fenêtre ne
# glisse. Une exécution hebdomadaire laisse 45 jours de marge ; deux semaines
# ratées d'affilée ne troueraient toujours rien, trois oui.
#
# C'est cette archive qui permettra un jour de rejouer le §9 sur des données
# natives plutôt que sur le proxy Binance.
#
# Sort en code non nul si la collecte échoue OU si la couverture 15m recule
# sous le seuil d'alerte — systemd déclenche alors l'unité OnFailure.

set -uo pipefail

REPO="/home/francois/Scalper-V6"
PY="$REPO/.venv/bin/python"
# EXPORTÉ : le contrôle de couverture tourne dans un sous-processus Python qui
# lit cette valeur via l'environnement. Sans `export`, il retombait sur son
# défaut interne et le seuil configuré ici n'avait aucun effet — un garde-fou
# qui a l'air de fonctionner mais ne garde rien.
export MIN_15M_DAYS="${MIN_15M_DAYS:-40}"     # sous ce seuil, la fenêtre API a glissé

cd "$REPO" || { echo "FATAL: dépôt introuvable: $REPO"; exit 1; }

echo "=== $(date -Is) — collecte native Hyperliquid ==="

if ! "$PY" -m confluence.run collect; then
    echo "ÉCHEC: la commande collect a retourné une erreur"
    exit 1
fi

# Contrôle de couverture : une collecte qui « réussit » en n'ajoutant rien
# (clé API cassée, symbole renommé, endpoint modifié) doit alerter aussi.
"$PY" - <<'PYCHECK'
import json
import sys
from pathlib import Path

MIN_DAYS = float(__import__("os").environ.get("MIN_15M_DAYS", "40"))
archive = Path("confluence/state/archive/BTC__15m.json")
if not archive.exists():
    print("ÉCHEC: archive 15m absente")
    sys.exit(1)

candles = json.loads(archive.read_text())
if not candles:
    print("ÉCHEC: archive 15m vide")
    sys.exit(1)

span = (candles[-1]["ts"] - candles[0]["ts"]) / 86_400_000.0
import time
age_h = (time.time() * 1000 - candles[-1]["ts"]) / 3_600_000.0
print(f"archive 15m: {len(candles)} bougies, {span:.0f} j couverts, "
      f"dernière bougie il y a {age_h:.1f} h")

if span < MIN_DAYS:
    print(f"ÉCHEC: couverture {span:.0f} j < seuil {MIN_DAYS:.0f} j")
    sys.exit(1)
if age_h > 48:
    print(f"ÉCHEC: dernière bougie vieille de {age_h:.0f} h — collecte non fraîche")
    sys.exit(1)
print("OK")
PYCHECK

status=$?
echo "=== $(date -Is) — fin (code $status) ==="
exit $status

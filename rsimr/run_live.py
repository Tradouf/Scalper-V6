"""Boucle live RSI-MR — un sweep par heure UTC, DRY-RUN par défaut.

⚠️ Le live réel exige RSIMR_DRY_RUN=0 dans l'environnement. Sans cette
variable, aucun ordre n'est passé : les fills sont simulés au close.

Usage : python -m rsimr.run_live [--once]
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import sys
import time
from pathlib import Path

from rsimr.live import (DRY_RUN, MAX_CONCURRENT, NOTIONAL_PCT, REGIME_FILTER,
                        RSIMRLiveTrader)
from rsimr.paper import SYMBOLS

LOCK_FILE = Path(__file__).resolve().parent / "state" / "rsimr_live.lock"
LOOP_SEC = 60
SWEEP_DELAY_SEC = 120     # hh:02 — la bougie 1h est servie partout


def acquire_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    fh.truncate(0)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def main() -> int:
    parser = argparse.ArgumentParser(description="RSI-MR live (dry-run par défaut)")
    parser.add_argument("--once", action="store_true", help="un sweep puis sortie")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    logger = logging.getLogger("sdm.rsimr.run_live")

    lock = acquire_lock()
    if lock is None:
        logger.critical("Une instance live RSI-MR tourne déjà — refus.")
        return 1

    trader = RSIMRLiveTrader()
    logger.warning(
        "[RSIMR-LIVE] %s — %d symboles, %.0f%% d'equity/trade, %d slots max, "
        "filtre de régime %s",
        "DRY-RUN (aucun ordre réel)" if trader.dry_run else
        "⚠️ ORDRES RÉELS sur wallet HL2",
        len(SYMBOLS), 100 * NOTIONAL_PCT, MAX_CONCURRENT,
        "actif (régime calme exclu)" if REGIME_FILTER else "DÉSACTIVÉ")
    if not trader.dry_run:
        logger.warning("[RSIMR-LIVE] le verdict paper est prévu mi-septembre : "
                       "ce live anticipe volontairement ce verdict")

    if args.once:
        trader.sweep_if_due()
        return 0

    while True:
        try:
            now = time.time()
            if now % 3600 >= SWEEP_DELAY_SEC:
                trader.sweep_if_due(now)
        except Exception as e:
            logger.error("[RSIMR-LIVE] tick en erreur: %r", e, exc_info=True)
        time.sleep(LOOP_SEC)


if __name__ == "__main__":
    sys.exit(main())

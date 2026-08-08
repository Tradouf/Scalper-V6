"""Boucle RSI-MR paper — un sweep par heure UTC (peu après la clôture 1h),
verrou single-instance, paper only. Usage : python -m rsimr.run [--once]"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import sys
import time
from pathlib import Path

from rsimr.paper import FEE_SIDE, PAPER_EQUITY0, RSIMRPaperTrader, SYMBOLS

LOCK_FILE = Path(__file__).resolve().parent / "state" / "rsimr.lock"
LOOP_SEC = 60             # vérifie chaque minute si une nouvelle heure a clôturé
SWEEP_DELAY_SEC = 120     # attendre hh:02 que la bougie 1h soit servie partout


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
    parser = argparse.ArgumentParser(description="RSI-MR — rachat de survente paper")
    parser.add_argument("--once", action="store_true",
                        help="un seul sweep si dû, puis sortie")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    logger = logging.getLogger("sdm.rsimr.run")

    lock = acquire_lock()
    if lock is None:
        logger.critical("Une instance RSI-MR tourne déjà (rsimr.lock) — refus.")
        return 1

    trader = RSIMRPaperTrader()
    logger.info(
        "[RSIMR-PAPER] démarré — RSI(14) 1h 30↑ long only, H=4h, %d symboles, "
        "frais %.1f bps RT, equity paper %.2f$ — AUCUN ordre réel",
        len(SYMBOLS), 2 * FEE_SIDE * 1e4, PAPER_EQUITY0)
    if args.once:
        trader.sweep_if_due()
        return 0

    while True:
        try:
            now = time.time()
            # sweep seulement passé hh:02 (bougie 1h précédente clôturée partout)
            if now % 3600 >= SWEEP_DELAY_SEC:
                trader.sweep_if_due(now)
        except Exception as e:
            logger.error("[RSIMR-PAPER] tick en erreur: %r", e, exc_info=True)
        time.sleep(LOOP_SEC)


if __name__ == "__main__":
    sys.exit(main())

"""Boucle XSMom paper — un rebalance par jour UTC (peu après 00:00), verrou
single-instance, paper only. Usage : python -m xsmom.run [--once]"""

from __future__ import annotations

import argparse
import fcntl
import logging
import sys
import time
from pathlib import Path

from xsmom.paper import XSMomentumPaperTrader

LOCK_FILE = Path(__file__).resolve().parent / "state" / "xsmom.lock"
LOOP_SEC = 600           # vérifie toutes les 10 min si un nouveau jour UTC a commencé
REBALANCE_DELAY_SEC = 600  # attendre 00:10 UTC que la bougie 1d soit servie partout


def acquire_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    fh.truncate(0)
    fh.write(str(__import__("os").getpid()))
    fh.flush()
    return fh


def main() -> int:
    parser = argparse.ArgumentParser(description="XSMom — momentum cross-sectionnel paper")
    parser.add_argument("--once", action="store_true", help="un seul rebalance si dû, puis sortie")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    logger = logging.getLogger("sdm.xsmom.run")

    lock = acquire_lock()
    if lock is None:
        logger.critical("Une instance XSMom tourne déjà (xsmom.lock) — refus.")
        return 1

    trader = XSMomentumPaperTrader()
    logger.info(
        "[XSMOM-PAPER] démarré — score=ret14j/vol20j, 8L/8S, 7 tranches, "
        "frais maker %.1f bps/côté, equity paper %.2f$ — AUCUN ordre réel",
        0.00015 * 1e4, trader.state["equity"],
    )
    if args.once:
        trader.rebalance_if_due()
        return 0

    while True:
        try:
            now = time.time()
            # rebalance seulement passé 00:10 UTC (bougie 1d de la veille close)
            if now % 86_400 >= REBALANCE_DELAY_SEC:
                trader.rebalance_if_due(now)
        except Exception as e:
            logger.error("[XSMOM-PAPER] tick en erreur: %r", e, exc_info=True)
        time.sleep(LOOP_SEC)


if __name__ == "__main__":
    sys.exit(main())

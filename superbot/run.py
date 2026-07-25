"""
Point d'entrée SuperBot.

    python -m superbot.run                  # optimiseur (thread) + trader, DRY-RUN par défaut
    python -m superbot.run --optimize-once  # une optimisation puis exit (cron-friendly)
    python -m superbot.run --live-only      # trader seul (optimiseur lancé ailleurs)
    SUPERBOT_DRY_RUN=0 python -m superbot.run   # ordres réels (wallet HL3_*)
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from superbot import config
from superbot.live_trader import SuperLiveTrader, make_third_wallet_client
from superbot.optimizer import SuperOptimizer


def acquire_single_instance_lock():
    """flock : UNE instance SuperBot par machine (leçon simplebot 11/07 :
    lancement manuel + systemd = double bot, hystérésis contournée)."""
    import fcntl
    import os

    lock_path = config.STATE_DIR / "superbot.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def main() -> int:
    parser = argparse.ArgumentParser(description="SuperBot — 3 sleeves + HMM double gate")
    parser.add_argument("--optimize-once", action="store_true")
    parser.add_argument("--live-only", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    logger = logging.getLogger("sdm.superbot")

    optimizer = SuperOptimizer()
    if args.optimize_once:
        optimizer.run_once()
        return 0

    lock = acquire_single_instance_lock()   # gardé vivant toute la vie du process
    if lock is None:
        logger.critical("Une instance SuperBot tourne déjà (superbot.lock) — refus.")
        time.sleep(60)
        return 1

    if not args.live_only:
        if not config.BEST_PARAMS_FILE.exists():
            logger.info("Pas de best_params.json — optimisation initiale…")
            optimizer.run_once()
        optimizer.start()

    client = None
    if not config.DRY_RUN:
        client = make_third_wallet_client()
    else:
        logger.info("Mode DRY-RUN — papier, aucun wallet requis "
                    "(SUPERBOT_DRY_RUN=0 pour le live)")

    trader = SuperLiveTrader(client=client)
    trader.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())

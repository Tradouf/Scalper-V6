"""
Point d'entrée SimpleBot : optimiseur périodique + trader live.

    python -m simplebot.run                 # optimiseur (thread) + live, DRY-RUN par défaut
    python -m simplebot.run --optimize-once # une optimisation puis exit (cron-friendly)
    python -m simplebot.run --live-only     # live seul (optimiseur lancé ailleurs)
    SIMPLEBOT_DRY_RUN=0 python -m simplebot.run   # ordres réels (wallet HL2_*)
"""

from __future__ import annotations

import argparse
import logging
import sys

from simplebot import config
from simplebot.optimizer import BacktestOptimizerAgent
from simplebot.live_trader import ParamStore, SimpleLiveTrader, make_second_wallet_client


def main() -> int:
    parser = argparse.ArgumentParser(description="SimpleBot — algo paramétrique auto-optimisé")
    parser.add_argument("--optimize-once", action="store_true",
                        help="lance une optimisation puis quitte")
    parser.add_argument("--live-only", action="store_true",
                        help="ne lance pas l'optimiseur en fond")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    logger = logging.getLogger("sdm.simplebot")

    optimizer = BacktestOptimizerAgent()

    if args.optimize_once:
        optimizer.run_once()
        return 0

    if not args.live_only:
        # première optimisation synchrone : le live démarre avec des paramètres frais
        if not config.BEST_PARAMS_FILE.exists():
            logger.info("Pas de best_params.json — optimisation initiale…")
            optimizer.run_once()
        optimizer.start()

    if config.MOMENTUM_ENABLED:
        # Stratégie momentum 4h en PAPER (params figés, aucun ordre réel).
        from simplebot.momentum import MomentumPaperTrader
        momentum = MomentumPaperTrader()
        momentum.start()
        logger.info("Momentum 4h paper démarré (SIMPLEBOT_MOMENTUM=0 pour désactiver)")

    client = None
    if not config.DRY_RUN:
        client = make_second_wallet_client()
    else:
        logger.info("Mode DRY-RUN — aucun wallet requis, aucun ordre envoyé "
                    "(SIMPLEBOT_DRY_RUN=0 pour le live)")

    trader = SimpleLiveTrader(client=client, store=ParamStore())
    trader.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())

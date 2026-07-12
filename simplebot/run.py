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
import time

from simplebot import config
from simplebot.optimizer import BacktestOptimizerAgent
from simplebot.live_trader import ParamStore, SimpleLiveTrader, make_second_wallet_client


def acquire_single_instance_lock():
    """Verrou flock : une seule instance de SimpleBot par machine.

    Incident 2026-07-11 18:53 : un lancement manuel a coexisté avec l'instance
    systemd (Restart=always) → deux bots sur le même wallet, logs en double et
    hystérésis kill-switch contournée (compteurs indépendants). Retourne le
    file-handle (à garder vivant) ou None si une instance tourne déjà."""
    import fcntl
    import os

    lock_path = config.STATE_DIR / "simplebot.lock"
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
        optimizer.run_once()   # one-shot cron-friendly : pas de verrou requis
        return 0

    lock = acquire_single_instance_lock()   # gardé vivant toute la vie du process
    if lock is None:
        logger.critical(
            "Une instance SimpleBot tourne déjà (simplebot.lock verrouillé) — "
            "refus de démarrer. Utiliser `systemctl --user restart simplebot`."
        )
        time.sleep(60)   # évite une boucle de restart systemd trop agressive
        return 1

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

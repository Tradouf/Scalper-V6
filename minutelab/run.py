"""
Point d'entrée MinuteLab.

    python -m minutelab.run --scan-once   # une sélection, affiche le classement
    python -m minutelab.run               # boucle permanente (paper trading)

PAPER TRADING UNIQUEMENT — aucun ordre réel.
"""

from __future__ import annotations

import argparse
import logging
import sys

from minutelab import config
from minutelab.data1m import fetch_recent_1m
from minutelab.selector import select


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )


def scan_once(top: int = 15) -> int:
    candles = fetch_recent_1m(config.SYMBOL, config.WARMUP_HOURS)
    if len(candles) < config.LOOKBACK_MIN + 60:
        print(f"Historique insuffisant ({len(candles)} bougies) — réessayer.")
        return 1
    res = select(candles)

    print(f"\n=== MinuteLab scan — {config.SYMBOL} 1m, fenêtre {config.LOOKBACK_MIN} min "
          f"(récent {config.RECENT_MIN} min), mode {config.QUAL_MODE}, "
          f"{res['scanned']} stratégies, "
          f"coût {2 * (config.FEE_PCT + config.SLIPPAGE_PCT) * 100:.3f}%/trade ===\n")

    header = f"{'PnL%':>9} {'PnLrec%':>9} {'trades':>6} {'win%':>5}  stratégie"
    print(f"QUALIFIÉES (mode {config.QUAL_MODE}) :")
    if res["qualified"]:
        print(header)
        for r in res["qualified"][:top]:
            print(f"{r.pnl_pct * 100:>9.4f} {r.pnl_recent_pct * 100:>9.4f} "
                  f"{r.n_trades:>6d} {r.winrate * 100:>5.0f}  {r.strat.name}")
    else:
        print("  AUCUNE — sur cette fenêtre, rien ne bat les frais. Position : FLAT.")

    print(f"\nTop {top} toutes stratégies (même perdantes), tri par score :")
    print(header)
    for r in res["ranked"][:top]:
        print(f"{r.pnl_pct * 100:>9.4f} {r.pnl_recent_pct * 100:>9.4f} "
              f"{r.n_trades:>6d} {r.winrate * 100:>5.0f}  {r.strat.name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="MinuteLab — labo BTC 1m (paper)")
    parser.add_argument("--scan-once", action="store_true",
                        help="une sélection puis sortie (pas de boucle)")
    parser.add_argument("--top", type=int, default=15, help="lignes affichées")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose)
    if args.scan_once:
        return scan_once(args.top)

    from minutelab.engine import PaperEngine
    PaperEngine().run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())

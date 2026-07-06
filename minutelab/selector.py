"""
Sélecteur MinuteLab : passe toute la grille de stratégies au backtest sur la
fenêtre glissante (60 min par défaut) et retient celles qui sont gagnantes
NET DE FRAIS à la fois sur la fenêtre entière et sur la sous-fenêtre récente
(20 min par défaut). Si rien ne passe → FLAT (aucune stratégie appliquée),
c'est un résultat honnête, pas une erreur.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from minutelab import config
from minutelab.backtester import LabResult, run_lab_backtest
from minutelab.strategies import build_grid

logger = logging.getLogger("sdm.minutelab.selector")


def select(
    candles: List[dict],
    lookback_bars: int = None,
    recent_bars: int = None,
    min_trades: int = None,
) -> dict:
    """
    candles : bougies 1 m CLÔTURÉES, warmup inclus (≥ WARMUP_HOURS conseillé).
    Retourne {"champion": LabResult|None, "qualified": [...], "ranked": [...],
              "scanned": int, "ts": epoch}.
    """
    lookback_bars = lookback_bars or config.LOOKBACK_MIN
    recent_bars = recent_bars or config.RECENT_MIN
    min_trades = min_trades if min_trades is not None else config.MIN_TRADES

    n = len(candles)
    if n < lookback_bars + 60:
        logger.warning("historique insuffisant : %d bougies", n)
        return {"champion": None, "qualified": [], "ranked": [], "scanned": 0,
                "ts": time.time()}

    start_index = n - lookback_bars
    recent_index = n - recent_bars

    results: List[LabResult] = []
    for strat in build_grid():
        try:
            r = run_lab_backtest(
                candles, strat,
                fee_pct=config.FEE_PCT,
                slippage_pct=config.SLIPPAGE_PCT,
                start_index=start_index,
                recent_index=recent_index,
                hard_sl_pct=config.HARD_SL_PCT,
                max_hold_bars=config.MAX_HOLD_MIN,
            )
        except Exception:
            logger.exception("backtest en échec pour %s", strat.name)
            continue
        results.append(r)

    ranked = sorted(results, key=lambda r: r.score, reverse=True)
    qualified = [
        r for r in ranked
        if r.n_trades >= min_trades and r.pnl_pct > 0 and r.pnl_recent_pct > 0
    ]
    champion: Optional[LabResult] = qualified[0] if qualified else None

    logger.info(
        "sélection : %d stratégies testées, %d qualifiées, champion=%s",
        len(results), len(qualified),
        champion.strat.name if champion else "AUCUN (flat)",
    )
    return {
        "champion": champion,
        "qualified": qualified,
        "ranked": ranked,
        "scanned": len(results),
        "ts": time.time(),
    }

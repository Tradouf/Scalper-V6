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


_USE_CONFIG = object()


def select(
    candles: List[dict],
    lookback_bars: int = None,
    recent_bars: int = None,
    min_trades: int = None,
    exit_min_gain=_USE_CONFIG,
    fee_pct: float = None,
    slippage_pct: float = None,
) -> dict:
    """
    candles : bougies 1 m CLÔTURÉES, warmup inclus (≥ WARMUP_HOURS conseillé).
    lookback_bars == recent_bars → mode « fenêtre unique » : seules les N
    dernières minutes jugent la stratégie (le pouls du moment).
    Retourne {"champion": LabResult|None, "qualified": [...], "ranked": [...],
              "scanned": int, "ts": epoch}.
    """
    lookback_bars = lookback_bars or config.LOOKBACK_MIN
    recent_bars = recent_bars or config.RECENT_MIN
    min_trades = min_trades if min_trades is not None else config.MIN_TRADES
    fee_pct = config.FEE_PCT if fee_pct is None else fee_pct
    slippage_pct = config.SLIPPAGE_PCT if slippage_pct is None else slippage_pct
    if exit_min_gain is _USE_CONFIG:
        exit_min_gain = (
            2.0 * (fee_pct + slippage_pct)
            if config.EXIT_REQUIRE_NET_GAIN else None
        )

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
                fee_pct=fee_pct,
                slippage_pct=slippage_pct,
                start_index=start_index,
                recent_index=recent_index,
                hard_sl_pct=config.HARD_SL_PCT,
                max_hold_bars=config.MAX_HOLD_MIN,
                exit_min_gain=exit_min_gain,
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

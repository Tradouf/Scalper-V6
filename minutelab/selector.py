"""
Sélecteur MinuteLab : passe toute la grille de stratégies au backtest sur la
fenêtre glissante et retient celles qui passent les critères de qualification
(mode pulse / dual / legacy). Si rien ne passe → candidat None (FLAT honnête).

L'hystérésis du champion (tenure, grace, marge de score) est appliquée dans
engine.py via champion.pick_champion().
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from minutelab import config
from minutelab.backtester import LabResult, run_lab_backtest
from minutelab.champion import count_near_misses, qualify_results
from minutelab.strategies import Strat, build_grid

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
    incumbent: Optional[Strat] = None,
) -> dict:
    """
    candles : bougies 1 m CLÔTURÉES, warmup inclus (≥ WARMUP_HOURS conseillé).
    lookback_bars == recent_bars → mode « fenêtre unique » : seules les N
    dernières minutes jugent la stratégie (pouls du moment).
    incumbent : stratégie championne actuelle (informatif pour les logs).
    Retourne {"candidate": LabResult|None, "qualified": [...], "ranked": [...],
              "scanned": int, "ts": epoch, "qual_mode": str, "n_near_miss": int}.
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
        return {
            "candidate": None, "champion": None, "qualified": [], "ranked": [],
            "scanned": 0, "ts": time.time(), "qual_mode": config.QUAL_MODE,
            "n_near_miss": 0, "incumbent": incumbent.name if incumbent else None,
        }

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
    qualified = qualify_results(ranked, min_trades, lookback_bars, recent_bars)
    candidate: Optional[LabResult] = qualified[0] if qualified else None
    n_near = count_near_misses(ranked, min_trades, lookback_bars, recent_bars)

    logger.info(
        "sélection [%s] : %d stratégies testées, %d qualifiées, "
        "candidat=%s, near_miss=%d",
        config.QUAL_MODE, len(results), len(qualified),
        candidate.strat.name if candidate else "AUCUN (flat)",
        n_near,
    )
    return {
        "candidate": candidate,
        "champion": candidate,  # alias rétrocompat ; engine remplace via hysteresis
        "qualified": qualified,
        "ranked": ranked,
        "scanned": len(results),
        "ts": time.time(),
        "qual_mode": config.QUAL_MODE,
        "n_near_miss": n_near,
        "incumbent": incumbent.name if incumbent else None,
    }

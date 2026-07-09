"""
Sélection du champion MinuteLab avec hystérésis anti-churn.

Règles :
- tenure minimale avant remplacement ;
- marge de score pour battre l'incumbent ;
- grace period avant passage FLAT quand plus aucun qualifié ;
- démotion forcée si le PnL live depuis la sélection est trop mauvais.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from minutelab import config
from minutelab.backtester import LabResult
from minutelab.strategies import Strat


@dataclass
class ChampionState:
    strat: Optional[Strat] = None
    since: float = 0.0
    entry_equity: float = 0.0
    grace_misses: int = 0


def qualifies(r: LabResult, min_trades: int,
              lookback_bars: int, recent_bars: int) -> bool:
    """True si la stratégie passe les critères du mode de qualification actif."""
    mode = config.QUAL_MODE
    if mode == "legacy":
        return (
            r.n_trades >= min_trades
            and r.pnl_pct > 0
            and r.pnl_recent_pct > 0
        )
    if mode == "dual":
        return (
            r.n_trades >= min_trades
            and r.pnl_pct >= config.MIN_PNL_FULL_PCT
            and r.pnl_recent_pct >= config.MIN_PNL_RECENT_PCT
            and r.profit_factor >= config.MIN_PROFIT_FACTOR
        )
    # pulse (défaut) : fenêtre unique ou récente dominante
    if lookback_bars == recent_bars:
        return (
            r.n_trades >= min_trades
            and r.pnl_recent_pct >= config.MIN_PNL_RECENT_PCT
            and r.score >= config.MIN_SCORE_PCT
            and r.profit_factor >= config.MIN_PROFIT_FACTOR
        )
    return (
        r.n_trades >= min_trades
        and r.pnl_recent_pct >= config.MIN_PNL_RECENT_PCT
        and r.score >= config.MIN_SCORE_PCT
        and r.profit_factor >= config.MIN_PROFIT_FACTOR
    )


def qualify_results(
    ranked: List[LabResult],
    min_trades: int,
    lookback_bars: int,
    recent_bars: int,
) -> List[LabResult]:
    return [
        r for r in ranked
        if qualifies(r, min_trades, lookback_bars, recent_bars)
    ]


def near_miss_reason(r: LabResult, min_trades: int,
                     lookback_bars: int, recent_bars: int) -> Optional[str]:
    """Raison du rejet si la stratégie est proche mais non qualifiée."""
    if qualifies(r, min_trades, lookback_bars, recent_bars):
        return None
    if r.n_trades < min_trades:
        return "min_trades"
    if r.score <= 0:
        return "score_nonpos"
    mode = config.QUAL_MODE
    if mode == "legacy":
        if r.pnl_pct <= 0:
            return "pnl_full_nonpos"
        if r.pnl_recent_pct <= 0:
            return "pnl_recent_nonpos"
        return "other"
    if mode == "dual":
        if r.pnl_pct < config.MIN_PNL_FULL_PCT:
            return "pnl_full_low"
        if r.pnl_recent_pct < config.MIN_PNL_RECENT_PCT:
            return "pnl_recent_low"
        if r.profit_factor < config.MIN_PROFIT_FACTOR:
            return "pf_low"
        return "other"
    if r.pnl_recent_pct < config.MIN_PNL_RECENT_PCT:
        return "pnl_recent_low"
    if r.score < config.MIN_SCORE_PCT:
        return "score_low"
    if r.profit_factor < config.MIN_PROFIT_FACTOR:
        return "pf_low"
    return "other"


def count_near_misses(ranked: List[LabResult], min_trades: int,
                      lookback_bars: int, recent_bars: int,
                      top_n: int = 10) -> int:
    n = 0
    for r in ranked[:top_n]:
        if r.score > 0 and near_miss_reason(r, min_trades, lookback_bars, recent_bars):
            n += 1
    return n


def find_score(ranked: List[LabResult], strat: Strat) -> Optional[float]:
    for r in ranked:
        if r.strat == strat:
            return r.score
    return None


def pick_champion(
    candidate: Optional[LabResult],
    qualified: List[LabResult],
    ranked: List[LabResult],
    state: ChampionState,
    equity_pct: float,
    now: Optional[float] = None,
) -> Tuple[Optional[Strat], str, ChampionState]:
    """
    Retourne (strat_champion, raison, nouvel_état).
    Ne mute pas state en place — retourne une copie mise à jour.
    """
    now = now if now is not None else time.time()
    new_state = ChampionState(
        strat=state.strat,
        since=state.since,
        entry_equity=state.entry_equity,
        grace_misses=state.grace_misses,
    )
    inc = state.strat
    pnl_since = equity_pct - state.entry_equity

    if inc and pnl_since <= config.CHAMPION_DEMOTE_PNL_PCT:
        new_state.grace_misses = 0
        if candidate:
            new_state.strat = candidate.strat
            new_state.since = now
            new_state.entry_equity = equity_pct
            return candidate.strat, "DEMOTE_BAD_LIVE", new_state
        new_state.strat = None
        new_state.since = 0.0
        return None, "DEMOTE_FLAT", new_state

    if candidate is None and inc:
        new_state.grace_misses = state.grace_misses + 1
        if new_state.grace_misses < config.CHAMPION_GRACE_SCANS:
            return inc, f"GRACE_HOLD({new_state.grace_misses})", new_state
        new_state.grace_misses = 0
        new_state.strat = None
        new_state.since = 0.0
        return None, "GRACE_EXPIRED", new_state

    new_state.grace_misses = 0

    if candidate is None:
        new_state.strat = None
        new_state.since = 0.0
        return None, "NO_CANDIDATE", new_state

    if inc is None:
        new_state.strat = candidate.strat
        new_state.since = now
        new_state.entry_equity = equity_pct
        return candidate.strat, "NEW_CHAMPION", new_state

    if any(r.strat == inc for r in qualified):
        return inc, "INCUMBENT_OK", new_state

    tenure_sec = now - state.since
    if tenure_sec < config.CHAMPION_MIN_TENURE_MIN * 60:
        return inc, "TENURE_HOLD", new_state

    inc_sc = find_score(ranked, inc) or 0.0
    if candidate.score < inc_sc + config.CHAMPION_SCORE_MARGIN_PCT:
        return inc, "SCORE_MARGIN", new_state

    new_state.strat = candidate.strat
    new_state.since = now
    new_state.entry_equity = equity_pct
    return candidate.strat, "SWITCH_SCORE", new_state
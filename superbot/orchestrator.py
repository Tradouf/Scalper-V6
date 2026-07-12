"""
Orchestrateur SuperBot (SPEC §4) — la DOUBLE GATE.

  Gate 1 — MARCHÉ (HMM BTC 4h) : autorise/bloque les sleeves entières
    | état                | A momentum | B ema | C breakout | sizing |
    | bull_orderly        |     ✅     |  ✅   |     ✅     |  ×1.0  |
    | bear_orderly        |     ✅     |  ✅   |     ✅     |  ×1.0  |
    | range_compressed    |     ❌     |  ✅   |     ❌     |  ×0.7  |
    | high_vol_chaotic    |     ✅     |  ✅   |     ❌     |  ×0.5  |

  Gate 2 — SYMBOLE (HMM K=3) : autorise/bloque l'entrée sur CE coin
    trending_up → LONG seulement ; trending_down → SHORT seulement ;
    choppy → aucune entrée. Sizing × confiance.

  Règles combinées : transition_risk (marché OU symbole) > seuil → gel des
  nouvelles entrées ; confiance symbole < min → refus.

Les décisions sont pures (état passé en argument) → testables sans réseau.
La boucle temps réel qui alimente ces états arrive avec le live (phase
ultérieure) ; ici on fournit la logique + la priorisation des candidats.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from superbot import config

logger = logging.getLogger("sdm.superbot.orchestrator")

#: sleeves autorisées par état de marché (SPEC §4 gate 1)
SLEEVE_GATES = {
    "bull_orderly":     {"momentum": True,  "adaptive_ema": True, "breakout": True},
    "bear_orderly":     {"momentum": True,  "adaptive_ema": True, "breakout": True},
    "range_compressed": {"momentum": False, "adaptive_ema": True, "breakout": False},
    "high_vol_chaotic": {"momentum": True,  "adaptive_ema": True, "breakout": False},
}

#: multiplicateur de sizing par état de marché (SPEC §5)
MARKET_SIZE_MULT = {
    "bull_orderly": 1.0,
    "bear_orderly": 1.0,
    "range_compressed": 0.7,
    "high_vol_chaotic": 0.5,
}


def sleeve_allowed(sleeve: str, market_state: str) -> bool:
    return SLEEVE_GATES.get(market_state, {}).get(sleeve, False)


def allow_entry(signal: int, sleeve: str,
                market: Dict, symbol_regime: Dict) -> Tuple[bool, str]:
    """Double gate (SPEC §4) — ordre des refus identique à la spec."""
    if market.get("transition_risk", 1.0) > config.HMM_TRANSITION_FREEZE:
        return False, "market_transition"
    if symbol_regime.get("transition_risk", 1.0) > config.HMM_TRANSITION_FREEZE:
        return False, "symbol_transition"
    if not sleeve_allowed(sleeve, market.get("state", "")):
        return False, "sleeve_blocked"
    sym_state = symbol_regime.get("state", "")
    if sym_state == "choppy":
        return False, "hmm_choppy"
    if signal == 1 and sym_state != "trending_up":
        return False, "hmm_no_long"
    if signal == -1 and sym_state != "trending_down":
        return False, "hmm_no_short"
    if symbol_regime.get("confidence", 0.0) < config.HMM_SYMBOL_MIN_CONF:
        return False, "hmm_low_conf"
    return True, "ok"


def hmm_size_mult(signal: int, symbol_regime: Dict) -> float:
    """Sizing gate 2 : direction × confiance (SPEC §4)."""
    state = symbol_regime.get("state", "choppy")
    direction_ok = {
        "trending_up": 1.0 if signal == 1 else 0.0,
        "trending_down": 1.0 if signal == -1 else 0.0,
        "choppy": 0.0,
    }.get(state, 0.0)
    return direction_ok * float(symbol_regime.get("confidence", 0.0))


def market_size_mult(market: Dict) -> float:
    return MARKET_SIZE_MULT.get(market.get("state", ""), 0.5)


def effective_margin_pct(base_margin_pct: float, signal: int,
                         market: Dict, symbol_regime: Dict) -> float:
    """margin_pct effectif = base × mult symbole (dir×conf) × mult marché."""
    return (base_margin_pct
            * hmm_size_mult(signal, symbol_regime)
            * market_size_mult(market))


def prioritize_candidates(candidates: List[Dict]) -> List[Dict]:
    """Priorise les entrées candidates par quality_score × confiance HMM
    symbole (SPEC §4 étape 5). candidates: [{symbol, sleeve, signal,
    quality_score, symbol_regime}, ...]"""
    def key(c: Dict) -> float:
        conf = float((c.get("symbol_regime") or {}).get("confidence", 0.0))
        return float(c.get("quality_score", 0.0)) * conf
    return sorted(candidates, key=key, reverse=True)


class Orchestrator:
    """Filtre un lot de signaux candidats à travers la double gate.

    Pur et sans I/O : les régimes sont fournis par l'appelant (RegimeFacade
    en live, dicts en tests). La boucle 30s qui construit les candidats
    arrive avec le live_trader (phase ultérieure)."""

    def __init__(self, market_regime_provider=None, symbol_regime_provider=None):
        self._market = market_regime_provider
        self._symbol = symbol_regime_provider

    def filter_entries(self, candidates: List[Dict],
                       market: Optional[Dict] = None) -> List[Dict]:
        mkt = market if market is not None else (self._market() if self._market else {})
        accepted = []
        for c in prioritize_candidates(candidates):
            sym_reg = c.get("symbol_regime")
            if sym_reg is None and self._symbol is not None:
                sym_reg = self._symbol(c["symbol"])
            sym_reg = sym_reg or {}
            ok, reason = allow_entry(c["signal"], c["sleeve"], mkt, sym_reg)
            c = dict(c)
            c["gate"] = reason
            if ok:
                c["margin_mult"] = (hmm_size_mult(c["signal"], sym_reg)
                                    * market_size_mult(mkt))
                accepted.append(c)
            else:
                logger.info("%s: entrée %+d %s refusée — %s",
                            c.get("symbol"), c.get("signal"), c.get("sleeve"), reason)
        return accepted

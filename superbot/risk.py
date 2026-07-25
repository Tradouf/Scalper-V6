"""
Gestion du risque SuperBot (SPEC §5) — pur et testable, aucune I/O.

  - Kill-switch portefeuille : perte journalière -3 % → flatten + pause 12 h ;
    drawdown -8 % vs pic 7 j → pause 24 h. HYSTÉRÉSIS obligatoire
    (KILL_CONFIRMATIONS lectures consécutives — leçon incident SimpleBot 04/07 :
    une lecture aberrante isolée ne doit jamais tout fermer).
  - Sizing dynamique : MARGIN_PCT (4 %) → MARGIN_PCT_MAX (7 %) interpolé par
    quality_score normalisé sur les actifs.
  - Corrélation simplifiée : {BTC, ETH, SOL} = majors (max 2 même direction),
    reste = alts (max 4 même direction), MAX_SAME_DIRECTION global.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

from superbot import config

logger = logging.getLogger("sdm.superbot.risk")

MAJORS = {"BTC", "ETH", "SOL"}
MAX_SAME_DIR_MAJORS = 2
MAX_SAME_DIR_ALTS = 4

DAILY_PAUSE_SEC = 12 * 3600
DD_PAUSE_SEC = 24 * 3600
DD_WINDOW_SEC = 7 * 24 * 3600


class KillSwitch:
    """Décide pause/flatten à partir de l'equity — l'appelant exécute.

    check() retourne None ou {"action": "flatten_pause", "pause_sec", "reason"}.
    Les compteurs de confirmation sont par-règle et se réinitialisent dès
    qu'une lecture repasse au-dessus du seuil."""

    def __init__(self):
        self._day_anchor_ts = 0.0
        self._day_anchor_equity: Optional[float] = None
        self._peak_history: List[List[float]] = []      # [[ts, equity], ...]
        self._confirm: Dict[str, int] = {"daily": 0, "dd": 0}

    def _roll_day(self, equity: float, now: float) -> None:
        day = int(now // 86_400)
        if int(self._day_anchor_ts // 86_400) != day or self._day_anchor_equity is None:
            self._day_anchor_ts = now
            self._day_anchor_equity = equity
            self._confirm["daily"] = 0

    def check(self, equity: float, now: Optional[float] = None) -> Optional[dict]:
        if equity <= 0:
            return None                       # lecture douteuse : jamais de kill dessus
        now = time.time() if now is None else now
        self._roll_day(equity, now)

        self._peak_history.append([now, equity])
        cutoff = now - DD_WINDOW_SEC
        self._peak_history = [p for p in self._peak_history if p[0] >= cutoff]
        peak_7d = max(v for _, v in self._peak_history)

        # Règle 1 — perte journalière
        anchor = self._day_anchor_equity or equity
        if anchor > 0 and equity <= anchor * (1 - config.DAILY_LOSS_LIMIT_PCT):
            self._confirm["daily"] += 1
            if self._confirm["daily"] >= config.KILL_CONFIRMATIONS:
                self._confirm["daily"] = 0
                return {"action": "flatten_pause", "pause_sec": DAILY_PAUSE_SEC,
                        "reason": f"daily_loss {equity:.2f} <= {anchor:.2f} "
                                  f"×(1-{config.DAILY_LOSS_LIMIT_PCT:.0%})"}
            logger.warning("Kill-switch daily: confirmation %d/%d",
                           self._confirm["daily"], config.KILL_CONFIRMATIONS)
        else:
            self._confirm["daily"] = 0

        # Règle 2 — drawdown vs pic 7 j
        if peak_7d > 0 and equity <= peak_7d * (1 - config.PORTFOLIO_DD_LIMIT):
            self._confirm["dd"] += 1
            if self._confirm["dd"] >= config.KILL_CONFIRMATIONS:
                self._confirm["dd"] = 0
                return {"action": "flatten_pause", "pause_sec": DD_PAUSE_SEC,
                        "reason": f"portfolio_dd {equity:.2f} <= pic7j {peak_7d:.2f} "
                                  f"×(1-{config.PORTFOLIO_DD_LIMIT:.0%})"}
            logger.warning("Kill-switch DD: confirmation %d/%d",
                           self._confirm["dd"], config.KILL_CONFIRMATIONS)
        else:
            self._confirm["dd"] = 0
        return None


def dynamic_margin_pct(symbol: str, scores: Dict[str, float]) -> float:
    """MARGIN_PCT → MARGIN_PCT_MAX interpolé par quality_score normalisé
    (patron SimpleBot P1.4). Base si symbole inconnu ou < 2 actifs."""
    if symbol not in scores or len(scores) < 2:
        return config.MARGIN_PCT
    lo, hi = min(scores.values()), max(scores.values())
    norm = 1.0 if hi <= lo else (scores[symbol] - lo) / (hi - lo)
    pct = config.MARGIN_PCT + norm * (config.MARGIN_PCT_MAX - config.MARGIN_PCT)
    return min(config.MARGIN_PCT_MAX, max(config.MARGIN_PCT, pct))


def direction_caps_ok(symbol: str, direction: int,
                      positions: Dict[str, dict]) -> Tuple[bool, str]:
    """Caps de corrélation (SPEC §5) sur les positions OUVERTES.
    positions : {symbol: {"dir": ±1, ...}}."""
    same_dir = [s for s, p in positions.items() if p.get("dir") == direction]
    if len(same_dir) >= config.MAX_SAME_DIRECTION:
        return False, "max_same_direction"
    group_majors = symbol.upper() in MAJORS
    same_group = [s for s in same_dir if (s.upper() in MAJORS) == group_majors]
    cap = MAX_SAME_DIR_MAJORS if group_majors else MAX_SAME_DIR_ALTS
    if len(same_group) >= cap:
        return False, "majors_same_dir" if group_majors else "alts_same_dir"
    return True, "ok"

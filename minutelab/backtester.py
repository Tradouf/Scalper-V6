"""
Backtester MinuteLab — simulation bougie par bougie (1 m) avec sortie PnL/MA.

Règles d'exécution :
- un signal à la clôture de la bougie i est exécuté à l'OPEN de la bougie i+1 ;
- coût par side = FEE + SLIPPAGE (2 sides par trade), appliqué au PnL du trade ;
- sortie principale : le gain (mark-to-market, en % prix, hors coûts) croise
  sous sa moyenne mobile. En backtest 1 m le gain est échantillonné à chaque
  clôture — approximation de l'échantillonnage 5 s du moteur live ;
- garde-fous : stop dur intrabar (HARD_SL_PCT) et durée max (MAX_HOLD_MIN) ;
- position encore ouverte en fin de fenêtre → clôturée à la dernière close.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from minutelab.strategies import Strat, compute_signals


@dataclass
class LabResult:
    strat: Strat
    n_trades: int = 0
    pnl_pct: float = 0.0          # somme des pnl nets (% prix, hors levier)
    pnl_recent_pct: float = 0.0   # pnl net des trades clos dans la sous-fenêtre récente
    winrate: float = 0.0
    profit_factor: float = 0.0
    trades: List[dict] = field(default_factory=list)

    @property
    def score(self) -> float:
        # Le récent pèse double : on cherche ce qui gagne MAINTENANT.
        return self.pnl_recent_pct + 0.5 * self.pnl_pct


def run_lab_backtest(
    candles: List[dict],
    strat: Strat,
    fee_pct: float,
    slippage_pct: float,
    start_index: int,
    recent_index: int,
    hard_sl_pct: float = 0.004,
    max_hold_bars: int = 30,
    exit_min_gain: float = None,
) -> LabResult:
    """
    start_index   : les entrées avant cet index sont ignorées (fenêtre d'éval).
    recent_index  : les trades clos à partir de cet index comptent dans pnl_recent.
    exit_min_gain : si non-None, le croisement PnL/MA ne sort que lorsque le
                    gain brut dépasse ce seuil (typiquement le coût aller-retour) ;
                    en dessous, seuls le stop dur et la durée max coupent.
    """
    signals = compute_signals(candles, strat)
    cost = 2.0 * (fee_pct + slippage_pct)

    trades: List[dict] = []
    pos = None       # {"dir", "entry", "bar", "gains": [échantillons de gain]}
    pending = None   # direction à exécuter à l'open suivant

    def close_trade(exit_px: float, reason: str, bar: int) -> None:
        pnl = pos["dir"] * (exit_px - pos["entry"]) / pos["entry"] - cost
        trades.append({
            "dir": pos["dir"], "entry": pos["entry"], "exit": exit_px,
            "pnl_pct": pnl, "reason": reason,
            "entry_bar": pos["bar"], "exit_bar": bar,
        })

    for i, c in enumerate(candles):
        # 1) Exécution du signal en attente à l'open
        if pending is not None:
            direction, pending = pending, None
            if pos is None:
                pos = {"dir": direction, "entry": c["open"], "bar": i, "gains": []}

        # 2) Gestion de la position ouverte
        if pos is not None and i > pos["bar"]:
            # Stop dur intrabar (pessimiste : exécuté au niveau du stop)
            stop_px = pos["entry"] * (1 - pos["dir"] * hard_sl_pct)
            hit = c["low"] <= stop_px if pos["dir"] == 1 else c["high"] >= stop_px
            if hit:
                close_trade(stop_px, "HARD_SL", i)
                pos = None
            else:
                g = pos["dir"] * (c["close"] - pos["entry"]) / pos["entry"]
                gains = pos["gains"]
                gains.append(g)
                mb = strat.exit_ma_bars
                if len(gains) >= 2:
                    ma_now = sum(gains[-mb:]) / min(mb, len(gains))
                    prev = gains[:-1]
                    ma_prev = sum(prev[-mb:]) / min(mb, len(prev))
                    crossed = gains[-2] >= ma_prev and g < ma_now
                    if crossed and exit_min_gain is not None and g <= exit_min_gain:
                        crossed = False
                    if crossed or (i - pos["bar"]) >= max_hold_bars:
                        close_trade(c["close"], "PNL_MA" if crossed else "MAX_HOLD", i)
                        pos = None

        # 3) Nouveau signal à la clôture (une position à la fois, pas de flip)
        if pos is None and pending is None and i >= start_index and signals[i] != 0:
            pending = signals[i]

    if pos is not None:
        close_trade(candles[-1]["close"], "EOW", len(candles) - 1)

    return _result(strat, trades, recent_index)


def _result(strat: Strat, trades: List[dict], recent_index: int) -> LabResult:
    if not trades:
        return LabResult(strat=strat)
    pnls = [t["pnl_pct"] for t in trades]
    winners = [p for p in pnls if p > 0]
    losses = abs(sum(p for p in pnls if p < 0))
    gross = sum(winners)
    pf = gross / losses if losses > 0 else (999.0 if gross > 0 else 0.0)
    return LabResult(
        strat=strat,
        n_trades=len(trades),
        pnl_pct=sum(pnls),
        pnl_recent_pct=sum(t["pnl_pct"] for t in trades if t["exit_bar"] >= recent_index),
        winrate=len(winners) / len(trades),
        profit_factor=pf,
        trades=trades,
    )

"""
Backtester SimpleBot — simulation bougie par bougie de la stratégie EMA/ATR.

Règles d'exécution (identiques au live) :
- un signal à la clôture de la bougie i est exécuté à l'OPEN de la bougie i+1 ;
- TP/SL posés à l'entrée en multiples de l'ATR de la bougie de signal ;
- si TP et SL sont touchables dans la même bougie, le SL est retenu (pessimiste) ;
- signal opposé → flip : clôture à l'open puis réouverture dans l'autre sens ;
- coût par side = FEE + SLIPPAGE, soit 2 sides par trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from simplebot.strategy import StrategyParams, atr, compute_signals


@dataclass
class BacktestResult:
    params: StrategyParams
    n_trades: int = 0
    total_pnl_pct: float = 0.0     # somme des pnl en % prix (non levier)
    winrate: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    trades: List[dict] = field(default_factory=list)

    def metrics(self) -> dict:
        return {
            "n_trades": self.n_trades,
            "total_pnl_pct": round(self.total_pnl_pct, 4),
            "winrate": round(self.winrate, 3),
            "profit_factor": round(self.profit_factor, 3),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
        }


def run_backtest(
    candles: List[dict],
    params: StrategyParams,
    fee_pct: float,
    slippage_pct: float,
    start_index: int = 0,
) -> BacktestResult:
    """
    start_index : les signaux avant cet index sont ignorés — permet de passer
    une fenêtre de validation avec son préfixe de warmup sans compter les
    trades du train.
    """
    signals = compute_signals(candles, params)
    atr_v = atr(candles, params.atr_len)
    cost = 2.0 * (fee_pct + slippage_pct)

    trades: List[dict] = []
    pos = None       # {"dir", "entry", "sl", "tp", "bar"}
    pending = None   # (direction, atr_ref) → exécuté à l'open suivant

    def close_trade(exit_px: float, reason: str, bar: int) -> None:
        pnl = pos["dir"] * (exit_px - pos["entry"]) / pos["entry"] - cost
        trades.append({
            "dir": pos["dir"],
            "entry": pos["entry"],
            "exit": exit_px,
            "pnl_pct": pnl,
            "reason": reason,
            "entry_bar": pos["bar"],
            "exit_bar": bar,
        })

    for i, c in enumerate(candles):
        # 1) Exécution du signal en attente à l'open
        if pending is not None:
            direction, atr_ref = pending
            pending = None
            if pos is not None and pos["dir"] != direction:
                close_trade(c["open"], "FLIP", i)
                pos = None
            if pos is None and atr_ref > 0:
                entry = c["open"]
                pos = {
                    "dir": direction,
                    "entry": entry,
                    "sl": entry - direction * params.sl_atr * atr_ref,
                    "tp": entry + direction * params.tp_atr * atr_ref,
                    "bar": i,
                }

        # 2) TP/SL intrabar (SL prioritaire si les deux sont touchables)
        if pos is not None:
            if pos["dir"] == 1:
                if c["low"] <= pos["sl"]:
                    close_trade(pos["sl"], "SL", i)
                    pos = None
                elif c["high"] >= pos["tp"]:
                    close_trade(pos["tp"], "TP", i)
                    pos = None
            else:
                if c["high"] >= pos["sl"]:
                    close_trade(pos["sl"], "SL", i)
                    pos = None
                elif c["low"] <= pos["tp"]:
                    close_trade(pos["tp"], "TP", i)
                    pos = None

        # 3) Nouveau signal à la clôture de la bougie i
        s = signals[i]
        if s != 0 and i >= start_index and (pos is None or pos["dir"] != s):
            pending = (s, atr_v[i])

    # Position encore ouverte en fin de fenêtre → clôture à la dernière close
    if pos is not None:
        close_trade(candles[-1]["close"], "EOW", len(candles) - 1)

    return _compute_result(params, trades)


def _compute_result(params: StrategyParams, trades: List[dict]) -> BacktestResult:
    if not trades:
        return BacktestResult(params=params)

    pnls = [t["pnl_pct"] for t in trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    if gross_loss > 0:
        pf = gross_profit / gross_loss
    else:
        pf = 999.0 if gross_profit > 0 else 0.0

    cum = peak = max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    return BacktestResult(
        params=params,
        n_trades=len(trades),
        total_pnl_pct=sum(pnls),
        winrate=len(winners) / len(trades),
        profit_factor=pf,
        max_drawdown_pct=max_dd,
        trades=trades,
    )

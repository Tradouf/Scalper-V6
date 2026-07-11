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
    entry_mode: str = "taker",
    maker_fee_pct: float = 0.00015,
) -> BacktestResult:
    """
    start_index : les signaux avant cet index sont ignorés — permet de passer
    une fenêtre de validation avec son préfixe de warmup sans compter les
    trades du train.

    entry_mode :
      - "taker" (défaut) : entrée market à l'open suivant, coût fee+slippage
        par side — comportement historique, résultats inchangés ;
      - "maker" : modèle DÉTERMINISTE de l'exécution maker-first live (limit
        Alo au close du signal, timeout → market). Fill maker si la bougie
        d'exécution revient toucher le limit :
          open ≤ limit (gap favorable)  → fill maker à l'open ;
          low < limit (pénétration)     → fill maker au limit, MAIS le TP est
             interdit sur cette même bougie (le chemin intrabar est inconnu —
             créditer low→high fabrique un mirage, cf. R&D 07/2026) ; le SL
             même-bougie reste actif (pessimiste) ;
          sinon                         → fallback market à l'open (taker).
        La sortie (TP/SL natifs déclenchés) reste taker dans tous les cas.
    """
    signals = compute_signals(candles, params)
    atr_v = atr(candles, params.atr_len)
    taker_side = fee_pct + slippage_pct
    maker_side = maker_fee_pct

    trades: List[dict] = []
    pos = None       # {"dir", "entry", "sl", "tp", "bar", "entry_cost", "no_tp_bar"}
    pending = None   # (direction, atr_ref, ref_close) → exécuté à l'open suivant

    def close_trade(exit_px: float, reason: str, bar: int) -> None:
        cost = pos["entry_cost"] + taker_side
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
            direction, atr_ref, ref_close = pending
            pending = None
            if pos is not None and pos["dir"] != direction:
                close_trade(c["open"], "FLIP", i)
                pos = None
            if pos is None and atr_ref > 0:
                no_tp_bar = -1
                if entry_mode == "maker":
                    lim = ref_close
                    crosses = c["open"] <= lim if direction == 1 else c["open"] >= lim
                    pierces = c["low"] < lim if direction == 1 else c["high"] > lim
                    if crosses:
                        entry, entry_cost = c["open"], maker_side
                    elif pierces:
                        entry, entry_cost = lim, maker_side
                        no_tp_bar = i          # fill mid-bar → pas de TP cette bougie
                    else:
                        entry, entry_cost = c["open"], taker_side   # fallback market
                else:
                    entry, entry_cost = c["open"], taker_side
                pos = {
                    "dir": direction,
                    "entry": entry,
                    "sl": entry - direction * params.sl_atr * atr_ref,
                    "tp": entry + direction * params.tp_atr * atr_ref,
                    "bar": i,
                    "entry_cost": entry_cost,
                    "no_tp_bar": no_tp_bar,
                }

        # 2) TP/SL intrabar (SL prioritaire si les deux sont touchables)
        if pos is not None:
            tp_allowed = i != pos["no_tp_bar"]
            if pos["dir"] == 1:
                if c["low"] <= pos["sl"]:
                    close_trade(pos["sl"], "SL", i)
                    pos = None
                elif tp_allowed and c["high"] >= pos["tp"]:
                    close_trade(pos["tp"], "TP", i)
                    pos = None
            else:
                if c["high"] >= pos["sl"]:
                    close_trade(pos["sl"], "SL", i)
                    pos = None
                elif tp_allowed and c["low"] <= pos["tp"]:
                    close_trade(pos["tp"], "TP", i)
                    pos = None

        # 3) Nouveau signal à la clôture de la bougie i
        s = signals[i]
        if s != 0 and i >= start_index and (pos is None or pos["dir"] != s):
            pending = (s, atr_v[i], c["close"])

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

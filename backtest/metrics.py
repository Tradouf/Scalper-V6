"""
Métriques de backtest : Sharpe, max drawdown, turnover, attribution.

Calculs sur une série temporelle d'equity + un journal de fills/PnL.
"""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class BacktestMetrics:
    """Résumé statistique d'un backtest."""

    n_bars: int = 0
    n_fills: int = 0
    initial_equity: float = 0.0
    final_equity: float = 0.0
    total_return_pct: float = 0.0
    sharpe_annualized: float = 0.0
    max_drawdown_pct: float = 0.0
    turnover_total_usd: float = 0.0
    total_fees_usd: float = 0.0
    total_funding_usd: float = 0.0
    # Par stratégie
    pnl_by_strategy: Dict[str, float] = field(default_factory=dict)
    fills_by_strategy: Dict[str, int] = field(default_factory=dict)
    fees_by_strategy: Dict[str, float] = field(default_factory=dict)

    def print_report(self) -> str:
        lines = []
        lines.append("=" * 60)
        lines.append("BACKTEST METRICS")
        lines.append("=" * 60)
        lines.append(f"Bars                 : {self.n_bars}")
        lines.append(f"Fills                : {self.n_fills}")
        lines.append(f"Initial equity       : ${self.initial_equity:.2f}")
        lines.append(f"Final equity         : ${self.final_equity:.2f}")
        lines.append(f"Total return         : {self.total_return_pct:.2%}")
        lines.append(f"Sharpe (annualized)  : {self.sharpe_annualized:.2f}")
        lines.append(f"Max drawdown         : {self.max_drawdown_pct:.2%}")
        lines.append(f"Turnover total       : ${self.turnover_total_usd:.0f}")
        lines.append(f"Fees total           : ${self.total_fees_usd:.2f}")
        lines.append(f"Funding total        : ${self.total_funding_usd:.2f}")
        lines.append("")
        lines.append("PnL par stratégie :")
        for s in sorted(self.pnl_by_strategy.keys()):
            n = self.fills_by_strategy.get(s, 0)
            pnl = self.pnl_by_strategy[s]
            f = self.fees_by_strategy.get(s, 0.0)
            net = pnl - f
            lines.append(f"  {s:18s}  fills={n:4d}  pnl=${pnl:+8.2f}  fees=${f:6.2f}  NET=${net:+8.2f}")
        return "\n".join(lines)


def sharpe_annualized(equity_curve: List[float], bars_per_year: float = 365 * 24) -> float:
    """Sharpe ratio annualisé à partir d'une série d'equity bar-par-bar.

    bars_per_year : pour 1h candles = 365 × 24 = 8760.
    Suppose taux sans risque ~ 0.
    """
    if len(equity_curve) < 2:
        return 0.0
    rets = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]
        if prev <= 0:
            continue
        rets.append((equity_curve[i] - prev) / prev)
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    if var <= 0:
        return 0.0
    sd = math.sqrt(var)
    if sd == 0:
        return 0.0
    return mean / sd * math.sqrt(bars_per_year)


def max_drawdown(equity_curve: List[float]) -> float:
    """Max drawdown en %, retourné comme valeur positive ∈ [0, 1]."""
    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        peak = max(peak, v)
        if peak <= 0:
            continue
        dd = (peak - v) / peak
        max_dd = max(max_dd, dd)
    return max_dd


def compute_metrics(
    equity_curve: List[float],
    fills_log: List[dict],
    funding_log: List[dict] = None,
    bars_per_year: float = 365 * 24,
) -> BacktestMetrics:
    """Calcul des métriques à partir des séries.

    equity_curve : equity à chaque bar.
    fills_log : list de dicts {asset, notional, fee, strategy_id, closedPnl}
    funding_log : list de dicts {amount} (signed)
    """
    m = BacktestMetrics()
    m.n_bars = len(equity_curve)
    m.n_fills = len(fills_log)
    if equity_curve:
        m.initial_equity = equity_curve[0]
        m.final_equity = equity_curve[-1]
        if m.initial_equity > 0:
            m.total_return_pct = (m.final_equity - m.initial_equity) / m.initial_equity
    m.sharpe_annualized = sharpe_annualized(equity_curve, bars_per_year)
    m.max_drawdown_pct = max_drawdown(equity_curve)

    pnl_by_strat: Dict[str, float] = defaultdict(float)
    fills_by_strat: Dict[str, int] = defaultdict(int)
    fees_by_strat: Dict[str, float] = defaultdict(float)
    turnover = 0.0
    total_fees = 0.0
    for f in fills_log:
        notional = float(f.get("notional", 0))
        fee = float(f.get("fee", 0))
        strat = f.get("strategy_id") or "_unknown"
        pnl = float(f.get("closedPnl", 0))
        turnover += abs(notional)
        total_fees += fee
        pnl_by_strat[strat] += pnl
        fills_by_strat[strat] += 1
        fees_by_strat[strat] += fee
    m.pnl_by_strategy = dict(pnl_by_strat)
    m.fills_by_strategy = dict(fills_by_strat)
    m.fees_by_strategy = dict(fees_by_strat)
    m.turnover_total_usd = turnover
    m.total_fees_usd = total_fees

    if funding_log:
        m.total_funding_usd = sum(float(f.get("amount", 0)) for f in funding_log)

    return m

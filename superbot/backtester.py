"""
Backtester unifié SuperBot (SPEC §7) — UN moteur pour les trois sleeves.

Adapté de simplebot/backtester.py avec généralisations :
  - la sleeve fournit signaux + politique de sortie (TP optionnel, time-exit) ;
  - TP absent (momentum) et time-exit supportés nativement ;
  - entry_mode="maker" par défaut : modèle de fill DÉTERMINISTE hérité des
    leçons R&D 07/2026 — fill maker si la bougie d'exécution revient sur le
    limit (posé au close du signal), et TP INTERDIT dans la bougie d'un fill
    mid-bar (le chemin intrabar est inconnu : créditer low→high fabrique des
    mirages de +100 %). SL même-bougie toujours actif (pessimiste).
  - la sortie est toujours facturée taker (TP/SL natifs déclenchés = market).

Règles d'exécution héritées :
  - signal à la clôture de la bougie i → exécution à la bougie i+1 ;
  - SL prioritaire si TP et SL touchables dans la même bougie ;
  - signal opposé → flip (clôture à l'open puis réouverture) ;
  - position ouverte en fin de fenêtre → clôturée à la dernière close (EOW).

Le funding horaire (Sleeve A) sera ajouté en Phase 3 — hook prévu.
"""

from __future__ import annotations

from typing import List, Optional

from simplebot.backtester import BacktestResult
from simplebot.strategy import atr

from superbot import config
from superbot.sleeves.base import ExitPolicy, Sleeve


def run_sleeve_backtest(
    sleeve: Sleeve,
    candles: List[dict],
    params: object,
    start_index: int = 0,
    entry_mode: Optional[str] = None,
    fee_pct: Optional[float] = None,
    slippage_pct: Optional[float] = None,
    maker_fee_pct: Optional[float] = None,
) -> BacktestResult:
    entry_mode = entry_mode or config.ENTRY_MODE
    fee_pct = config.FEE_PCT if fee_pct is None else fee_pct
    slippage_pct = config.SLIPPAGE_PCT if slippage_pct is None else slippage_pct
    maker_fee_pct = config.FEE_MAKER_PCT if maker_fee_pct is None else maker_fee_pct

    policy = sleeve.exit_policy(params)
    signals = sleeve.signals(candles, params)
    atr_v = atr(candles, policy.atr_len)

    taker_side = fee_pct + slippage_pct
    maker_side = maker_fee_pct

    trades: List[dict] = []
    pos = None       # {"dir","entry","sl","tp","bar","entry_cost","no_tp_bar"}
    pending = None   # (direction, atr_ref, ref_close)

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
        # 1) Exécution du signal en attente
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
                tp = (entry + direction * policy.tp_atr * atr_ref
                      if policy.tp_atr is not None else None)
                pos = {
                    "dir": direction,
                    "entry": entry,
                    "sl": entry - direction * policy.sl_atr * atr_ref,
                    "tp": tp,
                    "bar": i,
                    "entry_cost": entry_cost,
                    "no_tp_bar": no_tp_bar,
                }

        # 2) Sorties : SL prioritaire, puis TP (si présent), puis time-exit
        if pos is not None:
            d = pos["dir"]
            tp_allowed = pos["tp"] is not None and i != pos["no_tp_bar"]
            exited = False
            if d == 1:
                if c["low"] <= pos["sl"]:
                    close_trade(pos["sl"], "SL", i); pos = None; exited = True
                elif tp_allowed and c["high"] >= pos["tp"]:
                    close_trade(pos["tp"], "TP", i); pos = None; exited = True
            else:
                if c["high"] >= pos["sl"]:
                    close_trade(pos["sl"], "SL", i); pos = None; exited = True
                elif tp_allowed and c["low"] <= pos["tp"]:
                    close_trade(pos["tp"], "TP", i); pos = None; exited = True
            if (not exited and policy.time_exit_bars is not None
                    and i - pos["bar"] >= policy.time_exit_bars):
                close_trade(c["close"], "TIME", i); pos = None

        # 3) Nouveau signal à la clôture de la bougie i
        s = signals[i]
        if s != 0 and i >= start_index and (pos is None or pos["dir"] != s):
            pending = (s, atr_v[i], c["close"])

    if pos is not None:
        close_trade(candles[-1]["close"], "EOW", len(candles) - 1)

    return _result(trades)


def _result(trades: List[dict]) -> BacktestResult:
    if not trades:
        return BacktestResult(params=None)
    pnls = [t["pnl_pct"] for t in trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    cum = peak = max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return BacktestResult(
        params=None,
        n_trades=len(trades),
        total_pnl_pct=sum(pnls),
        winrate=len(winners) / len(trades),
        profit_factor=pf,
        max_drawdown_pct=max_dd,
        trades=trades,
    )

"""
Walk-forward MinuteLab : rejoue le cycle complet « sélection → application »
sur l'historique, sans regarder le futur.

À chaque pas de RESELECT minutes :
1. sélection sur les LOOKBACK dernières minutes (critère : gagnant net sur la
   fenêtre ET sur les RECENT dernières minutes) ;
2. si un champion existe, il est appliqué (paper) sur les RESELECT minutes
   SUIVANTES — entrées sur ses signaux, sortie PnL/MA, position clôturée au
   pas suivant si encore ouverte.

C'est le test honnête du système : le PnL agrégé est 100 % out-of-sample.

    python -m minutelab.walkforward --hours 24 --step 15
"""

from __future__ import annotations

import argparse
import sys
from typing import List

from minutelab import config
from minutelab.backtester import run_lab_backtest
from minutelab.champion import ChampionState, pick_champion
from minutelab.data1m import fetch_recent_1m
from minutelab.selector import select
from minutelab.strategies import Strat

# Tranche d'historique passée au sélecteur : warmup indicateurs + lookback.
_WARMUP_BARS = 360


def walk_forward(
    candles: List[dict],
    step_min: int,
    start_bar: int,
    lookback_min: int = None,
    recent_min: int = None,
    min_trades: int = None,
    exit_min_gain=None,
    fee_pct: float = None,
    slippage_pct: float = None,
    hysteresis: bool = True,
) -> dict:
    lookback_min = lookback_min or config.LOOKBACK_MIN
    recent_min = recent_min or config.RECENT_MIN
    fee_pct = config.FEE_PCT if fee_pct is None else fee_pct
    slippage_pct = config.SLIPPAGE_PCT if slippage_pct is None else slippage_pct
    slice_bars = _WARMUP_BARS + lookback_min

    steps = []
    n = len(candles)
    champ_state = ChampionState()
    equity_pct = 0.0
    for t in range(start_bar, n - step_min, step_min):
        window = candles[max(0, t - slice_bars):t]
        res = select(window, lookback_bars=lookback_min, recent_bars=recent_min,
                     min_trades=min_trades, exit_min_gain=exit_min_gain,
                     fee_pct=fee_pct, slippage_pct=slippage_pct,
                     incumbent=champ_state.strat)
        if hysteresis:
            strat, _reason, champ_state = pick_champion(
                res["candidate"], res["qualified"], res["ranked"],
                champ_state, equity_pct, now=candles[t]["ts"] / 1000.0,
            )
            active_strat: Strat | None = strat
        else:
            active_strat = res["candidate"].strat if res["candidate"] else None
            champ_state = ChampionState(
                strat=active_strat,
                since=candles[t]["ts"] / 1000.0 if active_strat else 0.0,
                entry_equity=equity_pct,
            )
        step = {
            "bar": t,
            "ts": candles[t]["ts"],
            "champion": active_strat.name if active_strat else None,
            "pnl_pct": 0.0,
            "n_trades": 0,
        }
        if active_strat is not None:
            fwd_slice = candles[max(0, t - slice_bars):t + step_min]
            start_index = len(fwd_slice) - step_min
            r = run_lab_backtest(
                fwd_slice, active_strat,
                fee_pct=fee_pct,
                slippage_pct=slippage_pct,
                start_index=start_index,
                recent_index=start_index,
                hard_sl_pct=config.HARD_SL_PCT,
                max_hold_bars=config.MAX_HOLD_MIN,
                exit_min_gain=exit_min_gain,
            )
            fwd_trades = [x for x in r.trades if x["entry_bar"] >= start_index]
            step["pnl_pct"] = sum(x["pnl_pct"] for x in fwd_trades)
            step["n_trades"] = len(fwd_trades)
            equity_pct += step["pnl_pct"]
        steps.append(step)

    active = [s for s in steps if s["champion"]]
    traded = [s for s in active if s["n_trades"] > 0]
    total = sum(s["pnl_pct"] for s in steps)
    return {
        "steps": steps,
        "n_steps": len(steps),
        "n_active": len(active),
        "n_traded": len(traded),
        "n_trades": sum(s["n_trades"] for s in steps),
        "total_pnl_pct": total,
        "win_steps": sum(1 for s in traded if s["pnl_pct"] > 0),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Walk-forward MinuteLab (OOS)")
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--step", type=int, default=config.RESELECT_START_MIN)
    parser.add_argument("--lookback", type=int, default=config.LOOKBACK_MIN,
                        help="fenêtre de sélection (min) ; == --recent pour le mode fenêtre unique")
    parser.add_argument("--recent", type=int, default=config.RECENT_MIN)
    parser.add_argument("--min-trades", type=int, default=config.MIN_TRADES)
    parser.add_argument("--no-gate", action="store_true",
                        help="désactive la règle « le PnL/MA ne coupe que si gain > frais »")
    parser.add_argument("--zero-cost", action="store_true",
                        help="frais et slippage à zéro (mesure du signal brut)")
    parser.add_argument("--no-hysteresis", action="store_true",
                        help="désactive l'hystérésis champion (comportement brut qualified[0])")
    args = parser.parse_args()

    fee = 0.0 if args.zero_cost else config.FEE_PCT
    slip = 0.0 if args.zero_cost else config.SLIPPAGE_PCT
    gate = None if args.no_gate else 2.0 * (fee + slip)
    slice_bars = _WARMUP_BARS + args.lookback

    fetch_hours = args.hours + slice_bars / 60.0 + 1
    candles = fetch_recent_1m(config.SYMBOL, fetch_hours)
    need = int(args.hours * 60) + slice_bars
    if len(candles) < need:
        print(f"Historique insuffisant : {len(candles)} bougies, besoin {need}.")
        return 1
    start_bar = len(candles) - int(args.hours * 60)

    wf = walk_forward(candles, args.step, start_bar,
                      lookback_min=args.lookback, recent_min=args.recent,
                      min_trades=args.min_trades, exit_min_gain=gate,
                      fee_pct=fee, slippage_pct=slip,
                      hysteresis=not args.no_hysteresis)

    print(f"\n=== Walk-forward {config.SYMBOL} 1m — {args.hours:.0f} h, "
          f"fenêtre {args.lookback}/{args.recent} min, "
          f"réévaluation toutes les {args.step} min, "
          f"coût {2 * (fee + slip) * 100:.3f}%/trade, "
          f"gate sortie {'ON' if gate is not None else 'off'} ===")
    print(f"Pas de temps           : {wf['n_steps']}")
    print(f"Pas avec champion      : {wf['n_active']} "
          f"({wf['n_active'] / max(1, wf['n_steps']) * 100:.0f}% — le reste FLAT)")
    print(f"Pas ayant tradé        : {wf['n_traded']} "
          f"(gagnants : {wf['win_steps']})")
    print(f"Trades totaux          : {wf['n_trades']}")
    print(f"PnL net total (OOS)    : {wf['total_pnl_pct'] * 100:+.4f}% prix "
          f"(hors levier)")

    worst = sorted((s for s in wf["steps"] if s["n_trades"]), key=lambda s: s["pnl_pct"])
    if worst:
        print("\nPires / meilleurs pas :")
        for s in worst[:3] + worst[-3:]:
            print(f"  {s['pnl_pct'] * 100:+.4f}% ({s['n_trades']} trades) {s['champion']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

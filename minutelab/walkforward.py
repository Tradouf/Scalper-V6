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
from minutelab.data1m import fetch_recent_1m
from minutelab.selector import select

# Tranche d'historique passée au sélecteur : warmup indicateurs + lookback.
_SLICE_BARS = 360 + config.LOOKBACK_MIN


def walk_forward(candles: List[dict], step_min: int, start_bar: int) -> dict:
    steps = []
    n = len(candles)
    for t in range(start_bar, n - step_min, step_min):
        window = candles[max(0, t - _SLICE_BARS):t]
        res = select(window)
        champ = res["champion"]
        step = {
            "bar": t,
            "ts": candles[t]["ts"],
            "champion": champ.strat.name if champ else None,
            "pnl_pct": 0.0,
            "n_trades": 0,
        }
        if champ is not None:
            fwd_slice = candles[max(0, t - _SLICE_BARS):t + step_min]
            start_index = len(fwd_slice) - step_min
            r = run_lab_backtest(
                fwd_slice, champ.strat,
                fee_pct=config.FEE_PCT,
                slippage_pct=config.SLIPPAGE_PCT,
                start_index=start_index,
                recent_index=start_index,
                hard_sl_pct=config.HARD_SL_PCT,
                max_hold_bars=config.MAX_HOLD_MIN,
            )
            fwd_trades = [x for x in r.trades if x["entry_bar"] >= start_index]
            step["pnl_pct"] = sum(x["pnl_pct"] for x in fwd_trades)
            step["n_trades"] = len(fwd_trades)
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
    args = parser.parse_args()

    fetch_hours = args.hours + _SLICE_BARS / 60.0 + 1
    candles = fetch_recent_1m(config.SYMBOL, fetch_hours)
    need = int(args.hours * 60) + _SLICE_BARS
    if len(candles) < need:
        print(f"Historique insuffisant : {len(candles)} bougies, besoin {need}.")
        return 1
    start_bar = len(candles) - int(args.hours * 60)

    wf = walk_forward(candles, args.step, start_bar)

    print(f"\n=== Walk-forward {config.SYMBOL} 1m — {args.hours:.0f} h, "
          f"réévaluation toutes les {args.step} min, "
          f"coût {2 * (config.FEE_PCT + config.SLIPPAGE_PCT) * 100:.3f}%/trade ===")
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

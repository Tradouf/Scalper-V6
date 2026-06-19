#!/usr/bin/env python3
"""
Test des SORTIES alternatives (hypothèse francois 2026-06-18) : « la sortie en TP
n'est peut-être pas la bonne — avant de jeter, on essaie ».

Tous les signaux du sprint ont été jugés avec UNE seule sortie (barrière TP/SL).
Ici on rejoue un signal donné avec CHAQUE mode de sortie (tp_sl / reverse / time /
trail) au walk-forward OOS. Si un signal ne devient profitable qu'avec un TP pile
réglé → overfit. Si un edge réel existe, il devrait survivre à plusieurs sorties.

Discipline : chaque mode est jugé SÉPARÉMENT par le gate (pas de sélection du
meilleur mode → pas de multi-testing caché). On rapporte le verdict de chacun.

Usage :
    source .venv/bin/activate
    python3 backtest/run_exit_variation.py --symbol BTC --strategy trend
    python3 backtest/run_exit_variation.py --symbol ETH --strategy ao --interval 1h --days 200
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pandas as pd

from backtest.backtester import Backtester
from backtest.evaluator import WalkForwardEvaluator
from backtest.run_ao_sweep import _OHLCVClient
from execution.hyperliquid_adapter import HyperliquidReadAdapter


def combos_for(mode: str) -> list[dict]:
    """Grille de params propre à chaque mode de sortie (modeste : multi-testing)."""
    if mode == "tp_sl":
        return [{"exit_mode": "tp_sl", "tp_pct": tp, "sl_pct": tp / r}
                for tp in (0.01, 0.02, 0.03) for r in (1.0, 2.0)]
    if mode == "reverse":
        # sortie sur signal inverse : pas de tp/sl (barrières neutralisées hautes).
        return [{"exit_mode": "reverse", "tp_pct": 9.9, "sl_pct": 0.0}]
    if mode == "time":
        return [{"exit_mode": "time", "tp_pct": 9.9, "sl_pct": 0.0, "hold_bars": h}
                for h in (6, 12, 24, 48)]
    if mode == "trail":
        return [{"exit_mode": "trail", "tp_pct": 9.9, "sl_pct": 0.0, "trail_pct": t}
                for t in (0.01, 0.02, 0.04)]
    raise ValueError(mode)


def main() -> None:
    ap = argparse.ArgumentParser(description="Sorties alternatives au walk-forward OOS")
    ap.add_argument("--symbol", default="BTC")
    ap.add_argument("--strategy", default="trend",
                    choices=["trend", "ao", "ao_zerocross", "momentum",
                             "cvd_divergence", "cvd_breakout"])
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--folds", type=int, default=6)
    ap.add_argument("--fast", type=int, default=5)
    ap.add_argument("--slow", type=int, default=34)
    args = ap.parse_args()

    client = _OHLCVClient(HyperliquidReadAdapter())
    rows = client.get_ohlcv(args.symbol, interval=args.interval, days=args.days)
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).astype(
        {"open": float, "high": float, "low": float, "close": float, "volume": float})
    print(f"\nSorties alternatives — {args.symbol} {args.interval} {len(df)} barres, "
          f"signal={args.strategy} (fast={args.fast} slow={args.slow})\n")

    ev = WalkForwardEvaluator(Backtester(client))
    results = []
    for mode in ("tp_sl", "reverse", "time", "trail"):
        combos = combos_for(mode)
        # fast/slow passés via combos pour les signaux MA/AO.
        for c in combos:
            c.setdefault("fast", args.fast)
            c.setdefault("slow", args.slow)
        rep = ev.evaluate(df, args.symbol, args.strategy, combos, n_folds=args.folds)
        results.append((mode, rep))
        tag = "✅ PASS" if rep.passed else "❌ REJET"
        print(f"── sortie={mode:>8} : OOS {rep.oos_total_pnl:+7.2f}%  "
              f"folds+={rep.positive_folds}/{rep.n_folds}  PF={rep.oos_median_pf:.2f}  "
              f"t={rep.oos_tstat:.2f}  trades={rep.oos_total_trades}  {tag}")
        if not rep.passed:
            print(f"            ({', '.join(rep.gate_reasons)})")

    print("\n" + "=" * 70)
    winners = [m for m, r in results if r.passed]
    if winners:
        print(f"VERDICT : sortie(s) qui PASSENT le gate → {winners}  "
              f"(à creuser, mais 1 PASS sur 4 modes peut être chance — vérifier multi-symbole)")
    else:
        print("VERDICT : AUCUNE sortie ne passe le gate → le problème n'est PAS la sortie, "
              "c'est l'absence de signal. La sortie TP/SL n'était pas le coupable.")


if __name__ == "__main__":
    main()

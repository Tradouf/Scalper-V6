#!/usr/bin/env python3
"""
Lance le backtester walk-forward V7 sur les 6 mois de candles historiques.

Usage :
  cd ~/SalleDesMarches_v7
  python3 scripts/run_backtest.py [INITIAL_EQUITY]   # default 1000.0
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backtest.engine import Backtester, load_market_data
from core.config import load_config


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    initial = float(sys.argv[1]) if len(sys.argv) > 1 else 1000.0

    cfg = load_config()
    print(f"[bt] symbols: {cfg.symbols}")
    print(f"[bt] initial equity: ${initial:.2f}")

    parquet_dir = REPO / "data" / "historical"
    candles = load_market_data(cfg.symbols, parquet_dir)
    print(f"[bt] loaded candles for {len(candles)} symbols")
    for sym, c in candles.items():
        print(f"  {sym}: {len(c)} bars  {c[0].ts_open} → {c[-1].ts_open}")

    bt = Backtester(cfg, candles, initial_equity=initial, warmup_bars=120)
    metrics = bt.run()
    print()
    print(metrics.print_report())

    return 0


if __name__ == "__main__":
    sys.exit(main())

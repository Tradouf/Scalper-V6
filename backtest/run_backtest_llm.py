import logging
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

from exchanges.hyperliquid import HyperliquidExchangeClient
from backtest.backtester_llm import BacktesterLLM

SYMBOLS  = ["BTC", "MON", "LINK", "DOGE", "OP", "BNB", "SOL"]
INTERVAL = "1h"
DAYS     = 14

if __name__ == "__main__":
    exchange = HyperliquidExchangeClient(enable_trading=False)
    bt       = BacktesterLLM(exchange, step_bars=4, lookback=50)

    print("\n" + "="*60)
    print(" BACKTEST LLM — deepseek-coder-v2:lite")
    print(f" Symboles : {', '.join(SYMBOLS)}")
    print(f" Periode  : {DAYS}j  Interval: {INTERVAL}  Step: 4 bougies")
    print("="*60 + "\n")

    results = []
    for symbol in SYMBOLS:
        print(f"Analyse {symbol}...")
        try:
            r = bt.run(symbol, interval=INTERVAL, days=DAYS)
            if r:
                results.append(r)
                print(f"  OK {r}")
            else:
                print(f"  Pas assez de donnees")
        except Exception as e:
            print(f"  Erreur: {e}")

    print("\n" + "="*60)
    print(" RESUME")
    print("="*60)
    results.sort(key=lambda r: r.profit_factor, reverse=True)
    for r in results:
        icon = "TOP" if r.profit_factor >= 2.0 else "OK" if r.profit_factor >= 1.5 else "NON"
        print(f"  {icon} {r.symbol:<10} PF={r.profit_factor:.2f}  WR={r.winrate*100:.0f}%  PnL={r.total_pnl:+.1f}%  trades={r.nb_trades}  conf={r.avg_confidence:.2f}")
    print()

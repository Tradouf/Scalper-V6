"""
Lance un backtest sur toutes les paires du top scanner.
Affiche uniquement les stratégies avec Profit Factor > 1.5
"""
import logging
logging.basicConfig(level=logging.WARNING)

from exchanges.hyperliquid import HyperliquidExchangeClient
from agents.market_scanner import MarketScannerAgent
from backtest.backtester import Backtester

client    = HyperliquidExchangeClient(enable_trading=False)
scanner   = MarketScannerAgent(client, whale_api_key="DISABLED")
backtester = Backtester(client)

# Récupère le top 15 du scanner
print("Scan des marchés...")
top = scanner.scan(top_n=15)
symbols = [m.symbol for m in top]
print(f"Marchés à backtester : {symbols}\n")

results = []
for symbol in symbols:
    for strategy in ["trend", "momentum"]:
        try:
            result = backtester.run(
                symbol   = symbol,
                interval = "1h",
                days     = 30,
                strategy = strategy,
            )
            results.append(result)
        except Exception as e:
            print(f"  ⚠️  {symbol} [{strategy}] ignoré: {e}")

# Afficher uniquement les bonnes stratégies
print("\n" + "═"*60)
print("  STRATÉGIES RENTABLES (Profit Factor > 1.5)")
print("═"*60)
good = [r for r in results if r.profit_factor > 1.5 and r.nb_trades >= 2]
good.sort(key=lambda r: r.profit_factor, reverse=True)
for r in good:
    print(
        f"  ✅ {r.strategy.upper():<10} {r.symbol:<8} "
        f"PF={r.profit_factor:.2f}  WR={r.winrate*100:.0f}%  "
        f"P&L={r.total_pnl:+.2f}USDT  DD={r.max_drawdown:.1f}%"
    )

if not good:
    print("  Aucune stratégie rentable détectée sur cette période.")

print("\n" + "═"*60)
print("  TOUTES LES STRATÉGIES")
print("═"*60)
results.sort(key=lambda r: r.total_pnl, reverse=True)
for r in results:
    emoji = "✅" if r.profit_factor > 1.5 else "❌"
    print(
        f"  {emoji} {r.strategy.upper():<10} {r.symbol:<8} "
        f"PF={r.profit_factor:.2f}  WR={r.winrate*100:.0f}%  "
        f"P&L={r.total_pnl:+.2f}USDT  DD={r.max_drawdown:.1f}%  "
        f"({r.nb_trades} trades)"
    )

#!/usr/bin/env python3
"""
Balayage des paramètres de l'Awesome Oscillator (BTC 5m) — Phase 1.

Backteste la stratégie AO sur BTC 5m en variant x_long / x_short, sortie TP seul
(pas de SL). Sert à choisir les seuils avant toute activation live (cf.
AO_STRATEGY_PLAN.md). Utilise les candles HL en lecture seule (aucun ordre).

Usage :
    source .venv/bin/activate
    python3 backtest/run_ao_sweep.py                       # défauts
    python3 backtest/run_ao_sweep.py --days 60 --tp 0.012  # 60j, TP 1.2%
    python3 backtest/run_ao_sweep.py --x-long 40,55,65,80 --x-short 40,55,60,75
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backtest.backtester import Backtester
from execution.hyperliquid_adapter import HyperliquidReadAdapter


class _OHLCVClient:
    """Adapte HyperliquidReadAdapter.get_candles → get_ohlcv attendu par Backtester."""

    _MIN = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}

    def __init__(self, adapter: HyperliquidReadAdapter) -> None:
        self._a = adapter

    # HL candleSnapshot plafonne à ~5000 candles et renvoie les PLUS ANCIENNES de
    # la fenêtre demandée. Pour obtenir les candles les plus RÉCENTES, on borne la
    # fenêtre à 5000 candles (≈17,3j en 5m) : au-delà, HL tronquerait au début
    # (données périmées). On émet un avertissement si `days` dépasse la capacité.
    _HL_CAP = 5000

    def get_ohlcv(self, symbol, interval="5m", days=30):
        per = self._MIN.get(interval, 5)
        want = int(days * 24 * 60 / per) + 5
        limit = min(want, self._HL_CAP)
        if want > self._HL_CAP:
            cap_days = self._HL_CAP * per / (24 * 60)
            print(
                f"[avert] {days}j en {interval} = {want} candles > cap HL {self._HL_CAP} "
                f"→ fenêtre tronquée aux {cap_days:.1f}j les plus récents."
            )
        candles = self._a.get_candles(symbol, interval=interval, limit=limit)
        return [
            [int(c.ts_open.timestamp() * 1000), c.open, c.high, c.low, c.close, c.volume]
            for c in candles
        ]


def _floats(s: str) -> list[float]:
    return [float(x) for x in s.split(",") if x.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description="Balayage AO BTC 5m (TP + SL, ratio TP/SL fixe)")
    ap.add_argument("--symbol", default="BTC")
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--days", type=int, default=17)
    ap.add_argument("--tp", default="0.008,0.012,0.016,0.024,0.03",
                    help="Liste de TP (fraction du prix), balayée")
    ap.add_argument("--ratio", default="2.0",
                    help="Liste de ratios TP/SL → SL = TP/ratio (2.0 = TP vaut 2×SL ; 0 = SL off)")
    ap.add_argument("--fast", type=int, default=5)
    ap.add_argument("--slow", type=int, default=34)
    ap.add_argument("--motif", default="threshold", choices=["threshold", "zerocross"],
                    help="threshold = AO<−x_long / AO>+x_short + bougie ; zerocross = AO franchit 0 (sans seuil)")
    ap.add_argument("--x-long", default="65", help="Liste séparée par des virgules (motif threshold)")
    ap.add_argument("--x-short", default="60", help="Liste séparée par des virgules (motif threshold)")
    args = ap.parse_args()

    zerocross = args.motif == "zerocross"
    strat_id = "ao_zerocross" if zerocross else "ao"
    # En zero-cross les seuils n'ont pas de sens → une seule cellule placeholder.
    x_longs = [0.0] if zerocross else _floats(args.x_long)
    x_shorts = [0.0] if zerocross else _floats(args.x_short)
    tps = _floats(args.tp)
    ratios = _floats(args.ratio)

    bt = Backtester(_OHLCVClient(HyperliquidReadAdapter()))

    print(
        f"\nBalayage AO [{args.motif}] {args.symbol} {args.interval} sur {args.days}j  "
        f"(fast={args.fast} slow={args.slow})\n"
    )
    header = (f"{'x_long':>7} {'x_short':>8} {'ratio':>6} {'TP%':>6} {'SL%':>6} {'trades':>7} "
              f"{'pnl%':>8} {'winrate':>8} {'PF':>6} {'maxDD%':>7}")
    print(header)
    print("-" * len(header))

    rows = []
    for xl in x_longs:
        for xs in x_shorts:
            for tp in tps:
                for ratio in ratios:
                    sl = tp / ratio if ratio > 0 else 0.0
                    try:
                        r = bt.run(
                            args.symbol, interval=args.interval, days=args.days, strategy=strat_id,
                            tp_pct=tp, sl_pct=sl, fast=args.fast, slow=args.slow,
                            x_long=xl, x_short=xs,
                        )
                    except Exception as e:
                        print(f"{xl:>7.0f} {xs:>8.0f} {ratio:>6.1f} {tp*100:>6.2f} {sl*100:>6.2f}  ERREUR: {e}")
                        continue
                    rows.append((xl, xs, ratio, tp, sl, r))
                    print(
                        f"{xl:>7.0f} {xs:>8.0f} {ratio:>6.1f} {tp*100:>6.2f} {sl*100:>6.2f} {r.nb_trades:>7d} "
                        f"{r.total_pnl:>8.2f} {r.winrate:>8.3f} {r.profit_factor:>6.2f} {r.max_drawdown:>7.2f}"
                    )

    if rows:
        best = max(rows, key=lambda t: t[5].total_pnl)
        xl, xs, ratio, tp, sl, r = best
        print(
            f"\nMeilleur PnL : x_long={xl:.0f} x_short={xs:.0f} ratio={ratio:g} TP={tp:.2%} SL={sl:.2%} → "
            f"{r.total_pnl:.2f}% ({r.nb_trades} trades, winrate {r.winrate:.1%}, PF {r.profit_factor:.2f})"
        )


if __name__ == "__main__":
    main()

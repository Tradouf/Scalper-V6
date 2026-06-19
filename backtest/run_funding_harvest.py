#!/usr/bin/env python3
"""
Funding harvest MARKET-NEUTRAL avec carry (hyp. #1, version textbook) — walk-forward OOS.

Combine ce qui manquait aux 2 tests précédents :
  - cross-sectionnel = LONG bottom-k funding / SHORT top-k funding → market-neutral,
    pas de risque de queue directionnel (ce qui avait tué la version par symbole) ;
  - carry crédité = on encaisse le spread de funding pendant la détention (le vrai edge,
    absent de la 1re version cross-sectionnelle qui ne comptait que le prix).

sign : −1 = FADE (short le funding haut, le harvest classique) ; +1 = inverse. Balayé
pour vérifier que l'OOS choisit bien le fade.

Usage :
    source .venv/bin/activate
    python3 backtest/run_funding_harvest.py --days 200
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backtest.backtester import Backtester
from backtest.cross_sectional import CrossSectionalWalkForward, build_funding_matrix, fetch_panel
from backtest.run_ao_sweep import _OHLCVClient, _floats
from execution.hyperliquid_adapter import HyperliquidReadAdapter


def main() -> None:
    ap = argparse.ArgumentParser(description="Funding harvest market-neutral + carry — walk-forward OOS")
    ap.add_argument("--symbols", default="BTC,ETH,SOL,BNB,AAVE,LINK,SUI,DOGE")
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--interval", default="1h", help="Timeframe des closes (1h, 4h...)")
    ap.add_argument("--lookback", default="24,48,96,168", help="Fenêtres funding cumulé (barres) pour le classement")
    ap.add_argument("--rebal", default="", help="Rebal (h) ; défaut = lookback")
    ap.add_argument("--k", type=int, default=2)
    ap.add_argument("--sign", default="1,-1", help="-1 = fade/harvest, +1 = inverse. Balayé")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--fee", type=float, default=Backtester.DEFAULT_FEE_PCT)
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    lookbacks = [int(x) for x in _floats(args.lookback)]
    signs = [int(x) for x in _floats(args.sign)]
    rebals = [int(x) for x in _floats(args.rebal)] if args.rebal else None

    adapter = HyperliquidReadAdapter()
    closes = fetch_panel(_OHLCVClient(adapter), symbols, args.interval, args.days)
    syms = list(closes.columns)
    fund = build_funding_matrix(adapter, syms, closes.index, args.days)
    valid = [s for s in syms if fund[s].notna().any()]
    closes, fund = closes[valid], fund[valid]
    print(f"\nPanel {valid}  {len(closes)} barres {args.interval}, funding aligné (carry crédité)")

    combos, rank_matrices = [], []
    for lb in lookbacks:
        cum = fund.rolling(lb, min_periods=lb).sum()
        for rb in (rebals if rebals else [lb]):
            for sg in signs:
                combos.append({"lookback": lb, "rebal": rb, "k": args.k, "sign": sg})
                rank_matrices.append(cum)
    print(f"Grille = {len(combos)} combos\n")

    rep = CrossSectionalWalkForward(fee_pct=args.fee).evaluate(
        closes, combos, n_folds=args.folds, rank_matrices=rank_matrices,
        carry_matrix=fund, strategy="cs_funding_harvest",
    )
    print(rep.summary())


if __name__ == "__main__":
    main()

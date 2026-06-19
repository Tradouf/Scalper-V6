#!/usr/bin/env python3
"""
Runner stat-arb cointégration (pairs trading) — hypothèse #9, 2026-06-18.

Récupère un panel de closes multi-symboles (1h, longue durée = multi-régime),
liste les paires cointégrées sur tout l'historique (diagnostic), puis lance le
walk-forward OOS (sélection paire+params par fold sur le train, jugement OOS,
gate à barre relevée). Lecture seule. Rien en live.

Usage :
    source .venv/bin/activate
    python3 backtest/run_pairs_statarb.py                          # 1h, 200j
    python3 backtest/run_pairs_statarb.py --interval 4h --days 400
    python3 backtest/run_pairs_statarb.py --symbols BTC,ETH,SOL,BNB,LINK
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backtest.backtester import Backtester
from backtest.cross_sectional import fetch_panel
from backtest.pairs_statarb import PairsWalkForward, default_combos, select_pairs
from backtest.run_ao_sweep import _OHLCVClient
from execution.hyperliquid_adapter import HyperliquidReadAdapter


def main() -> None:
    ap = argparse.ArgumentParser(description="Walk-forward OOS du stat-arb cointégration")
    ap.add_argument("--symbols", default="BTC,ETH,SOL,BNB,LINK,AAVE,SUI,DOGE,XRP,LINK",
                    help="panier de candidats (paires testées = C(n,2))")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--folds", type=int, default=6)
    ap.add_argument("--adf", type=float, default=-2.9, help="seuil ADF de cointégration")
    ap.add_argument("--max-pairs", type=int, default=5)
    ap.add_argument("--min-tstat", type=float, default=2.0, help="gate relevé (multi-testing)")
    ap.add_argument("--select", default="pnl", choices=["pnl", "pf"])
    ap.add_argument("--book", type=int, default=0,
                    help="0 = meilleure paire/fold ; N>0 = book de N paires équipondérées (diversifié)")
    args = ap.parse_args()

    symbols = sorted({s.strip().upper() for s in args.symbols.split(",") if s.strip()})
    client = _OHLCVClient(HyperliquidReadAdapter())
    closes = fetch_panel(client, symbols, args.interval, args.days)
    span = (closes.index[-1] - closes.index[0]) / 1000 / 86400
    print(f"\nStat-arb cointégration — {list(closes.columns)}")
    print(f"{len(closes)} barres {args.interval} ≈ {span:.0f}j  ·  {len(closes.columns)} symboles "
          f"→ {len(closes.columns)*(len(closes.columns)-1)//2} paires candidates\n")

    # Diagnostic : paires cointégrées sur tout l'historique (indicatif, PAS le verdict).
    diag = select_pairs(closes, adf_thr=args.adf, max_pairs=15)
    if diag:
        print("Paires cointégrées (historique complet — indicatif, biais rétrospectif) :")
        print(f"  {'paire':>12} {'beta':>8} {'ADF t':>8} {'demi-vie(barres)':>17}")
        for d in diag:
            print(f"  {d['a']+'/'+d['b']:>12} {d['beta']:>8.3f} {d['adf']:>8.2f} {d['hl']:>17.1f}")
    else:
        print("Aucune paire cointégrée sur l'historique complet au seuil ADF "
              f"{args.adf}. (marché trop décorrélé / seuil trop strict)")
    print()

    wf = PairsWalkForward(fee_pct=Backtester.DEFAULT_FEE_PCT)
    if args.book > 0:
        print(f"Mode BOOK : {args.book} paires équipondérées par fold (diversifié)\n")
        rep = wf.evaluate_book(closes, default_combos(), n_folds=args.folds,
                               book_size=args.book, adf_thr=args.adf,
                               min_oos_tstat=args.min_tstat)
    else:
        rep = wf.evaluate(closes, default_combos(), n_folds=args.folds,
                          adf_thr=args.adf, max_pairs=args.max_pairs,
                          select_metric=args.select, min_oos_tstat=args.min_tstat)
    print(rep.summary())
    print()
    print("=" * 70)
    tag = "✅ PASS" if rep.passed else "❌ REJET"
    print(f"VERDICT stat-arb cointégration : {tag}")
    if not rep.passed:
        print("  (mécanisme valable, mais ne tient pas le gate OOS net de frais — "
              "ne PAS déployer ; cf. STRATEGY_HYPOTHESES.md)")


if __name__ == "__main__":
    main()

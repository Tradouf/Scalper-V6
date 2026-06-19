#!/usr/bin/env python3
"""
Hypothèse #1 — funding fade cross-sectionnel, évalué en walk-forward OOS net de frais.

Thèse : le funding est une prime de risque structurelle (flux de cash contraint).
Un funding cumulé fortement positif = longs surpondérés payant les shorts → pression
de débouclage → SHORT ; symétrique en négatif → LONG. Market-neutral (short top-k
funding / long bottom-k). Le harnais balaie aussi le signe : si l'OOS choisit
systématiquement le FADE (short high funding), c'est une confirmation ; s'il
flip-flope, pas d'effet stable.

Données : closes 1h (get_ohlcv) + historique funding HL horaire (get_funding_history,
câblé 2026-06-18, paginé). Rien d'autre à brancher.

Usage :
    source .venv/bin/activate
    python3 backtest/run_funding_fade.py --days 200
    python3 backtest/run_funding_fade.py --days 300 --lookback 24,48,96 --k 2
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
    ap = argparse.ArgumentParser(description="Funding fade cross-sectionnel — walk-forward OOS")
    ap.add_argument("--symbols", default="BTC,ETH,SOL,BNB,AAVE,LINK,SUI,DOGE")
    ap.add_argument("--days", type=int, default=200)
    ap.add_argument("--lookback", default="24,48,96,168", help="Fenêtres de funding cumulé (heures)")
    ap.add_argument("--rebal", default="", help="Rebal (heures) ; défaut = lookback")
    ap.add_argument("--k", type=int, default=2)
    ap.add_argument("--sign", default="1,-1", help="1=long top funding, -1=FADE (short top). Balayé")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--fee", type=float, default=Backtester.DEFAULT_FEE_PCT)
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    lookbacks = [int(x) for x in _floats(args.lookback)]
    signs = [int(x) for x in _floats(args.sign)]
    rebals = [int(x) for x in _floats(args.rebal)] if args.rebal else None

    adapter = HyperliquidReadAdapter()
    client = _OHLCVClient(adapter)
    closes = fetch_panel(client, symbols, "1h", args.days)
    syms = list(closes.columns)
    print(f"\nPanel {syms}  {len(closes)} barres 1h alignées")

    fund = build_funding_matrix(adapter, syms, closes.index, args.days)
    # Garde uniquement les symboles avec funding exploitable.
    valid = [s for s in syms if fund[s].notna().any()]
    closes, fund = closes[valid], fund[valid]
    print(f"Funding aligné pour {len(valid)} symboles : {valid}")

    # Une matrice de classement (funding cumulé) par combo, alignée sur `combos`.
    combos, rank_matrices = [], []
    for lb in lookbacks:
        cum = fund.rolling(lb, min_periods=lb).sum()
        rb_list = rebals if rebals else [lb]
        for rb in rb_list:
            for sg in signs:
                combos.append({"lookback": lb, "rebal": rb, "k": args.k, "sign": sg})
                rank_matrices.append(cum)
    print(f"Grille = {len(combos)} combos (lookback×rebal×sign, k={args.k})\n")

    rep = CrossSectionalWalkForward(fee_pct=args.fee).evaluate(
        closes, combos, n_folds=args.folds,
        rank_matrices=rank_matrices, strategy="cs_funding_fade",
    )
    print(rep.summary())


if __name__ == "__main__":
    main()

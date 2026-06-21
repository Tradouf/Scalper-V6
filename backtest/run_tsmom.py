#!/usr/bin/env python3
"""
Runner — Time-Series Momentum (trend following) sur timeframe LONG, walk-forward OOS (2026-06-20).

Pourquoi cet angle, alors que 10 signaux ont déjà été rejetés : TOUT le sprint a testé du
SCALPING court terme (1m→1h). Le frais round-trip (~0,09 %) est fixe par aller-retour ; pour
le battre il faut un edge BRUT par trade >> 0,09 %. Impossible en grattant des micro-mouvements
(le frais domine — confirmé par les fills live : brut ~à plat, frais = 121 % du brut absolu),
mais trivial sur des mouvements multi-jours. Le time-series momentum (Moskowitz/Ooi/Pedersen 2012)
est l'anomalie la PLUS robuste et documentée des marchés futures/crypto, BASSE FRÉQUENCE (frais
négligeables), et le sprint ne l'a JAMAIS testée (seulement du momentum cross-sectionnel 1h, rejeté
car 1h trop court). HL sert ~2100 bougies 1d (≈5,8 ans, MULTI-RÉGIME) et 5000 en 4h (≈830 j) —
exactement la puissance statistique qui manquait.

Signal : état persistant = signe du rendement trailing sur `lookback` barres (band = zone morte).
Sortie : reverse (stop-and-reverse), la sortie canonique du TSMOM. Params choisis sur le TRAIN de
chaque fold, jugés OOS, gate standard du repo. Verdict honnête : un vrai edge est POSITIF sur la
MAJORITÉ des symboles, pas un seul.

Usage :
    source .venv/bin/activate
    python3 backtest/run_tsmom.py                                  # 1d, univers complet
    python3 backtest/run_tsmom.py --interval 4h --folds 8
    python3 backtest/run_tsmom.py --strategy donchian
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backtest.backtester import Backtester
from backtest.evaluator import WalkForwardEvaluator
from execution.hyperliquid_adapter import HyperliquidReadAdapter

UNIVERSE = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "LINK", "AVAX", "LTC", "AAVE",
    "SUI", "ARB", "NEAR", "WLD", "UNI", "CRV", "ADA", "TRX", "XLM", "INJ",
    "ENA", "ONDO", "JTO", "TAO", "JUP", "AXS", "BCH", "APT", "OP", "ATOM",
    "DOT", "FIL", "TIA", "SEI", "WIF", "XMR", "TON", "LDO", "PENDLE", "FET", "GMX"]


def fetch_df(adapter: HyperliquidReadAdapter, sym: str, interval: str, limit: int = 5000) -> pd.DataFrame:
    # Retry/backoff anti-429 (univers large = beaucoup d'appels rapprochés).
    candles = None
    for attempt in range(5):
        try:
            candles = adapter.get_candles(sym, interval, limit)
            if candles:
                break
        except Exception:
            pass
        time.sleep(0.5 * (attempt + 1))
    if not candles:
        return pd.DataFrame()
    def _ms(t):
        return int(t.timestamp() * 1000) if hasattr(t, "timestamp") else int(t)
    rows = [{"ts": _ms(c.ts_open), "open": float(c.open), "high": float(c.high),
             "low": float(c.low), "close": float(c.close), "volume": float(c.volume)}
            for c in candles]
    df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
    return df


def build_combos(strategy: str) -> list[dict]:
    """Espace réduit (peu de params = peu d'overfit). Sortie reverse → tp/sl ignorés.
    lookback en BARRES (sur 1d : 10→120 j ; band = zone morte sur le rendement trailing)."""
    combos = []
    lookbacks = (10, 20, 30, 50, 80, 120)
    bands = (0.0,) if strategy == "donchian" else (0.0, 0.02, 0.05)
    for lb in lookbacks:
        for band in bands:
            combos.append({"lookback": lb, "band": band, "exit_mode": "reverse"})
    return combos


def main() -> None:
    ap = argparse.ArgumentParser(description="Walk-forward OOS du time-series momentum (trend following)")
    ap.add_argument("--symbols", default=",".join(UNIVERSE))
    ap.add_argument("--strategy", default="tsmom", choices=["tsmom", "donchian"])
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--limit", type=int, default=5000, help="nb de bougies (1d cape ~2100, 4h ~5000)")
    ap.add_argument("--folds", type=int, default=6)
    ap.add_argument("--min-tstat", type=float, default=1.5)
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    adapter = HyperliquidReadAdapter()
    bt = Backtester(None)
    ev = WalkForwardEvaluator(bt)
    combos = build_combos(args.strategy)

    print(f"\n{args.strategy.upper()} — walk-forward OOS  ({len(combos)} combos)")
    print(f"interval={args.interval}  folds={args.folds}  frais {bt.DEFAULT_FEE_PCT:.3%}/côté  "
          f"gate t-stat≥{args.min_tstat}  sortie=reverse (stop-and-reverse)\n")

    verdicts = []
    for sym in symbols:
        df = fetch_df(adapter, sym, args.interval, args.limit)
        if len(df) < 50 * args.folds:
            print(f"[{sym}] données insuffisantes ({len(df)} bougies) — skip")
            continue
        span = (df["ts"].iloc[-1] - df["ts"].iloc[0]) / 1000 / 86400
        t0 = time.time()
        rep = ev.evaluate(df, sym, args.strategy, combos, n_folds=args.folds,
                          min_oos_tstat=args.min_tstat)
        tag = "PASS" if rep.passed else "rejet"
        print(f"[{sym:>5}] {len(df)} bg ≈{span:>5.0f}j  OOS {rep.oos_total_pnl:+7.2f}%  "
              f"folds+={rep.positive_folds}/{rep.n_folds}  trades={rep.oos_total_trades:>3}  "
              f"PFméd={rep.oos_median_pf:.2f}  t={rep.oos_tstat:+.2f}  [{tag}]  ({time.time()-t0:.0f}s)")
        verdicts.append((sym, rep))

    if not verdicts:
        print("aucun symbole exploitable"); return

    # Agrégat cross-univers = le vrai test d'un edge anomalie (positif sur la MAJORITÉ ?)
    n_pos = sum(1 for _, r in verdicts if r.oos_total_pnl > 0)
    n_pass = sum(1 for _, r in verdicts if r.passed)
    mean_oos = sum(r.oos_total_pnl for _, r in verdicts) / len(verdicts)
    med_t = sorted(r.oos_tstat for _, r in verdicts)[len(verdicts) // 2]
    print("\n" + "=" * 72)
    print(f"AGRÉGAT {args.strategy.upper()} {args.interval} sur {len(verdicts)} symboles :")
    print(f"  OOS net positif : {n_pos}/{len(verdicts)} symboles   |   gate PASS : {n_pass}/{len(verdicts)}")
    print(f"  OOS moyen : {mean_oos:+.2f}%   |   t-stat médian : {med_t:+.2f}")
    verdict = ("EDGE PLAUSIBLE — approfondir" if n_pos >= 0.7 * len(verdicts) and mean_oos > 0
               else "PAS D'EDGE ROBUSTE — rejet")
    print(f"  → {verdict}")


if __name__ == "__main__":
    main()

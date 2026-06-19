#!/usr/bin/env python3
"""
Runner du scalper adaptatif « Le Danseur » (prototype offline, 2026-06-18).

Récupère un long historique 5m (paginé, car HL plafonne ~5000 candles/requête),
lance le walk-forward GLISSANT, et imprime le verdict : l'adaptation (ré-optimisée
sur fenêtre glissante) bat-elle des params FIXES et les frais, en OUT-OF-SAMPLE ?

Usage :
    source .venv/bin/activate
    python3 backtest/run_adaptive_scalper.py                          # BTC, défauts
    python3 backtest/run_adaptive_scalper.py --symbol ETH --days 60
    python3 backtest/run_adaptive_scalper.py --train 1500 --test 50   # ré-opt + fréquente
    python3 backtest/run_adaptive_scalper.py --symbols BTC,ETH,SOL    # plusieurs actifs
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pandas as pd

from backtest.adaptive_scalper import rolling_walkforward, default_grid
from backtest.backtester import Backtester
from execution.hyperliquid_adapter import HyperliquidReadAdapter

_INTERVAL_MS = {"1m": 60_000, "5m": 5 * 60_000, "15m": 15 * 60_000,
                "30m": 30 * 60_000, "1h": 3600_000, "4h": 4 * 3600_000}


def fetch_paginated(adapter: HyperliquidReadAdapter, symbol: str, interval: str,
                    days: int, page: int = 4500) -> pd.DataFrame:
    """Pagine candleSnapshot en fenêtres de `page` bougies vers le passé pour
    dépasser le cap ~5000/requête. Déduplique sur le timestamp d'ouverture."""
    per_ms = _INTERVAL_MS.get(interval, 5 * 60_000)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000
    rows: dict[int, list] = {}
    cursor = end_ms
    guard = days * 24 * 60 // (page * (per_ms // 60_000)) + 6
    for _ in range(max(guard, 3)):
        if cursor <= start_ms:
            break
        # get_candles part de maintenant ; on simule une fenêtre en bornant limit
        # puis en avançant le curseur. Ici on récupère les `page` plus récentes
        # avant `cursor` via une requête directe à l'API (start/end).
        win_start = max(start_ms, cursor - page * per_ms)
        candles = adapter.get_candles_window(symbol, interval, win_start, cursor) \
            if hasattr(adapter, "get_candles_window") else _window(adapter, symbol, interval, win_start, cursor)
        if not candles:
            break
        for c in candles:
            ts = int(c.ts_open.timestamp() * 1000)
            rows[ts] = [ts, c.open, c.high, c.low, c.close, c.volume]
        oldest = min(int(c.ts_open.timestamp() * 1000) for c in candles)
        if oldest >= cursor:   # pas de progression → stop
            break
        cursor = oldest
    if not rows:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame([rows[k] for k in sorted(rows)],
                      columns=["ts", "open", "high", "low", "close", "volume"])
    return df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})


def _window(adapter, symbol, interval, start_ms, end_ms):
    """Requête candleSnapshot bornée [start,end] (l'adapter n'expose que les N
    dernières ; ici on tape l'API directement pour une fenêtre arbitraire)."""
    import requests
    from execution.hyperliquid_adapter import HL_API, Candle
    import datetime as dt
    try:
        r = requests.post(HL_API, json={"type": "candleSnapshot", "req": {
            "coin": symbol.upper(), "interval": interval,
            "startTime": int(start_ms), "endTime": int(end_ms)}}, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[avert] fetch {symbol} fenêtre échec: {e!r}")
        return []
    out = []
    if isinstance(data, list):
        for row in data:
            try:
                out.append(Candle(
                    ts_open=dt.datetime.utcfromtimestamp(int(row.get("t", 0)) / 1000.0),
                    open=float(row.get("o", 0)), high=float(row.get("h", 0)),
                    low=float(row.get("l", 0)), close=float(row.get("c", 0)),
                    volume=float(row.get("v", 0) or 0)))
            except Exception:
                continue
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Walk-forward glissant du scalper adaptatif MA×RSI")
    ap.add_argument("--symbol", default="BTC")
    ap.add_argument("--symbols", default="", help="liste séparée par des virgules (override --symbol)")
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--days", type=int, default=60, help="historique à paginer (5m : au-delà de ~17j = multi-requêtes)")
    ap.add_argument("--train", type=int, default=1000, help="bougies de TRAIN (sélection des params)")
    ap.add_argument("--test", type=int, default=100, help="bougies de TEST OOS par pas (cadence de ré-opt)")
    ap.add_argument("--select", default="pnl", choices=["pnl", "pf"])
    ap.add_argument("--min-tstat", type=float, default=1.5, help="durcir le gate (multi-testing)")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in (args.symbols.split(",") if args.symbols else [args.symbol]) if s.strip()]
    adapter = HyperliquidReadAdapter()
    grid = default_grid()
    fee = Backtester.DEFAULT_FEE_PCT

    print(f"\n« Le Danseur » — scalper adaptatif MA×RSI  ({len(grid)} combos dans la grille)")
    print(f"interval={args.interval}  days={args.days}  train={args.train}  test={args.test}  select={args.select}\n")

    verdicts = []
    for sym in symbols:
        df = fetch_paginated(adapter, sym, args.interval, args.days)
        if len(df) < args.train + args.test + 50:
            print(f"[{sym}] données insuffisantes ({len(df)} bougies) — skip\n")
            continue
        span_days = (df["ts"].iloc[-1] - df["ts"].iloc[0]) / 1000 / 86400
        print(f"[{sym}] {len(df)} bougies {args.interval} ≈ {span_days:.1f}j — walk-forward en cours...")
        t0 = time.time()
        rep = rolling_walkforward(
            df, sym, grid=grid, train_bars=args.train, test_bars=args.test,
            fee=fee, select=args.select, min_oos_tstat=args.min_tstat,
        )
        print(f"  ({time.time() - t0:.1f}s)\n")
        print(rep.summary())
        print()
        verdicts.append((sym, rep))

    if verdicts:
        print("=" * 70)
        print("VERDICT (la question qui décide si l'idée peut vivre) :")
        for sym, rep in verdicts:
            edge = rep.adaptive_oos_pnl - rep.fixed_oos_pnl
            tag = "✅ PASS gate" if rep.report.passed else "❌ REJET gate"
            beat = "bat le fixe" if edge > 0 else "PERD vs fixe"
            print(f"  {sym:>5} : adaptatif {rep.adaptive_oos_pnl:+7.2f}% OOS  ({beat} {edge:+.2f}pts)  {tag}")
        print("\nRappel : si l'adaptatif ne bat ni le fixe ni les frais EN BACKTEST,")
        print("il ne le fera pas en live. Pas de chorégraphe LLM tant que ceci n'est pas vert.")


if __name__ == "__main__":
    main()

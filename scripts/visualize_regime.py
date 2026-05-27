#!/usr/bin/env python3
"""
Validation visuelle : régime détecté pas-à-pas sur les 6 mois de candles BTC
historiques. Affiche une time-series de labels + probabilités.

Sortie texte : un résumé statistique (% du temps par régime, nb transitions).
Sortie graphique optionnelle : PNG si matplotlib dispo.

Usage :
  cd ~/SalleDesMarches_v7
  python3 scripts/visualize_regime.py [SYMBOL]   # default BTC
"""
from __future__ import annotations

import datetime as dt
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from core.config import load_config
from core.types import Candle, MarketSnapshot, Regime
from regime.detector import RuleBasedRegimeDetector


def load_candles(symbol: str) -> list[Candle]:
    path = REPO / "data" / "historical" / f"ohlcv_1h_{symbol}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Parquet manquant : {path}")
    df = pd.read_parquet(path)
    out = []
    for _, row in df.iterrows():
        out.append(
            Candle(
                ts_open=pd.to_datetime(row["ts_open"], unit="ms").to_pydatetime(),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume", 0) or 0),
            )
        )
    return out


def walk_regimes(symbol: str, min_history: int = 120) -> list[tuple[dt.datetime, Regime, dict, float]]:
    """Itère sur les candles : à chaque t ≥ min_history, calcule le régime à t.
    Retourne [(ts, label, probas, close)]."""
    cfg = load_config()
    det = RuleBasedRegimeDetector(cfg.regime)
    candles = load_candles(symbol)
    print(f"[viz] {symbol} : {len(candles)} candles chargées", flush=True)

    history = []
    for t in range(min_history, len(candles)):
        sub = candles[: t + 1]
        snap = MarketSnapshot(
            timestamp=sub[-1].ts_open,
            candles={symbol: sub},
            prices={symbol: sub[-1].close},
        )
        rs = det.detect(snap)
        history.append((sub[-1].ts_open, rs.label, rs.probabilities, sub[-1].close))
        if t % 500 == 0:
            print(f"  t={t}/{len(candles)} label={rs.label.value} conf={rs.confidence:.2f}", flush=True)
    return history


def summarize(history: list, symbol: str) -> None:
    if not history:
        print("Aucun régime calculé.")
        return
    labels = [h[1] for h in history]
    counts = Counter(labels)
    n = len(labels)
    print(f"\n=== Distribution des régimes sur {n} bars 1h ({symbol}) ===")
    for r in Regime:
        c = counts.get(r, 0)
        pct = 100.0 * c / n
        bar = "█" * int(pct / 2)
        print(f"  {r.value:11s} {c:5d}  {pct:5.1f}%  {bar}")

    # Transitions
    transitions = 0
    for a, b in zip(labels[:-1], labels[1:]):
        if a != b:
            transitions += 1
    print(f"\nTransitions de label : {transitions} ({100.0 * transitions / n:.2f}%)")
    if transitions > 0:
        avg_dwell = n / (transitions + 1)
        print(f"Durée moyenne par régime : {avg_dwell:.1f} bars ({avg_dwell:.1f}h)")


def try_plot(history: list, symbol: str) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("\n[viz] matplotlib indispo, skip plot")
        return

    if not history:
        return
    ts = [h[0] for h in history]
    prices = [h[3] for h in history]
    labels = [h[1] for h in history]

    color_map = {
        Regime.TREND_UP: "#2ca02c",
        Regime.TREND_DOWN: "#d62728",
        Regime.RANGE: "#1f77b4",
        Regime.HIGH_VOL: "#ff7f0e",
    }

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True, gridspec_kw={"height_ratios": [3, 1]})

    # Prix coloré par régime
    for i in range(len(ts) - 1):
        ax1.plot(ts[i:i + 2], prices[i:i + 2], color=color_map[labels[i]], linewidth=1.2)
    ax1.set_ylabel(f"{symbol} close (USDC)")
    ax1.set_title(f"{symbol} 1h — régime détecté par RuleBasedRegimeDetector")
    ax1.grid(alpha=0.3)
    # Légende manuelle
    from matplotlib.lines import Line2D
    handles = [Line2D([0], [0], color=c, lw=2, label=r.value) for r, c in color_map.items()]
    ax1.legend(handles=handles, loc="upper left", framealpha=0.9)

    # Bande de régime en bas
    label_vals = {r: i for i, r in enumerate(Regime)}
    ys = [label_vals[l] for l in labels]
    for i in range(len(ts) - 1):
        ax2.axvspan(ts[i], ts[i + 1], color=color_map[labels[i]], alpha=0.7)
    ax2.set_yticks([])
    ax2.set_xlabel("Time")
    ax2.set_title("Régime label (bande)")

    out = REPO / "data" / "historical" / f"regime_{symbol}.png"
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    print(f"\n[viz] Plot → {out}")


def main() -> int:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTC"
    history = walk_regimes(symbol)
    summarize(history, symbol)
    try_plot(history, symbol)
    return 0


if __name__ == "__main__":
    sys.exit(main())

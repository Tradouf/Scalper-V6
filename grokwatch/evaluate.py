"""
Évaluation a posteriori des signaux enregistrés — le verdict du tracker.

Pour chaque signal assez vieux, mesure le rendement dans le sens du signal à
+1 h / +4 h / +24 h, brut et net de frais (aller-retour taker par défaut).
Agrège ensuite : n, hit-rate, moyenne, médiane par horizon.

    python -m grokwatch.evaluate
"""

from __future__ import annotations

import bisect
import os
import statistics
import time
from typing import List, Optional

from simplebot.data import closed_candles, fetch_ohlcv

from grokwatch.store import load_signals

HORIZONS_SEC = {"1h": 3_600, "4h": 4 * 3_600, "24h": 24 * 3_600}
_15M_MS = 900_000

# Aller-retour taker HL base tier (0.045 % × 2) — surchargeable
FEE_ROUNDTRIP = 2 * float(os.environ.get("GROKWATCH_FEE_PCT", "0.00045"))


def price_at(candles: List[dict], target_ms: int) -> Optional[float]:
    """Close de la bougie couvrant target_ms (dernière bougie ts <= target)."""
    if not candles:
        return None
    ts_list = [c["ts"] for c in candles]
    i = bisect.bisect_right(ts_list, target_ms) - 1
    if i < 0:
        return None
    return float(candles[i]["close"])


def signal_returns(sig: dict, candles: List[dict],
                   now_ts: Optional[float] = None) -> dict:
    """Rendements signés par horizon échu. {'1h': {'gross':…, 'net':…}, …}"""
    now_ts = now_ts if now_ts is not None else time.time()
    ts_ms = int(sig["ts"] * 1000)
    p0 = sig.get("mid_at_receipt") or price_at(candles, ts_ms)
    if not p0:
        return {}
    sign = 1.0 if sig["direction"] == "LONG" else -1.0
    out = {}
    for name, h in HORIZONS_SEC.items():
        if sig["ts"] + h > now_ts:
            continue  # horizon pas encore échu
        ph = price_at(candles, ts_ms + h * 1000)
        if ph is None:
            continue
        gross = sign * (ph - p0) / p0
        out[name] = {"gross": gross, "net": gross - FEE_ROUNDTRIP}
    return out


def evaluate(now_ts: Optional[float] = None) -> dict:
    """{'signals': [par signal], 'aggregate': {horizon: stats}}"""
    now_ts = now_ts if now_ts is not None else time.time()
    sigs = load_signals()
    if not sigs:
        return {"signals": [], "aggregate": {}}

    per_symbol: dict = {}
    rows = []
    for sig in sigs:
        symbol = sig["symbol"]
        if symbol not in per_symbol:
            oldest = min(s["ts"] for s in sigs if s["symbol"] == symbol)
            days = (now_ts - oldest) / 86_400 + 1.5
            candles = closed_candles(fetch_ohlcv(symbol, "15m", days=days),
                                     _15M_MS)
            per_symbol[symbol] = candles
        rets = signal_returns(sig, per_symbol[symbol], now_ts)
        rows.append({"iso": sig["iso"], "symbol": symbol,
                     "direction": sig["direction"],
                     "mid_at_receipt": sig.get("mid_at_receipt"),
                     "returns": rets})

    aggregate = {}
    for name in HORIZONS_SEC:
        nets = [r["returns"][name]["net"] for r in rows if name in r["returns"]]
        if not nets:
            continue
        aggregate[name] = {
            "n": len(nets),
            "hit_rate": sum(1 for x in nets if x > 0) / len(nets),
            "mean_net": statistics.mean(nets),
            "median_net": statistics.median(nets),
        }
    return {"signals": rows, "aggregate": aggregate}


def main() -> int:
    res = evaluate()
    if not res["signals"]:
        print("Aucun signal enregistré.")
        return 0
    print(f"{len(res['signals'])} signal(aux) enregistré(s) — "
          f"frais aller-retour {FEE_ROUNDTRIP * 100:.3f}%\n")
    for r in res["signals"]:
        rets = " | ".join(
            f"+{h}: {v['net'] * 100:+.2f}% net" for h, v in r["returns"].items()
        ) or "horizons non échus"
        print(f"  {r['iso']}  {r['direction']:<5} {r['symbol']:<6} "
              f"@ {r['mid_at_receipt']}  →  {rets}")
    if res["aggregate"]:
        print("\nAgrégat (net de frais) :")
        for h, a in res["aggregate"].items():
            print(f"  +{h:<4} n={a['n']:<3} hit={a['hit_rate'] * 100:.0f}%  "
                  f"moy={a['mean_net'] * 100:+.2f}%  "
                  f"méd={a['median_net'] * 100:+.2f}%")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

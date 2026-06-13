#!/usr/bin/env python3
"""
Benchmark LLM gouverneur : qwen3.5-9b vs deepseek-r1-14b (2026-06-13).

Compare deux modèles locaux sur la TÂCHE RÉELLE du gouverneur tactique (via le
vrai code RiskGovernor → teste parsing + clamps inclus). Pour chaque scénario,
on connaît la direction ATTENDUE (ex. high_vol → resserrer stop + réduire taille ;
range calme + bleed → élargir stop). On score :
  - latence
  - taux de JSON valide (source != fallback)
  - justesse directionnelle (la décision va-t-elle dans le bon sens ?)

Usage : python3 scripts/bench_governor_llm.py [modèle1 modèle2 ...]
        (défaut : qwen3.5-9b deepseek-r1-14b)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from governor.risk_governor import RiskGovernor  # noqa: E402

ENDPOINT = "http://localhost:8080"

# Scénarios + attentes directionnelles. emergency_dir : +1 = doit élargir (>0.04),
# -1 = doit resserrer (<0.04). size_dir : +1 = plutôt grand (>0.9), -1 = réduire (<0.9).
SCENARIOS = [
    ("range calme + bleed (stops trop serrés)", {
        "regime": "range", "regime_confidence": 0.92, "vol_ratio_vs_median": 0.8,
        "emergency_exits_last_1h": 4, "open_positions": 6,
        "positions_roe_pct": [-1.8, -0.9, 0.4, -2.1], "equity_usd": 631.0,
        "current_emergency_roe_pct": 0.022, "leverage": 3},
     {"emergency_dir": +1, "size_dir": -1}),
    ("high_vol violent (krach)", {
        "regime": "high_vol", "regime_confidence": 0.85, "vol_ratio_vs_median": 2.4,
        "emergency_exits_last_1h": 2, "open_positions": 3,
        "positions_roe_pct": [-3.0, -1.2, 0.5], "equity_usd": 631.0,
        "current_emergency_roe_pct": 0.045, "leverage": 3},
     {"emergency_dir": -1, "size_dir": -1}),
    ("trend sain, equity stable", {
        "regime": "trend_up", "regime_confidence": 0.78, "vol_ratio_vs_median": 1.0,
        "emergency_exits_last_1h": 0, "open_positions": 2,
        "positions_roe_pct": [1.5, 2.2], "equity_usd": 640.0,
        "current_emergency_roe_pct": 0.04, "leverage": 3},
     {"emergency_dir": 0, "size_dir": +1}),
    ("range très calme, 0 coupure", {
        "regime": "range", "regime_confidence": 0.95, "vol_ratio_vs_median": 0.6,
        "emergency_exits_last_1h": 0, "open_positions": 5,
        "positions_roe_pct": [0.3, -0.2, 0.8, 0.1, -0.4], "equity_usd": 638.0,
        "current_emergency_roe_pct": 0.04, "leverage": 3},
     {"emergency_dir": +1, "size_dir": +1}),
]


def score_direction(dec, expect) -> tuple[int, int]:
    """→ (emergency_ok, size_ok) ∈ {0,1}."""
    e_ok = 1
    if expect["emergency_dir"] > 0:
        e_ok = 1 if dec.emergency_roe_pct >= 0.042 else 0
    elif expect["emergency_dir"] < 0:
        e_ok = 1 if dec.emergency_roe_pct <= 0.040 else 0
    s_ok = 1
    if expect["size_dir"] > 0:
        s_ok = 1 if dec.size_mult >= 0.9 else 0
    elif expect["size_dir"] < 0:
        s_ok = 1 if dec.size_mult <= 0.9 else 0
    return e_ok, s_ok


def bench(model: str) -> None:
    print(f"\n{'='*70}\nMODÈLE : {model}\n{'='*70}")
    gov = RiskGovernor(ENDPOINT, model, timeout=180)
    lat, valid, e_hits, s_hits = [], 0, 0, 0
    for name, feats, expect in SCENARIOS:
        t = time.time()
        dec = gov.decide(feats)
        dt = time.time() - t
        lat.append(dt)
        ok = dec.source in ("llm", "clamped")
        valid += int(ok)
        e_ok, s_ok = score_direction(dec, expect)
        e_hits += e_ok; s_hits += s_ok
        mark = "✓" if ok else "✗FALLBACK"
        print(f"\n[{name}]  {dt:.0f}s {mark}")
        print(f"  → emergency={dec.emergency_roe_pct} (attendu {'élargir' if expect['emergency_dir']>0 else 'resserrer' if expect['emergency_dir']<0 else 'libre'}) "
              f"{'✓' if e_ok else '✗'}")
        print(f"  → size_mult={dec.size_mult} (attendu {'grand' if expect['size_dir']>0 else 'réduit' if expect['size_dir']<0 else 'libre'}) "
              f"{'✓' if s_ok else '✗'}")
        print(f"  → {dec.reason[:90]}")
    n = len(SCENARIOS)
    print(f"\n── BILAN {model} ──")
    print(f"  latence moy : {sum(lat)/n:.0f}s")
    print(f"  JSON valide : {valid}/{n}")
    print(f"  justesse    : emergency {e_hits}/{n}, taille {s_hits}/{n}  → TOTAL {e_hits+s_hits}/{2*n}")


def main() -> None:
    models = sys.argv[1:] or ["qwen3.5-9b", "deepseek-r1-14b"]
    for m in models:
        bench(m)


if __name__ == "__main__":
    main()

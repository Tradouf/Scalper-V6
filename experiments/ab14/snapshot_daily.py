#!/usr/bin/env python3
"""Agrège equity / trades paper des 2 bras → metrics.csv + daily markdown."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def _load(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _simplebot_metrics(state: dict, start_eq: float) -> dict:
    paper = state.get("paper") or {}
    trades = paper.get("trades") or []
    pnls = [float(t.get("pnl_usd") or 0) for t in trades]
    # fallback si vieux trades sans pnl_usd
    if trades and all(p == 0 for p in pnls) and any("pnl_pct" in t for t in trades):
        eq = start_eq
        pnls = []
        for t in trades:
            # approximation non-compound si pas de $
            notional = start_eq * 0.05 * 3
            p = float(t.get("pnl_pct") or 0) * notional
            pnls.append(p)
            eq += p
        equity = eq
    else:
        equity = float(state.get("paper_equity") or start_eq)
    wins = sum(1 for p in pnls if p > 0)
    return {
        "equity": equity,
        "n_trades": len(trades),
        "n_wins": wins,
        "n_open": len(paper.get("positions") or {}),
        "net": equity - start_eq,
        "fees_est": "",  # inclus dans pnl paper
    }


def _llmbot_metrics(state: dict, start_eq: float) -> dict:
    trades = state.get("trades") or []
    pnls = [float(t.get("pnl_usd") or 0) for t in trades]
    equity = float(state.get("equity") or start_eq)
    wins = sum(1 for p in pnls if p > 0)
    return {
        "equity": equity,
        "n_trades": len(trades),
        "n_wins": wins,
        "n_open": len(state.get("paper_positions") or {}),
        "net": equity - start_eq,
        "fees_est": "",
    }


def _max_dd(hist: list, start_eq: float) -> float:
    if not hist:
        return 0.0
    peak = start_eq
    max_dd = 0.0
    for row in hist:
        try:
            eq = float(row[1])
        except Exception:
            continue
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
    return max_dd


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ab-root", required=True)
    ap.add_argument("--start-equity", type=float, default=200.0)
    args = ap.parse_args()
    root = Path(args.ab_root)
    start_eq = float(args.start_equity)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    a_state = _load(root / "simplebot_state" / "live_state.json")
    b_state = _load(root / "llmbot_state" / "live_state.json")
    a = _simplebot_metrics(a_state, start_eq)
    b = _llmbot_metrics(b_state, start_eq)
    a_dd = _max_dd(a_state.get("equity_history") or [], start_eq)
    b_dd = _max_dd(b_state.get("equity_history") or [], start_eq)

    metrics = root / "report" / "metrics.csv"
    metrics.parent.mkdir(parents=True, exist_ok=True)
    if not metrics.exists():
        metrics.write_text(
            "date,arm,equity,day_pnl,n_trades,n_wins,n_open,fees_est,notes\n",
            encoding="utf-8",
        )

    # day_pnl = delta vs dernière ligne du même bras si existe
    prev = {"A": start_eq, "B": start_eq}
    if metrics.stat().st_size > 50:
        for line in metrics.read_text(encoding="utf-8").strip().splitlines()[1:]:
            parts = line.split(",")
            if len(parts) >= 3:
                prev[parts[1]] = float(parts[2])

    rows = [
        (day, "A", a, prev.get("A", start_eq), f"maxDD={a_dd:.1%}"),
        (day, "B", b, prev.get("B", start_eq), f"maxDD={b_dd:.1%}"),
    ]
    with metrics.open("a", encoding="utf-8") as f:
        for d, arm, m, p_eq, notes in rows:
            day_pnl = m["equity"] - p_eq
            f.write(
                f"{d},{arm},{m['equity']:.4f},{day_pnl:.4f},"
                f"{m['n_trades']},{m['n_wins']},{m['n_open']},"
                f"{m['fees_est']},{notes}\n"
            )

    # day markdown
    daily_dir = root / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    n = len(list(daily_dir.glob("day_*.md")))
    md = daily_dir / f"day_{n:02d}.md"
    md.write_text(
        f"""# Day {n:02d} — {day} UTC

| Arm | Equity | Net vs start | Trades | Wins | Open | MaxDD |
|-----|--------|--------------|--------|------|------|-------|
| A simplebot | {a['equity']:.2f} | {a['net']:+.2f} | {a['n_trades']} | {a['n_wins']} | {a['n_open']} | {a_dd:.1%} |
| B llmbot | {b['equity']:.2f} | {b['net']:+.2f} | {b['n_trades']} | {b['n_wins']} | {b['n_open']} | {b_dd:.1%} |

Notes:
- LocalAI: (remplir si down)
- Anomalies:
""",
        encoding="utf-8",
    )
    print(md)
    print(f"A equity={a['equity']:.2f} trades={a['n_trades']} | "
          f"B equity={b['equity']:.2f} trades={b['n_trades']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Filtre best_params.json sur l'univers A/B (symboles communs)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    ap.add_argument("--symbols", required=True, help="CSV")
    args = ap.parse_args()

    wanted = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
    src = Path(args.src)
    dst = Path(args.dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    if not src.is_file():
        # fallback minimal vide — le live-only refusera les symboles sans params
        data = {"updated_at": None, "interval": "15m", "symbols": {}}
        print(f"⚠️  {src} absent — écrit skeleton vide")
    else:
        data = json.loads(src.read_text(encoding="utf-8"))

    syms = data.get("symbols") or {}
    if isinstance(syms, dict):
        filtered = {k: v for k, v in syms.items() if str(k).upper() in wanted}
    else:
        filtered = {}

    out = dict(data)
    out["symbols"] = filtered
    out["ab_filter"] = sorted(wanted)
    out["ab_kept"] = sorted(filtered.keys())
    dst.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"kept {len(filtered)}/{len(wanted)} symbols → {dst}")
    missing = wanted - {str(k).upper() for k in filtered}
    if missing:
        print("missing params (seront ignorés en live-only):", ",".join(sorted(missing)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

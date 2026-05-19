#!/usr/bin/env python3
"""Affiche le top-30 par dayNtlVlm sur HL — à copier dans config/settings.py."""
import requests

m = requests.post(
    "https://api.hyperliquid.xyz/info", json={"type": "metaAndAssetCtxs"}, timeout=8
).json()
universe = m[0]["universe"]
ctxs = m[1]

top = []
for i, ctx in enumerate(ctxs):
    if i >= len(universe):
        continue
    u = universe[i]
    if bool(u.get("isDelisted", False)):
        continue
    if float(u.get("maxLeverage", 3) or 3) < 2:
        continue
    top.append((u["name"].upper(), float(ctx.get("dayNtlVlm", 0) or 0)))

top.sort(key=lambda x: x[1], reverse=True)
top30 = [n for n, _ in top[:30]]

print("# Top 30 par dayNtlVlm :")
for i, (n, v) in enumerate(top[:30], 1):
    print(f"#   {i:2}. {n:10} ${v / 1e6:.1f}M")
print()
print("SCALP_WATCHLIST = [")
for i in range(0, 30, 10):
    print("    " + ", ".join(f'"{s}"' for s in top30[i:i + 10]) + ",")
print("]")

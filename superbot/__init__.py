"""
SuperBot — bot Hyperliquid déterministe multi-sleeves (voir SPEC.md).

Trois sleeves (momentum 4h, adaptive EMA multi-TF, breakout 1h), régime HMM
double couche (marché BTC + par symbole), wallet HL3 dédié, zéro LLM.
Réutilise les briques validées de simplebot/ (data, strategy, execution,
symbol_filter) — voir SPEC.md §12.
"""

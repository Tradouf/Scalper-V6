"""
Exécution SuperBot (SPEC §6) — wrapper direct de simplebot/execution.py.
Une seule implémentation maker-first pour tout le repo : limit Alo au mid,
timeout, fallback market, fills partiels « mixed ». Ne pas réécrire.
"""

from __future__ import annotations

from simplebot.execution import smart_entry  # noqa: F401 — API publique SuperBot

from superbot import config


def enter_position(client, symbol: str, is_buy: bool, qty: float,
                   ref_price: float) -> dict:
    """Entrée maker-first (ou market si désactivé). Retourne
    {"mode": maker|taker|mixed, "avg_px", "total_sz"}."""
    if config.EXEC_MAKER_FIRST:
        return smart_entry(client, symbol, is_buy, qty, ref_price)
    result = client.place_order(coin=symbol, is_buy=is_buy, sz=qty,
                                limit_px=ref_price, order_type="market")
    return {"mode": "taker",
            "avg_px": float(result.get("avg_px") or ref_price),
            "total_sz": float(result.get("total_sz") or qty)}

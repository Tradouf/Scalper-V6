"""Sleeves SuperBot — une stratégie = une sleeve (SPEC §3).

Registre central : l'orchestrateur et le live résolvent une sleeve par son nom
(clé de best_params.json / allocations / caps) sans importer chaque module.
"""

from __future__ import annotations


def get_sleeve(name: str):
    """Instance (cachée) d'une sleeve par nom. KeyError si inconnue."""
    registry = _registry()
    return registry[name]


def all_sleeves() -> dict:
    return dict(_registry())


_CACHE = None


def _registry():
    global _CACHE
    if _CACHE is None:
        from superbot.sleeves.adaptive_ema import AdaptiveEMASleeve
        from superbot.sleeves.breakout import BreakoutSleeve
        from superbot.sleeves.momentum import MomentumSleeve
        _CACHE = {s.name: s for s in
                  (AdaptiveEMASleeve(), BreakoutSleeve(), MomentumSleeve())}
    return _CACHE

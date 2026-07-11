"""
Filtre qualité des symboles SimpleBot — post-traitement de l'optimiseur.

L'optimiseur walk-forward publie déjà active=True/False par symbole (PF valid ≥
MIN_VALID_PF, etc.). Ce module resserre la sélection pour concentrer le capital
sur les setups les plus robustes :

  1. allowlist / blocklist explicites ;
  2. seuils qualité plus stricts (PF, PnL, winrate train+valid) ;
  3. plafond MAX_ACTIVE_SYMBOLS : ne garde que le top-N par score de validation.

Les symboles déclassés restent dans best_params.json (active=False) avec
filter_reason pour traçabilité dashboard / logs.
"""

from __future__ import annotations

import logging
from typing import Dict, Tuple

from simplebot import config

logger = logging.getLogger("sdm.simplebot.symbol_filter")


def _valid_metrics(entry: dict) -> dict:
    return entry.get("valid") or {}


def _train_metrics(entry: dict) -> dict:
    return entry.get("train") or {}


def quality_score(entry: dict) -> float:
    """Score composite pour le classement top-N (plus haut = mieux)."""
    valid = _valid_metrics(entry)
    pf = float(valid.get("profit_factor", 0) or 0)
    pnl = float(valid.get("total_pnl_pct", 0) or 0)
    wr = float(valid.get("winrate", 0) or 0)
    n = int(valid.get("n_trades", 0) or 0)
    # Pénalise les confirmations sur très peu de trades malgré le seuil optimiseur.
    trade_factor = min(1.0, n / max(config.MIN_VALID_TRADES, 1))
    return pf * (1.0 + max(pnl, 0.0)) * (0.5 + wr) * trade_factor


def check_quality_gate(symbol: str, entry: dict) -> Tuple[bool, str]:
    """
    Vérifie qu'un symbole déjà actif côté optimiseur passe les critères qualité.
    Retourne (ok, reason) — reason vide si ok.
    """
    sym = symbol.upper()

    if config.SYMBOL_ALLOWLIST and sym not in config.SYMBOL_ALLOWLIST:
        return False, "allowlist"

    if sym in config.SYMBOL_BLOCKLIST:
        return False, "blocklist"

    valid = _valid_metrics(entry)
    train = _train_metrics(entry)

    pf_v = float(valid.get("profit_factor", 0) or 0)
    if pf_v < config.QUALITY_MIN_VALID_PF:
        return False, f"valid_pf<{config.QUALITY_MIN_VALID_PF:.2f}"

    pnl_v = float(valid.get("total_pnl_pct", 0) or 0)
    if pnl_v < config.QUALITY_MIN_VALID_PNL_PCT:
        return False, f"valid_pnl<{config.QUALITY_MIN_VALID_PNL_PCT:.4f}"

    wr_v = float(valid.get("winrate", 0) or 0)
    if wr_v < config.QUALITY_MIN_VALID_WINRATE:
        return False, f"valid_wr<{config.QUALITY_MIN_VALID_WINRATE:.2f}"

    pf_t = float(train.get("profit_factor", 0) or 0)
    if pf_t < config.QUALITY_MIN_TRAIN_PF:
        return False, f"train_pf<{config.QUALITY_MIN_TRAIN_PF:.2f}"

    pnl_t = float(train.get("total_pnl_pct", 0) or 0)
    if pnl_t <= 0:
        return False, "train_pnl<=0"

    return True, ""


def apply_symbol_filter(per_symbol: Dict[str, dict]) -> Dict[str, dict]:
    """
    Applique le filtre sur le dict symbols de l'optimiseur.
    Copie défensive : ne mute pas l'entrée d'origine si déjà partagée.
    """
    result: Dict[str, dict] = {}
    for symbol, entry in per_symbol.items():
        out = dict(entry)
        if out.get("active"):
            ok, reason = check_quality_gate(symbol, out)
            if not ok:
                out["active"] = False
                out["filter_reason"] = reason
        result[symbol] = out

    cap = config.MAX_ACTIVE_SYMBOLS
    if cap > 0:
        active = [(sym, ent) for sym, ent in result.items() if ent.get("active")]
        if len(active) > cap:
            active.sort(key=lambda item: quality_score(item[1]), reverse=True)
            keep = {sym for sym, _ in active[:cap]}
            for sym, ent in active[cap:]:
                ent["active"] = False
                ent["filter_reason"] = f"cap_top_{cap}"
            demoted = [sym for sym, _ in active[cap:]]
            logger.info(
                "Filtre symboles: cap %d — actifs %s | déclassés %s",
                cap, sorted(keep), demoted,
            )

    n_active = sum(1 for e in result.values() if e.get("active"))
    n_total = len(result)
    logger.info(
        "Filtre symboles: %d/%d actifs (PF≥%.2f, PnL≥%.2f%%, WR≥%.0f%%)",
        n_active, n_total,
        config.QUALITY_MIN_VALID_PF,
        config.QUALITY_MIN_VALID_PNL_PCT * 100,
        config.QUALITY_MIN_VALID_WINRATE * 100,
    )
    return result
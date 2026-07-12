"""
Optimiseur SuperBot — walk-forward multi-timeframe unifié (SPEC §8).

Pour chaque symbole de l'univers, pour chaque sleeve optimisable, pour chaque
timeframe déclaré par la sleeve :

  1. OHLCV (jours bornés au cap API du TF) ;
  2. split train 70 % / validation 30 % ;
  3. grille backtestée sur le TRAIN (mode maker), classée par (PnL, PF) ;
  4. gates train : n >= MIN_TRAIN_TRADES, PF >= MIN_TRAIN_PF, PnL > 0 ;
  5. les TOP_K du train sont rejoués sur la VALIDATION (jamais vue) —
     **filtre BINAIRE** : le PREMIER set du classement train qui confirme
     (n >= MIN_VALID_TRADES, PF >= MIN_VALID_PF, PnL > 0) est retenu.
     La validation n'est JAMAIS un critère de choix : sélectionner le meilleur
     PnL de validation réintroduirait l'overfit sur la fenêtre neutre.

Choix du timeframe (SPEC §3B) : le TF dont le set retenu a le meilleur
**score composite TRAIN** gagne — pas le meilleur valid (même principe
anti-snooping). À égalité, le TF le plus lent (moins de trades = moins de frais).

Post-traitement : symbol_filter (seuils qualité + cap top-N), écriture atomique
de best_params.json, historique JSONL.

Usage standalone : python -m superbot.optimizer
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Callable, List, Optional

from superbot import config
from superbot.backtester import run_sleeve_backtest
from superbot.data import fetch_closed
from superbot.sleeves.adaptive_ema import AdaptiveEMASleeve
from superbot.sleeves.base import Sleeve
from superbot.symbol_filter import apply_symbol_filter

logger = logging.getLogger("sdm.superbot.optimizer")

#: sleeves optimisées par le walk-forward (A/momentum = params figés, hors scope)
OPTIMIZABLE_SLEEVES: List[Sleeve] = [AdaptiveEMASleeve()]


def train_composite(train: dict) -> float:
    """Score composite calculé sur les métriques TRAIN uniquement — sert au
    choix du timeframe. Même forme que quality_score mais côté train : rester
    aveugle à la validation est ce qui la garde neutre."""
    pf = float(train.get("profit_factor", 0) or 0)
    pnl = float(train.get("total_pnl_pct", 0) or 0)
    wr = float(train.get("winrate", 0) or 0)
    n = int(train.get("n_trades", 0) or 0)
    trade_factor = min(1.0, n / max(config.MIN_TRAIN_TRADES, 1))
    return pf * (1.0 + max(pnl, 0.0)) * (0.5 + wr) * trade_factor


class SuperOptimizer:

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        fetch: Optional[Callable[..., List[dict]]] = None,
        state_file=None,
        backtest_fn: Optional[Callable] = None,
        sleeves: Optional[List[Sleeve]] = None,
    ):
        self.symbols = symbols or config.SYMBOLS
        self._fetch = fetch                      # None → fetch réseau simplebot
        self.state_file = state_file or config.BEST_PARAMS_FILE
        self._backtest = backtest_fn or run_sleeve_backtest
        self.sleeves = sleeves or OPTIMIZABLE_SLEEVES
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ── Walk-forward d'un (symbole, sleeve, timeframe) ───────────────────────

    def optimize_tf(self, sleeve: Sleeve, candles: List[dict]) -> Optional[dict]:
        """Retourne {"params", "train", "valid", "train_score"} pour le premier
        set du classement train qui confirme en validation, None sinon."""
        grid = sleeve.grid()
        if not grid:
            return None
        min_bars = max(sleeve.warmup_bars(p) for p in grid) * 2
        if len(candles) < min_bars:
            return None

        split = int(len(candles) * (1.0 - config.VALIDATION_RATIO))
        train_slice = candles[:split]

        ranked = []
        for p in grid:
            r = self._backtest(sleeve, train_slice, p)
            if (r.n_trades >= config.MIN_TRAIN_TRADES
                    and r.profit_factor >= config.MIN_TRAIN_PF
                    and r.total_pnl_pct > 0):
                ranked.append((p, r))
        # classement TRAIN : PnL puis PF (identique simplebot)
        ranked.sort(key=lambda pr: (pr[1].total_pnl_pct, pr[1].profit_factor),
                    reverse=True)

        for p, tr in ranked[: config.TOP_K_VALIDATION]:
            warmup = sleeve.warmup_bars(p)
            valid_slice = candles[max(0, split - warmup):]
            vr = self._backtest(sleeve, valid_slice, p,
                                start_index=min(warmup, split))
            ok = (vr.n_trades >= config.MIN_VALID_TRADES
                  and vr.profit_factor >= config.MIN_VALID_PF
                  and vr.total_pnl_pct > 0)
            if ok:
                train_m = tr.metrics()
                return {
                    "params": sleeve.params_to_dict(p),
                    "train": train_m,
                    "valid": vr.metrics(),
                    "train_score": round(train_composite(train_m), 4),
                }
        return None

    # ── Un symbole : tous TFs d'une sleeve, choix par composite TRAIN ────────

    def optimize_symbol(self, symbol: str, sleeve: Sleeve) -> dict:
        winners = {}   # tf -> winner dict
        for tf in sleeve.timeframes:
            try:
                candles = fetch_closed(symbol, tf, config.BACKTEST_DAYS,
                                       fetch=self._fetch)
            except Exception as e:
                logger.warning("%s %s: fetch échoué (%r)", symbol, tf, e)
                continue
            if config.FETCH_THROTTLE_SEC > 0 and self._fetch is None:
                time.sleep(config.FETCH_THROTTLE_SEC)
            w = self.optimize_tf(sleeve, candles)
            if w is not None:
                winners[tf] = w

        if not winners:
            return {"active": False, "sleeve": sleeve.name,
                    "reason": "aucun_set_confirme_sur_aucun_tf"}

        # Choix du TF : meilleur composite TRAIN ; égalité → TF le plus lent
        # (ordre de sleeve.timeframes supposé du plus rapide au plus lent).
        tf_order = {tf: i for i, tf in enumerate(sleeve.timeframes)}
        best_tf = max(winners,
                      key=lambda tf: (winners[tf]["train_score"], tf_order[tf]))
        w = winners[best_tf]
        return {
            "active": True,
            "sleeve": sleeve.name,
            "timeframe": best_tf,
            "params": w["params"],
            "train": w["train"],
            "valid": w["valid"],
            "train_score": w["train_score"],
            "tf_candidates": {tf: winners[tf]["train_score"] for tf in winners},
        }

    # ── Cycle complet ────────────────────────────────────────────────────────

    def run_once(self) -> dict:
        t0 = time.time()
        per_symbol = {}
        for sleeve in self.sleeves:
            logger.info("Optimisation %s — %d symboles × TFs %s, grille %d sets",
                        sleeve.name, len(self.symbols), list(sleeve.timeframes),
                        len(sleeve.grid()))
            for symbol in self.symbols:
                try:
                    entry = self.optimize_symbol(symbol, sleeve)
                except Exception as e:
                    logger.error("Optimisation %s/%s échouée: %r",
                                 symbol, sleeve.name, e)
                    entry = {"active": False, "sleeve": sleeve.name,
                             "reason": f"erreur: {e}"}
                per_symbol[symbol] = entry
                if entry.get("active"):
                    logger.info(
                        "  %s ✅ %s@%s %s | train PnL=%.2f%% PF=%.2f | valid PnL=%.2f%% PF=%.2f",
                        symbol, sleeve.name, entry["timeframe"], entry["params"],
                        entry["train"]["total_pnl_pct"] * 100,
                        entry["train"]["profit_factor"],
                        entry["valid"]["total_pnl_pct"] * 100,
                        entry["valid"]["profit_factor"],
                    )
                else:
                    logger.info("  %s ❌ inactif — %s", symbol, entry.get("reason"))

        per_symbol = apply_symbol_filter(per_symbol)

        state = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "backtest_days": config.BACKTEST_DAYS,
            "entry_mode": config.ENTRY_MODE,
            "symbols": per_symbol,
        }
        self._write_state(state)
        self._append_history(state)
        n_active = sum(1 for e in per_symbol.values() if e.get("active"))
        logger.info("Optimisation terminée en %.1fs — %d/%d actifs → %s",
                    time.time() - t0, n_active, len(per_symbol), self.state_file)
        return state

    def _write_state(self, state: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.state_file)

    def _append_history(self, state: dict) -> None:
        try:
            config.OPTIMIZER_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(config.OPTIMIZER_HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(state, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("Écriture historique échouée: %r", e)

    # ── Thread de fond (consommé par run.py en Phase 2) ─────────────────────

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop,
                                        name="SuperBotOptimizer", daemon=True)
        self._thread.start()
        logger.info("Optimiseur démarré (période %dh)",
                    config.OPTIMIZE_INTERVAL_SEC // 3600)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:
                logger.error("Cycle d'optimisation en erreur: %r", e, exc_info=True)
            self._stop.wait(config.OPTIMIZE_INTERVAL_SEC)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    SuperOptimizer().run_once()

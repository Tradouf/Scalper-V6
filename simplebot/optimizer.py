"""
BacktestOptimizerAgent — agent d'optimisation périodique des paramètres.

Toutes les OPTIMIZE_INTERVAL_SEC (6 h par défaut), pour chaque symbole :
1. télécharge BACKTEST_DAYS jours d'OHLCV ;
2. backteste toute la grille de paramètres sur la fenêtre de TRAIN (70 %) ;
3. les TOP_K meilleurs sets du train sont rejoués sur la fenêtre de
   VALIDATION (30 % restants, jamais vus) — anti-overfit walk-forward ;
4. le PREMIER set du classement train qui CONFIRME en validation
   (PF ≥ MIN_VALID_PF, PnL > 0, assez de trades) est publié dans
   simplebot/state/best_params.json. La validation est un filtre binaire,
   jamais un critère de choix : sélectionner le meilleur PnL de validation
   réintroduirait de l'overfit sur la fenêtre censée être neutre ;
5. si aucun set ne confirme, le symbole est marqué inactive → le live
   n'ouvre plus de position dessus (les TP/SL natifs des positions déjà
   ouvertes restent en place).

Le fichier est écrit de façon atomique (tmp + rename) : le live peut le
recharger à chaud sans jamais lire un JSON tronqué.

Usage standalone : python -m simplebot.optimizer
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Callable, List, Optional

from simplebot import config
from simplebot.backtester import BacktestResult, run_backtest
from simplebot.data import closed_candles, fetch_ohlcv
from simplebot.strategy import StrategyParams, param_grid

logger = logging.getLogger("sdm.simplebot.optimizer")


def _train_score(r: BacktestResult) -> tuple:
    return (r.total_pnl_pct, r.profit_factor)


class BacktestOptimizerAgent:

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        fetch: Optional[Callable[..., List[dict]]] = None,
        state_file=None,
    ):
        self.symbols = symbols or config.SYMBOLS
        self._fetch = fetch or fetch_ohlcv
        self.state_file = state_file or config.BEST_PARAMS_FILE
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ── Optimisation d'un symbole ────────────────────────────────────────────

    def optimize_symbol(self, candles: List[dict]) -> dict:
        """
        Retourne l'entrée à publier pour un symbole :
        {"active": bool, "params": ..., "train": ..., "valid": ..., "reason": ...}
        """
        grid = param_grid()
        min_bars = max(p.warmup_bars for p in grid) * 3
        if len(candles) < min_bars:
            return {"active": False, "reason": f"donnees_insuffisantes ({len(candles)} bougies)"}

        split = int(len(candles) * (1.0 - config.VALIDATION_RATIO))
        train = candles[:split]

        # Backtest de toute la grille sur le train
        train_results = [
            run_backtest(train, p, config.FEE_PCT, config.SLIPPAGE_PCT)
            for p in grid
        ]
        candidates = [r for r in train_results if r.n_trades >= config.MIN_TRAIN_TRADES]
        candidates.sort(key=_train_score, reverse=True)
        top = candidates[: config.TOP_K_VALIDATION]
        if not top:
            return {"active": False, "reason": "aucun_set_avec_assez_de_trades_en_train"}

        # Validation walk-forward : la fenêtre inclut le warmup mais seuls les
        # signaux après start_index (= dans la vraie fenêtre de valid) comptent.
        # Filtre binaire : les candidats sont parcourus dans l'ordre du train,
        # le premier qui confirme gagne (pas de sélection sur le PnL de valid).
        best = None
        best_valid: Optional[BacktestResult] = None
        for r in top:
            warmup = r.params.warmup_bars
            valid_slice = candles[max(0, split - warmup):]
            vr = run_backtest(
                valid_slice, r.params, config.FEE_PCT, config.SLIPPAGE_PCT,
                start_index=min(warmup, split),
            )
            ok = (
                vr.n_trades >= config.MIN_VALID_TRADES
                and vr.profit_factor >= config.MIN_VALID_PF
                and vr.total_pnl_pct > 0
            )
            if ok:
                best, best_valid = r, vr
                break

        if best is None or best_valid is None:
            return {"active": False, "reason": "aucun_set_confirme_en_validation"}

        return {
            "active": True,
            "params": best.params.to_dict(),
            "train": best.metrics(),
            "valid": best_valid.metrics(),
        }

    # ── Cycle complet ────────────────────────────────────────────────────────

    def run_once(self) -> dict:
        logger.info("Optimisation démarrée — %d symboles, grille de %d sets",
                    len(self.symbols), len(param_grid()))
        t0 = time.time()
        per_symbol = {}
        for symbol in self.symbols:
            try:
                candles = closed_candles(
                    self._fetch(symbol, config.INTERVAL, config.BACKTEST_DAYS),
                    config.INTERVAL_MS,
                )
                entry = self.optimize_symbol(candles)
            except Exception as e:
                logger.error("Optimisation %s échouée: %r", symbol, e)
                entry = {"active": False, "reason": f"erreur: {e}"}
            per_symbol[symbol] = entry
            if entry.get("active"):
                logger.info(
                    "  %s ✅ %s | train PnL=%.2f%% PF=%.2f | valid PnL=%.2f%% PF=%.2f",
                    symbol, entry["params"],
                    entry["train"]["total_pnl_pct"] * 100, entry["train"]["profit_factor"],
                    entry["valid"]["total_pnl_pct"] * 100, entry["valid"]["profit_factor"],
                )
            else:
                logger.info("  %s ❌ inactif — %s", symbol, entry.get("reason"))

        state = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "interval": config.INTERVAL,
            "backtest_days": config.BACKTEST_DAYS,
            "symbols": per_symbol,
        }
        self._write_state(state)
        self._append_history(state)
        logger.info("Optimisation terminée en %.1fs → %s", time.time() - t0, self.state_file)
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

    # ── Thread de fond ───────────────────────────────────────────────────────

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="SimpleBotOptimizer", daemon=True
        )
        self._thread.start()
        logger.info("Optimiseur démarré (période %dh)", config.OPTIMIZE_INTERVAL_SEC // 3600)

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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    BacktestOptimizerAgent().run_once()

"""
StrategyOptimizer — Salle des Marchés
Agent d'auto-optimisation des stratégies de trading.
Tourne en tâche de fond (thread), toutes les 24h.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Tuple

OPTIMIZE_INTERVAL_SEC = 86400
BACKTEST_DAYS         = 30
BACKTEST_INTERVAL     = "1h"
MIN_PF_TREND          = 1.5
MIN_PF_MOMENTUM       = 1.5
MAX_RISK_PCT          = 0.15
MIN_RISK_PCT          = 0.05
STATE_FILE            = "optimizer_state.json"
LOG_FILE              = "optimizer.log"

ALL_SYMBOLS = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "AVAX", "HYPE",
    "MON", "FARTCOIN", "ZEC", "SUI", "WLD", "TAO",
    "kPEPE", "LIT", "VVV", "DOGE", "LINK", "ARB", "OP",
]


def _setup_optimizer_logger() -> logging.Logger:
    logger = logging.getLogger("sdm.optimizer")
    if not logger.handlers:
        fmt = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
        handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        handler.setFormatter(logging.Formatter(fmt))
        logger.addHandler(handler)
        logger.addHandler(logging.StreamHandler())
        logger.setLevel(logging.INFO)
    return logger


class StrategyOptimizer:

    def __init__(self, exchange, supervisor=None):
        self.exchange   = exchange
        self.supervisor = supervisor
        self.logger     = _setup_optimizer_logger()
        self._thread    = None
        self._running   = False
        self.state: Dict = self._load_state()

    def start(self) -> None:
        self._running = True
        self._thread  = threading.Thread(
            target=self._loop,
            name="StrategyOptimizer",
            daemon=True,
        )
        self._thread.start()
        self.logger.info(
            "StrategyOptimizer démarré (interval=%dh)", OPTIMIZE_INTERVAL_SEC // 3600
        )

    def stop(self) -> None:
        self._running = False
        self.logger.info("StrategyOptimizer arrêté.")

    def _loop(self) -> None:
        try:
            self._run_optimization()
        except Exception as e:
            self.logger.error("Erreur optimisation initiale: %s", e, exc_info=True)

        while self._running:
            time.sleep(OPTIMIZE_INTERVAL_SEC)
            try:
                self._run_optimization()
            except Exception as e:
                self.logger.error("Erreur optimisation: %s", e, exc_info=True)

    def _run_optimization(self) -> None:
        self.logger.info("=" * 55)
        self.logger.info(
            " OPTIMISATION — %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
        self.logger.info("=" * 55)

        trend_results    = {}
        momentum_results = {}

        for symbol in ALL_SYMBOLS:
            t_pf, t_wr, t_pnl = self._backtest_symbol(symbol, "trend")
            m_pf, m_wr, m_pnl = self._backtest_symbol(symbol, "momentum")
            trend_results[symbol]    = {"pf": t_pf, "wr": t_wr, "pnl": t_pnl}
            momentum_results[symbol] = {"pf": m_pf, "wr": m_wr, "pnl": m_pnl}
            self.logger.info(
                "  %s | TREND PF=%.2f WR=%.0f%% | MOMENTUM PF=%.2f WR=%.0f%%",
                symbol.ljust(10), t_pf, t_wr * 100, m_pf, m_wr * 100,
            )

        # Nouvelle whitelist TREND
        new_whitelist = {
            sym for sym, r in trend_results.items()
            if r["pf"] >= MIN_PF_TREND
        }

        # Risk proportionnel au PF (PF=1.5 → 5%, PF=3.0 → 15%)
        new_risk_pct = {}
        for sym in new_whitelist:
            pf  = trend_results[sym]["pf"]
            raw = MIN_RISK_PCT + (pf - 1.5) / 1.5 * (MAX_RISK_PCT - MIN_RISK_PCT)
            new_risk_pct[sym] = round(min(max(raw, MIN_RISK_PCT), MAX_RISK_PCT), 3)

        # Décision momentum
        mom_pfs         = [r["pf"] for r in momentum_results.values() if r["pf"] > 0]
        avg_mom_pf      = sum(mom_pfs) / len(mom_pfs) if mom_pfs else 0
        momentum_active = avg_mom_pf >= MIN_PF_MOMENTUM

        self.logger.info("-" * 55)
        self.logger.info(
            "  Nouvelle whitelist TREND (%d actifs): %s",
            len(new_whitelist), ", ".join(sorted(new_whitelist))
        )
        for sym in sorted(new_whitelist):
            self.logger.info(
                "    %s → risk=%.0f%%  PF=%.2f",
                sym, new_risk_pct[sym] * 100, trend_results[sym]["pf"]
            )
        self.logger.info(
            "  Momentum avg PF=%.2f → %s",
            avg_mom_pf, "ACTIVE ✅" if momentum_active else "VEILLE ❌"
        )
        self.logger.info("-" * 55)

        self._apply_to_supervisor(new_whitelist, new_risk_pct, momentum_active)

        self.state = {
            "updated_at":       datetime.now().isoformat(),
            "trend_whitelist":  list(new_whitelist),
            "risk_pct":         new_risk_pct,
            "momentum_active":  momentum_active,
            "avg_momentum_pf":  round(avg_mom_pf, 3),
            "trend_results":    trend_results,
            "momentum_results": momentum_results,
        }
        self._save_state()
        self.logger.info("État sauvegardé → %s", STATE_FILE)

    def _backtest_symbol(self, symbol: str, strategy: str) -> Tuple[float, float, float]:
        try:
            from backtest.backtester import Backtester
            # Signature correcte : Backtester(exchange_client)
            bt     = Backtester(self.exchange)
            result = bt.run(
                symbol=symbol,
                strategy=strategy,
                interval=BACKTEST_INTERVAL,
                days=BACKTEST_DAYS,
            )
            if result is None:
                return 0.0, 0.0, 0.0

            # BacktestResult est un objet avec attributs
            pf  = getattr(result, "profit_factor", 0.0) or 0.0
            wr  = getattr(result, "winrate",       0.0) or 0.0
            pnl = getattr(result, "total_pnl",     0.0) or 0.0
            return float(pf), float(wr), float(pnl)

        except Exception as e:
            self.logger.debug("Backtest %s/%s échoué: %s", symbol, strategy, e)
            return 0.0, 0.0, 0.0

    def _apply_to_supervisor(self, new_whitelist, new_risk_pct, momentum_active) -> None:
        try:
            import supervisor.trading_supervisor as ts_module

            # PROTECTION : ne jamais vider la whitelist complètement
            if len(new_whitelist) == 0:
                self.logger.warning(
                    "⚠️  Whitelist vide après backtest — conservation de l'ancienne whitelist !"
                )
                return

            ts_module.TREND_WHITELIST.clear()
            ts_module.TREND_WHITELIST.update(new_whitelist)

            ts_module.RISK_PCT_BY_SYMBOL.clear()
            ts_module.RISK_PCT_BY_SYMBOL.update(new_risk_pct)

            if momentum_active:
                self.logger.info("🟢 MOMENTUM réactivé en production !")
            else:
                self.logger.info("🔴 MOMENTUM reste en veille.")

            self.logger.info("✅ Supervisor mis à jour en live (sans redémarrage)")

        except Exception as e:
            self.logger.error("Erreur mise à jour supervisor: %s", e)

    def _load_state(self) -> dict:
        if Path(STATE_FILE).exists():
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    state = json.load(f)
                self.logger.info(
                    "État chargé depuis %s (last update: %s)",
                    STATE_FILE, state.get("updated_at", "?")
                )
                return state
            except Exception:
                pass
        return {}

    def _save_state(self) -> None:
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.logger.error("Erreur sauvegarde état: %s", e)


if __name__ == "__main__":
    from exchanges.hyperliquid import HyperliquidExchangeClient
    exchange  = HyperliquidExchangeClient(enable_trading=False)
    optimizer = StrategyOptimizer(exchange, supervisor=None)
    optimizer._run_optimization()

"""
TradingSupervisor v3.1 — Salle des Marchés
Orchestre tous les agents IA H24.

Nouveautés v3.1 :
- Trailing stop en % du PnL (remplace les seuils de prix bruts)
- SL fixe :   -0.5 % du PnL  → sortie immédiate
- TP armé :   +0.20 % du PnL  → active le trailing
- Trailing :  sortie si recul de 0.05 % depuis le pic PnL
- _position_guards étendu : best_pnl_pct, trail_armed
- Toute la logique de sortie reste dans _monitor_positions()
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import datetime
from typing import List

from exchanges.hyperliquid import HyperliquidExchangeClient
from exchanges.base import OrderRequest
from agents.market_scanner import MarketScannerAgent
from agents.strategy_trend import TradeSignal
from agents.agent_trader import AgentTrader
from agents.risk_manager import RiskManagerAgent
from agents.strategy_optimizer import StrategyOptimizer

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

SCAN_INTERVAL_SEC   = 300
POSITION_CHECK_SEC  = 60
TOP_N_MARKETS       = 15
MIN_CONFIDENCE      = 0.65
LOG_FILE            = "supervisor.log"

# ── Trailing stop en % de PnL ──────────────────
SL_PCT          = -0.0035   # -0.35 % → stop loss fixe
TP_ARM_PCT      =  0.0060   # +0.60 % → arme le trailing
TRAIL_DROP_PCT  =  0.0025   # 0.25 % de recul depuis le pic → sortie

# Whitelist dynamique — mise à jour toutes les 24h par l'optimizer
TREND_WHITELIST = {"BTC", "MON", "SOL", "FARTCOIN", "ZEC", "AVAX", "HYPE", "SUI"}

RISK_PCT_BY_SYMBOL = {
    "BTC":      0.12,
    "MON":      0.12,
    "SOL":      0.12,
    "FARTCOIN": 0.10,
    "ZEC":      0.10,
    "AVAX":     0.10,
    "HYPE":     0.10,
    "SUI":      0.10,
}
DEFAULT_RISK_PCT = 0.10

# ──────────────────────────────────────────────
# Setup logging
# ──────────────────────────────────────────────

def _setup_logging() -> logging.Logger:
    fmt = "%(asctime)s,%(msecs)03d [%(levelname)s] %(name)s — %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )
    return logging.getLogger("sdm.supervisor")

# ──────────────────────────────────────────────
# Superviseur
# ──────────────────────────────────────────────

class TradingSupervisor:

    def __init__(self):
        self.logger = _setup_logging()
        self._running = True

        self.logger.info("=" * 60)
        self.logger.info(" SALLE DES MARCHES v3.1 — Démarrage %s",
                         datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.logger.info(" Cerveau : AgentTrader LLM (deepseek-coder-v2)")
        self.logger.info(" Trailing stop : SL=%.2f%%  TP_arm=%.2f%%  Trail=%.2f%%",
                         SL_PCT * 100, TP_ARM_PCT * 100, TRAIL_DROP_PCT * 100)
        self.logger.info(" Whitelist init : %s", ", ".join(sorted(TREND_WHITELIST)))
        self.logger.info(" Optimizer : backtest auto toutes les 24h")
        self.logger.info("=" * 60)

        self.exchange     = HyperliquidExchangeClient(enable_trading=True)
        self.scanner      = MarketScannerAgent(self.exchange)
        self.agent_trader = AgentTrader(self.exchange)
        self.risk         = RiskManagerAgent(self.exchange)
        self.optimizer    = StrategyOptimizer(self.exchange, supervisor=self)

        # Structure garde étendue :
        # {symbol: {side, entry, sl, tp, oid, best_pnl_pct, trail_armed}}
        self._position_guards: dict = {}

        signal.signal(signal.SIGINT,  self._handle_stop)
        signal.signal(signal.SIGTERM, self._handle_stop)

        self.optimizer.start()

    # ──────────────────────────────────────────
    # Boucle principale
    # ──────────────────────────────────────────

    def run(self) -> None:
        last_scan_time     = 0.0
        last_position_time = 0.0
        self.logger.info("Boucle H24 démarrée. Scan toutes les %ds.", SCAN_INTERVAL_SEC)

        while self._running:
            now = time.time()

            if now - last_scan_time >= SCAN_INTERVAL_SEC:
                try:
                    self._trading_cycle()
                except Exception as e:
                    self.logger.error("Erreur cycle trading: %s", e, exc_info=True)
                last_scan_time = time.time()

            if now - last_position_time >= POSITION_CHECK_SEC:
                try:
                    self._monitor_positions()
                except Exception as e:
                    self.logger.error("Erreur monitoring: %s", e, exc_info=True)
                last_position_time = time.time()

            time.sleep(5)

        self.logger.info("Superviseur arrêté proprement.")

    # ──────────────────────────────────────────
    # Cycle scan → LLM → risk → ordre
    # ──────────────────────────────────────────

    def _trading_cycle(self) -> None:
        self.logger.info("Nouveau cycle trading")

        capital = self.risk.get_capital()
        self.logger.info("Capital disponible: %.2f USDT", capital)
        if capital < 10:
            self.logger.warning("Capital insuffisant (%.2f USDT), cycle ignoré.", capital)
            return

        # 1. Scanner les marchés
        top_markets = self.scanner.scan(top_n=TOP_N_MARKETS)

        # 2. AgentTrader LLM analyse chaque marché et décide
        self.logger.info("🤖 AgentTrader analyse %d marchés...", len(top_markets))
        all_signals: List[TradeSignal] = self.agent_trader.analyze(top_markets)

        # 3. Filtrer sur la whitelist dynamique
        signals = [s for s in all_signals if s.symbol in TREND_WHITELIST]

        self.logger.info("%d signal(s) LLM détecté(s) (%d dans whitelist)",
                         len(all_signals), len(signals))

        if not signals:
            self.logger.info("Aucun signal whitelist — attente prochain cycle.")
            return

        # 4. Trier par confiance, dédupliquer par symbole
        signals.sort(key=lambda s: s.confidence, reverse=True)
        seen, filtered = set(), []
        for s in signals:
            if s.symbol not in seen and s.confidence >= MIN_CONFIDENCE:
                filtered.append(s)
                seen.add(s.symbol)

        self.logger.info("%d signal(s) après filtrage (conf >= %.2f)",
                         len(filtered), MIN_CONFIDENCE)

        # 5. Exécuter
        for sig in filtered:
            self._execute_signal(sig, capital)

    # ──────────────────────────────────────────
    # Exécution signal
    # ──────────────────────────────────────────

    def _execute_signal(self, sig: TradeSignal, capital: float) -> None:
        risk_pct = RISK_PCT_BY_SYMBOL.get(sig.symbol, DEFAULT_RISK_PCT)
        notional = capital * risk_pct
        qty      = notional / sig.entry_price

        req = OrderRequest(
            symbol     = sig.symbol,
            side       = sig.side,
            qty        = round(qty, 6),
            order_type = "limit",
            price      = round(sig.entry_price, 4),
            leverage   = 5.0,
        )

        decision = self.risk.validate(req, spread_bps=0.0)

        if not decision.approved:
            self.logger.warning("Signal %s %s REJETÉ: %s",
                                sig.side.upper(), sig.symbol, decision.reason)
            return

        req.qty      = decision.adjusted_qty      if decision.adjusted_qty      is not None else req.qty
        req.leverage = decision.adjusted_leverage if decision.adjusted_leverage is not None else req.leverage

        try:
            result = self.exchange.place_order(req)
            self.logger.info(
                "✅ ORDRE %s %s qty=%.6f @ %.4f — status=%s oid=%s [risk=%.0f%%]",
                req.side.upper(), req.symbol, req.qty, req.price,
                result.status, result.order_id, risk_pct * 100,
            )
            # Garde étendue avec champs trailing
            self._position_guards[sig.symbol] = {
                "side":         sig.side,
                "entry":        sig.entry_price,
                "sl":           sig.stop_loss,       # conservé pour affichage log
                "tp":           sig.take_profit,      # conservé pour affichage log
                "oid":          result.order_id,
                "best_pnl_pct": 0.0,                  # pic de PnL observé
                "trail_armed":  False,                 # trailing actif ?
            }
        except Exception as e:
            self.logger.error("Erreur envoi ordre %s %s: %s", sig.side, sig.symbol, e)

    # ──────────────────────────────────────────
    # Surveillance trailing stop en % du PnL
    # ──────────────────────────────────────────

    def _monitor_positions(self) -> None:
        positions = self.exchange.get_positions()
        if not positions:
            return

        for pos in positions:
            symbol = pos.symbol
            guard  = self._position_guards.get(symbol)

            if guard is None:
                continue

            # Récupération du prix courant
            price = pos.entry_price
            try:
                ticker = self.exchange._client.get_ticker(symbol)
                price  = float(ticker.get("price", pos.entry_price))
            except Exception:
                pass

            entry = guard["entry"]
            side  = guard["side"]

            if entry == 0:
                continue

            # ── Calcul PnL % ──────────────────────────────
            if side == "buy":
                pnl_pct = (price - entry) / entry
            else:
                pnl_pct = (entry - price) / entry

            # ── Mise à jour du pic PnL ────────────────────
            if pnl_pct > guard["best_pnl_pct"]:
                guard["best_pnl_pct"] = pnl_pct

            best = guard["best_pnl_pct"]

            # ── Armer le trailing dès que TP_ARM_PCT atteint ──
            if not guard["trail_armed"] and best >= TP_ARM_PCT:
                guard["trail_armed"] = True
                self.logger.info(
                    "🔒 TRAIL ARMÉ %s PnL pic=+%.3f%%",
                    symbol, best * 100,
                )

            # ── Décision de sortie ────────────────────────
            reason = None

            # 1. Stop loss fixe
            if pnl_pct <= SL_PCT:
                reason = f"SL ({pnl_pct*100:.3f}% <= {SL_PCT*100:.3f}%)"

            # 2. Trailing stop (seulement si armé)
            elif guard["trail_armed"] and (best - pnl_pct) >= TRAIL_DROP_PCT:
                reason = (
                    f"TRAIL ({pnl_pct*100:.3f}% / pic={best*100:.3f}% "
                    f"/ recul={((best-pnl_pct)*100):.3f}%)"
                )

            if reason:
                self.logger.warning(
                    "⛔ SORTIE %s %s PnL=%.3f%% — %s",
                    side.upper(), symbol, pnl_pct * 100, reason,
                )
                self._close_position(symbol, reason, price, guard)

            else:
                self.logger.info(
                    "📊 %s %s PnL=%.3f%% best=%.3f%% armed=%s",
                    side.upper(), symbol, pnl_pct * 100, best * 100,
                    guard["trail_armed"],
                )

    # ──────────────────────────────────────────
    # Fermeture position
    # ──────────────────────────────────────────

    def _close_position(self, symbol: str, reason: str,
                        exit_price: float, guard: dict) -> None:
        try:
            result = self.exchange._client.market_close(symbol)
            self.logger.info("Position %s fermée (%s) — %s", symbol, reason, result)

            # Enregistrement dans la mémoire de l'AgentTrader
            self.agent_trader.record_result(
                symbol      = symbol,
                side        = guard.get("side", "buy"),
                entry       = guard.get("entry", exit_price),
                exit_price  = exit_price,
                reason      = reason,
            )
            self._position_guards.pop(symbol, None)
            self.risk.update_pnl(0.0)
        except Exception as e:
            self.logger.error("Erreur fermeture %s: %s", symbol, e)

    # ──────────────────────────────────────────
    # Arrêt propre
    # ──────────────────────────────────────────

    def _handle_stop(self, signum, frame) -> None:
        self.logger.info("Signal d'arrêt reçu — fermeture propre...")
        self.optimizer.stop()

        try:
            positions = self.exchange.get_positions()
            if positions:
                self.logger.info("Fermeture de %d position(s)...", len(positions))
                for pos in positions:
                    if float(pos.qty) != 0:
                        try:
                            self.exchange._client.market_close(pos.symbol)
                            self.logger.info("✅ %s fermé.", pos.symbol)
                        except Exception as e:
                            self.logger.error("❌ %s: %s", pos.symbol, e)
            else:
                self.logger.info("Aucune position ouverte.")
        except Exception as e:
            self.logger.error("Erreur positions: %s", e)

        self._running = False

# ──────────────────────────────────────────────
# Point d'entrée
# ──────────────────────────────────────────────

def main():
    supervisor = TradingSupervisor()
    supervisor.run()

if __name__ == "__main__":
    main()

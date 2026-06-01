#!/usr/bin/env python3
"""
V7 main — composition root + boucle d'événements.

Démarrage :
  source .venv/bin/activate
  python3 main.py

Modes :
  - paper (défaut, lit ExecutionConfig.paper_mode) : PaperExchange interne,
    ne touche pas HL en écriture. Lit HL en read-only pour les prix et candles.
  - live (paper_mode=False dans config) : envoie de vrais ordres HL.
    Pour le MVP P7, on reste en paper.

Boucle :
  1. Charge config + composants
  2. Toutes les 30s :
     - Récupère MarketSnapshot (candles 1h pour chaque symbole, mark prices)
     - detector.detect → RegimeState
     - strategies.generate_signals
     - allocator.allocate → TargetPortfolio
     - risk.project → projeté
     - execution.reconcile → orders
     - execution.submit → fills
     - distribue les fills aux strategies via on_fill
     - persiste l'état (memory/v7_state.json) pour le dashboard
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import logging.handlers
import os
import signal
import sys
import time
from pathlib import Path
from typing import Dict, List

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))


def _setup_logging() -> None:
    log_dir = REPO / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "v7.log"
    fmt = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


def _load_env() -> str:
    env = REPO / ".env"
    if not env.exists():
        return ""
    for line in env.read_text().splitlines():
        if line.startswith("HL_ACCOUNT_ADDRESS="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


class V7Bot:
    def __init__(self) -> None:
        from core.config import load_config
        from execution.hyperliquid_adapter import HyperliquidReadAdapter
        from execution.engine import ExecutionEngine
        from execution.paper import PaperExchange
        from execution.portfolio import PortfolioImpl
        from regime.detector import RuleBasedRegimeDetector
        from strategies.mean_reversion import MeanReversionStrategy
        from strategies.momentum import MomentumStrategy
        from strategies.supertrend import SupertrendStrategy
        from strategies.grid_engine import GridEngine
        from strategies.grid import GridStrategy
        from allocation.allocator import RuleBasedAllocator
        from allocation.performance import PerformanceScorer
        from risk.manager import RiskManager

        self.logger = logging.getLogger("v7.main")
        self.cfg = load_config()
        self.logger.info("V7 config chargée. symbols=%s paper=%s",
                         self.cfg.symbols, self.cfg.execution.paper_mode)

        # HL read-only adapter
        addr = _load_env()
        if not addr:
            self.logger.warning("HL_ACCOUNT_ADDRESS manquant dans .env — equity à 0")
        self.hl_read = HyperliquidReadAdapter(account_address=addr)

        # Exchange : paper ou live
        if self.cfg.execution.paper_mode:
            self.exchange = PaperExchange(get_mark_price=self.hl_read.get_mark_price)
            self.logger.info("Mode PAPER : PaperExchange instancié")
        else:
            from execution.hyperliquid_write_adapter import HyperliquidWriteAdapter
            self.exchange = HyperliquidWriteAdapter(enable_trading=True)
            self.logger.warning(
                "Mode LIVE : HyperliquidWriteAdapter instancié — vrais ordres HL. "
                "Vérifier reconcile boot, Fix #2/#4/#8 portés avant production."
            )

        # Composants
        self.detector = RuleBasedRegimeDetector(self.cfg.regime)
        self.mr = MeanReversionStrategy(self.cfg.strategies.mean_reversion, self.cfg.symbols)
        self.momentum = MomentumStrategy(self.cfg.strategies.momentum, self.cfg.symbols)
        # Supertrend : sizing dynamique, lit l'equity courante via callback
        self.supertrend = SupertrendStrategy(
            self.cfg.strategies.supertrend, self.cfg.symbols,
            equity_callback=lambda: self.portfolio.equity,
        )
        # Grid : utilise notre exchange (paper). Sa FSM placera des limits via paper.
        self.grid_engine = GridEngine(self.exchange, self.cfg.strategies.grid)
        self.grid = GridStrategy(self.grid_engine, symbols=self.cfg.symbols)
        self.strategies = [self.mr, self.momentum, self.supertrend, self.grid]

        self.allocator = RuleBasedAllocator(self.cfg.allocation)
        self.scorer = PerformanceScorer(
            mult_min=self.cfg.allocation.mult_min,
            mult_max=self.cfg.allocation.mult_max,
            halflife_days=self.cfg.allocation.perf_halflife_days,
        )
        self.risk = RiskManager(self.cfg.risk)
        self.exec = ExecutionEngine(
            self.exchange, self.cfg.execution,
            prices_callback=self.hl_read.get_mark_price,
        )
        self.portfolio = PortfolioImpl(_equity=1000.0)  # initial paper equity

        # Boot reconciler : seulement en mode live (paper démarre vide à chaque restart).
        # Sync positions HL + equity + registry (ghost/orphan) avant le 1er tick.
        if not self.cfg.execution.paper_mode:
            from execution.boot_reconciler import BootReconciler
            br = BootReconciler(self.hl_read, self.exchange, self.portfolio)
            summary = br.reconcile()
            self.logger.info(
                "BootReconciler résumé: positions=%d equity=$%.2f orders=%d ghosts=%d orphans=%d errors=%d",
                summary["positions_loaded"], summary["equity"],
                summary["orders_live"], summary["ghosts_purged"],
                summary["orphans_absorbed"], len(summary["errors"]),
            )
            if summary["errors"]:
                self.logger.warning("BootReconciler erreurs: %s", summary["errors"])

        # EmergencyExitManager : filet de sécurité par-position (port V6 + Fix #8).
        # Instancié toujours mais no-op en paper (PaperExchange ne sync pas HL).
        from risk.emergency_exit import EmergencyExitManager
        self.emergency = EmergencyExitManager(
            cfg=self.cfg.risk,
            read_adapter=self.hl_read,
            write_adapter=self.exchange,
            portfolio=self.portfolio,
            paper_mode=self.cfg.execution.paper_mode,
        )

        self._running = True
        self._cycle = 0

    # ─── Boucle principale ────────────────────────────────────────────────────

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT, self._handle_stop)

        interval = 30  # secondes
        self.logger.info("V7 boucle démarrée (interval=%ds)", interval)
        while self._running:
            try:
                self._cycle += 1
                t0 = time.time()
                self._tick()
                elapsed = time.time() - t0
                if elapsed > interval:
                    self.logger.warning("Tick V7 #%d a pris %.1fs > %ds", self._cycle, elapsed, interval)
                sleep_s = max(0.0, interval - elapsed)
                if sleep_s > 0:
                    time.sleep(sleep_s)
            except KeyboardInterrupt:
                break
            except Exception as e:
                self.logger.exception("Tick V7 exception: %r", e)
                time.sleep(10)
        self.logger.info("V7 boucle arrêtée proprement (cycles=%d)", self._cycle)
        return 0

    def _handle_stop(self, *_) -> None:
        self._running = False
        self.logger.info("V7 SIGTERM/SIGINT reçu, arrêt en cours...")

    def _drive_grid(self, market, regime, prices) -> None:
        """Pilote le grid_engine : activation conditionnelle + tick FSM.

        Activation : poids grid (matrice B) × equity > activation_threshold_usdc
                    AND régime range dominant (proba RANGE > 0.4)
                    AND pas déjà actif sur ce symbole.
        Désactivation : régime hors range OU breakout/drift géré par grid_engine.
        """
        from regime.features import atr as compute_atr
        # Calcul du poids grid courant
        weights = self.allocator.get_weights(regime, self.scorer.scores())
        grid_w = weights.get("grid", 0.0)
        equity = max(self.portfolio.equity, 100.0)
        # Budget total alloué au grid (toutes positions confondues)
        grid_total_budget = grid_w * equity
        n_syms = max(len(self.cfg.symbols), 1)
        budget_per_sym = grid_total_budget / n_syms
        # Le grid s'active si budget >= activation_threshold ET régime range probable
        prob_range = regime.probabilities.get(__import__("core.types", fromlist=["Regime"]).Regime.RANGE, 0.0)
        should_activate = (
            budget_per_sym >= self.cfg.strategies.grid.activation_threshold_usdc
            and prob_range > 0.3
        )

        for sym in self.cfg.symbols:
            # Configure le budget côté grid (info pour le Signal généré ensuite)
            try:
                self.grid.set_budget(sym, budget_per_sym)
            except Exception:
                pass
            is_active = self.grid_engine.is_active(sym)
            if not is_active and should_activate:
                # Activate : calcule mid + ATR depuis les candles
                candles = market.candles.get(sym, [])
                if len(candles) < 30:
                    continue
                mid = float(candles[-1].close)
                atr_val = compute_atr(
                    [c.high for c in candles],
                    [c.low for c in candles],
                    [c.close for c in candles],
                    period=14,
                )
                if atr_val is None or atr_val <= 0:
                    continue
                ok = self.grid_engine.activate(sym, mid, atr_val)
                if ok:
                    self.logger.info("Grid ACTIVATED %s center=%.4f atr=%.4f budget=$%.0f", sym, mid, atr_val, budget_per_sym)
            elif is_active and not should_activate:
                self.grid_engine.deactivate(sym, cancel=True)
                self.logger.info("Grid DEACTIVATED %s (regime=%s prob_range=%.2f)", sym, regime.label.value, prob_range)

        # Tick FSM pour chaque grid actif (place TPs, recycle, drift, health check)
        try:
            open_oids = {int(o["oid"]) for o in self.exchange.get_open_orders() if o.get("oid")}
        except Exception:
            open_oids = set()
        # Lecture des positions HL réelles UNE FOIS par tick (en live).
        # Les fills internes du grid_engine ne passent PAS par ExecutionEngine →
        # portfolio.positions ne les reflète pas. Sans cette lecture HL, le
        # check G2 du grid voit szi=0 → "TP impossible" → frozen → trous sur
        # l'UI HL (observé en V7 live 31/05-01/06, 84 frozen avec szi=0.000000).
        hl_pos_detailed = {}
        if not self.cfg.execution.paper_mode:
            try:
                hl_pos_detailed = self.hl_read.get_positions_detailed()
            except Exception as e:
                self.logger.warning("HL positions read for grid szi: %r", e)
        for sym in list(self.grid_engine.active_symbols()):
            price = prices.get(sym, 0.0)
            if price <= 0:
                continue
            if hl_pos_detailed:
                # Live : szi signé réel depuis clearinghouseState
                szi = float(hl_pos_detailed.get(sym, {}).get("szi", 0.0))
            else:
                # Paper : fallback portfolio.positions (PaperExchange est local)
                current_notional = self.portfolio.positions.get(sym, 0.0)
                szi = current_notional / price if price > 0 else 0.0
            try:
                self.grid_engine.on_tick(
                    sym, open_oids=open_oids,
                    current_price=price,
                    position_szi=szi,
                    cache_fresh=True,
                )
            except Exception as e:
                self.logger.warning("Grid on_tick %s error: %r", sym, e)

    def _tick(self) -> None:
        from core.types import MarketSnapshot

        # 1. Snapshot du marché
        prices = self.hl_read.get_all_mids()
        if not prices:
            self.logger.warning("Tick V7 #%d : pas de prix, skip", self._cycle)
            return
        # Met à jour les marks du paper exchange
        for sym, px in prices.items():
            if sym in self.cfg.symbols:
                self.exchange.update_mark_price(sym, px)

        # Charge les candles 1h pour les symboles
        candles_dict = {}
        for sym in self.cfg.symbols:
            c = self.hl_read.get_candles(sym, interval="1h", limit=200)
            if c:
                candles_dict[sym] = c

        if not candles_dict:
            self.logger.warning("Tick V7 #%d : pas de candles, skip", self._cycle)
            return

        market = MarketSnapshot(
            timestamp=dt.datetime.utcnow(),
            candles=candles_dict,
            prices={s: prices.get(s, 0.0) for s in self.cfg.symbols},
        )

        # 2. Régime
        regime = self.detector.detect(market)

        # 2.5. Grid driver (séparé du flow Signal car le grid est event-driven).
        # On donne au grid son budget par symbole (= poids grid × equity / N syms),
        # et on appelle son on_tick pour faire avancer la FSM (fills internes,
        # health check, drift guard...).
        try:
            self._drive_grid(market, regime, prices)
        except Exception as e:
            self.logger.warning("Grid driver error: %r", e)

        # 2.7. Sync des positions tracées par stratégie avec la réalité exchange.
        # SÉCURITÉ anti-whipsaw : les stratégies ré-émettent leur exposition tant
        # qu'elles se croient en position (maintien). Si une position est fermée
        # hors stratégie (EmergencyExit, SL natif, liquidation), il faut purger
        # cette croyance, sinon le maintien la ré-ouvrirait en boucle.
        try:
            net_by_asset = (
                dict(self.portfolio.positions)
                if self.cfg.execution.paper_mode
                else self.hl_read.get_positions()
            )
            for strat in self.strategies:
                if hasattr(strat, "sync_positions"):
                    strat.sync_positions(net_by_asset)
        except Exception as e:
            self.logger.warning("sync_positions error: %r", e)

        # 3. Signaux (toutes stratégies)
        all_signals = []
        for strat in self.strategies:
            try:
                sigs = strat.generate_signals(market)
                all_signals.extend(sigs)
            except Exception as e:
                self.logger.warning("Strategy %s error: %r", strat.strategy_id, e)

        # 4. Perf scores
        perf_scores = self.scorer.scores()

        # 5. Allocate
        target = self.allocator.allocate(all_signals, regime, self.portfolio, perf_scores)

        # 6. Risk project
        from risk.state import RiskStateImpl
        equity = self.hl_read.get_equity() if not self.cfg.execution.paper_mode else self.portfolio.equity
        if equity > 0:
            self.portfolio.set_equity(equity)
        risk_state = RiskStateImpl(equity=self.portfolio.equity)
        projected = self.risk.project(target, risk_state)

        # 6.5. EmergencyExit : check ROE positions HL réelles, force-close si dépasse
        # seuil. AVANT reconcile pour laisser submit recomposer après les force-close.
        try:
            em = self.emergency.check_and_exit()
            if em["tracked_emergency"] + em["orphan_force_closed"] + em["orphan_grace_armed"] > 0:
                self.logger.warning(
                    "EmergencyExit tick: tracked_forced=%d orphan_armed=%d orphan_forced=%d (checked=%d)",
                    em["tracked_emergency"], em["orphan_grace_armed"],
                    em["orphan_force_closed"], em["checked"],
                )
        except Exception as e:
            self.logger.warning("EmergencyExit error: %r", e)

        # 7. Reconcile + submit
        orders = self.exec.reconcile(projected, self.portfolio)
        fills = self.exec.submit(orders)

        # 8. Update portfolio + distribute fills
        for f in fills:
            self.portfolio.adjust_position(f.asset, f.notional)
            # Coût des fees
            self.portfolio.adjust_equity(-f.fee)
            # Distribute aux stratégies
            for strat in self.strategies:
                if strat.strategy_id == f.strategy_id:
                    try:
                        strat.on_fill(f)
                    except Exception as e:
                        self.logger.warning("Strategy %s on_fill: %r", strat.strategy_id, e)
            self.scorer.on_fill(f)

        # 9. Log
        active_signals = [s for s in all_signals if s.target_notional > 0]
        self.logger.info(
            "tick #%d  regime=%s conf=%.2f  sig_act=%d/%d orders=%d fills=%d  "
            "equity=$%.2f positions=%d",
            self._cycle, regime.label.value, regime.confidence,
            len(active_signals), len(all_signals), len(orders), len(fills),
            self.portfolio.equity, len(self.portfolio.positions),
        )

        # 10. Persiste l'état pour le dashboard
        self._persist_state(regime, all_signals, target, projected, fills)

    # ─── Persistence ──────────────────────────────────────────────────────────

    def _persist_state(self, regime, signals, target, projected, fills) -> None:
        mem = REPO / "memory"
        mem.mkdir(exist_ok=True)
        # Cumulative counters (reset chaque restart)
        if not hasattr(self, "_total_fills"):
            self._total_fills = 0
            self._total_orders = 0
            self._total_signals = 0
        self._total_fills += len(fills)
        self._total_signals += len(signals)
        active_signals = [s for s in signals if s.target_notional > 0]

        # Détail signaux pour le dashboard
        signals_detail = []
        for s in signals:
            signals_detail.append({
                "strategy": s.strategy_id,
                "asset": s.asset,
                "direction": s.direction,
                "target_notional": s.target_notional,
                "confidence": s.confidence,
                "edge_bps": s.expected_edge_bps,
            })

        try:
            state = {
                "ts": time.time(),
                "cycle": self._cycle,
                "regime": {
                    "label": regime.label.value,
                    "confidence": regime.confidence,
                    "probabilities": {r.value: p for r, p in regime.probabilities.items()},
                },
                "weights": self.allocator.get_weights(regime, self.scorer.scores()),
                "perf_scores": self.scorer.scores(),
                "signals_count": len(signals),
                "signals_active_count": len(active_signals),
                "signals_detail": signals_detail,
                "target_gross": target.gross_exposure,
                "target_net": target.net_exposure,
                "projected_gross": projected.gross_exposure,
                "projected_net": projected.net_exposure,
                "portfolio_equity": self.portfolio.equity,
                "portfolio_positions": self.portfolio.positions,
                "fills_count_this_cycle": len(fills),
                # Compteurs cumulés depuis boot
                "cumulative_fills": self._total_fills,
                "cumulative_signals": self._total_signals,
            }
            (mem / "v7_state.json").write_text(json.dumps(state, indent=2, default=str))
        except Exception as e:
            self.logger.debug("persist v7_state: %r", e)


def main() -> int:
    _setup_logging()
    bot = V7Bot()
    return bot.run()


if __name__ == "__main__":
    sys.exit(main())

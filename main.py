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
        # Bandit d'exécution (2026-06-06) : OFF par défaut (exec_bandit_active).
        # ON → la politique apprise en shadow choisit market vs limit GTC.
        # Fail-open taker intégré à BanditPolicy (état absent, <500 obs, HF stale).
        bandit = None
        if self.cfg.execution.exec_bandit_active and not self.cfg.execution.paper_mode:
            from execution.bandit_policy import BanditPolicy
            bandit = BanditPolicy()
            self.logger.warning("Bandit exécution ACTIF — limit adaptatif appris "
                                "(fallback market, timeout 30s)")
        self.exec = ExecutionEngine(
            self.exchange, self.cfg.execution,
            prices_callback=self.hl_read.get_mark_price,
            bandit=bandit,
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
        # Verrou partagé : protège grid_engine._grids entre le thread principal
        # (activation/désactivation) et le thread grille dédié (FSM on_tick).
        import threading
        self._grid_lock = threading.RLock()
        self._grid_thread = None
        self._grid_safety_pause = False  # enveloppe sécurité high_vol (vol ingérable)

    # ─── Boucle principale ────────────────────────────────────────────────────

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT, self._handle_stop)

        interval = 30  # secondes
        # Thread grille dédié (port V6) : pilote la FSM grille à cadence rapide,
        # découplé du tick analytique lent. Évite l'épidémie szi=0→frozen.
        if getattr(self.cfg.strategies.grid, "fast_loop_enabled", True):
            import threading
            self._grid_thread = threading.Thread(
                target=self._grid_loop, daemon=True, name="grid-loop",
            )
            self._grid_thread.start()
            self.logger.info(
                "Grid fast loop démarré (cadence=%ds)",
                int(getattr(self.cfg.strategies.grid, "fast_loop_sec", 3)),
            )
        # Watchdog : re-exec le process si le tick principal ne progresse plus
        # (filet contre un éventuel hang résiduel, ex. blocage I/O). Le re-exec
        # remplace l'image process → fonctionne même si le thread principal est figé.
        self._last_tick_ts = time.time()
        import threading as _th
        _th.Thread(target=self._watchdog_loop, daemon=True, name="watchdog").start()

        self.logger.info("V7 boucle démarrée (interval=%ds)", interval)
        while self._running:
            try:
                self._cycle += 1
                t0 = time.time()
                self._tick()
                self._last_tick_ts = time.time()
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

    def _watchdog_loop(self) -> None:
        """Surveille la progression du tick principal. Si aucun tick depuis
        WATCHDOG_STALL_SEC, re-exec le process (auto-guérison anti-hang)."""
        stall_sec = float(os.environ.get("WATCHDOG_STALL_SEC", "180"))
        while self._running:
            time.sleep(30)
            age = time.time() - getattr(self, "_last_tick_ts", time.time())
            if age > stall_sec:
                self.logger.critical(
                    "WATCHDOG : aucun tick depuis %.0fs (> %.0fs) → re-exec du process",
                    age, stall_sec,
                )
                try:
                    sys.stdout.flush()
                    sys.stderr.flush()
                except Exception:
                    pass
                os.execv(sys.executable, [sys.executable, str(REPO / "main.py")])

    def _drive_grid(self, market, regime, prices) -> None:
        """Pilote le grid_engine : activation conditionnelle + tick FSM.

        2026-06-07 — la grille est active en HIGH_VOL (profil resserré) ET en
        RANGE (moissonneuse de fond, biais momentum 24h, priorité MR par
        préemption). Désactivation : régime trend OU breakout/drift OU sécurité
        vol OU préemption MR sur le symbole.
        """
        from regime.features import atr as compute_atr
        from core.types import Regime
        weights = self.allocator.get_weights(regime, self.scorer.scores())
        grid_w = weights.get("grid", 0.0)
        equity = max(self.portfolio.equity, 100.0)
        # Budget grille : poids allocateur en high_vol (inchangé) ; en RANGE,
        # fraction dédiée range_budget_frac — la grille est hors allocateur
        # (matrice range = 100% MR) pour ne pas diluer la taille des entrées MR.
        if regime.label == Regime.RANGE:
            grid_total_budget = self.cfg.strategies.grid.range_budget_frac * equity
        else:
            grid_total_budget = grid_w * equity
        n_syms = max(len(self.cfg.symbols), 1)
        budget_per_sym = grid_total_budget / n_syms
        # La grille s'active en HIGH_VOL (profil encadré) et, depuis 2026-06-07,
        # en RANGE comme moissonneuse de fond — avec PRIORITÉ MR : elle s'efface
        # (préemption plus bas) des symboles où MR est engagée.
        is_high_vol = regime.label == Regime.HIGH_VOL
        is_grid_regime = is_high_vol or regime.label == Regime.RANGE
        # Profil resserré uniquement en high_vol ; en range, ATR factor standard.
        hv_factor = (
            float(getattr(self.cfg.strategies.grid, "high_vol_atr_factor", 0.25))
            if is_high_vol else float(self.cfg.strategies.grid.atr_factor)
        )

        # Enveloppe de sécurité : si la vol réalisée explose (> mult × médiane), la
        # grille high_vol se ferait rincer → pause + flat (hystérésis : reprise <0.8×).
        safety_mult = float(getattr(self.cfg.strategies.grid, "high_vol_safety_mult", 2.5))
        vol_ratio = self._avg_vol_ratio(market)
        if not self._grid_safety_pause and vol_ratio > safety_mult:
            self._grid_safety_pause = True
            self.logger.warning(
                "GRID SÉCURITÉ : vol réalisée %.2f× médiane (> %.1f×) → flat + pause grilles",
                vol_ratio, safety_mult,
            )
        elif self._grid_safety_pause and vol_ratio < safety_mult * 0.8:
            self._grid_safety_pause = False
            self.logger.info("GRID SÉCURITÉ : vol revenue (%.2f×) → reprise autorisée", vol_ratio)

        should_activate = (
            is_grid_regime
            and not self._grid_safety_pause
            and budget_per_sym >= self.cfg.strategies.grid.activation_threshold_usdc
        )
        # Priorité MR (2026-06-07) : symboles occupés par MR interdits à la grille.
        mr_engaged = self.mr.engaged_symbols()
        # En pause sécurité : on flatte les grilles encore actives (position incluse).
        if self._grid_safety_pause:
            with self._grid_lock:
                for sym in list(self.grid_engine.active_symbols()):
                    try:
                        self.grid_engine.deactivate(sym, cancel=True, close_position=True)
                        self.logger.warning("GRID SÉCURITÉ : %s flatté (vol ingérable)", sym)
                    except Exception as e:
                        self.logger.warning("GRID SÉCURITÉ deactivate %s: %r", sym, e)

        for sym in self.cfg.symbols:
            # Heartbeat watchdog (2026-06-07) : l'activation séquentielle de
            # 8 grilles (~20s de fetch candles chacune) dépassait les 180s du
            # watchdog au boot → re-exec en plein vol + double batch d'ordres
            # (observé 10:48). On signale la progression ; un vrai hang HTTP
            # à l'intérieur d'une itération déclenche toujours après 180s.
            self._last_tick_ts = time.time()
            # Configure le budget côté grid (info pour le Signal généré ensuite)
            try:
                self.grid.set_budget(sym, budget_per_sym)
            except Exception:
                pass
            is_active = self.grid_engine.is_active(sym)
            try:
                with self._grid_lock:
                    # Préemption MR : grille active sur un symbole où MR vient de
                    # s'engager → flat immédiat (position fermée, ordres annulés).
                    # MR entre ensuite via l'allocateur avec sa taille propre.
                    if is_active and sym in mr_engaged:
                        self.grid_engine.deactivate(sym, cancel=True, close_position=True)
                        self.logger.info("GRID %s PRÉEMPTÉE par MR (flat + cancel, priorité MR)", sym)
                    elif not is_active and should_activate and sym not in mr_engaged:
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
                        # Biais directionnel par momentum 24h (2026-06-07) :
                        # un marché qui repart ne doit pas se faire shorter
                        # par la grille — long-only en hausse, inverse en baisse.
                        bias = "neutral"
                        if len(candles) >= 25:
                            c24 = float(candles[-25].close)
                            mom = (mid / c24 - 1.0) if c24 > 0 else 0.0
                            thr = float(self.cfg.strategies.grid.bias_momentum_pct)
                            if mom > thr:
                                bias = "long"
                            elif mom < -thr:
                                bias = "short"
                        if self.grid_engine.activate(sym, mid, atr_val, atr_factor=hv_factor, bias=bias):
                            self.logger.info("Grid ACTIVATED %s center=%.4f atr=%.4f factor=%.2f budget=$%.0f bias=%s (%s)", sym, mid, atr_val, hv_factor, budget_per_sym, bias, regime.label.value)
                    elif is_active and not should_activate:
                        self.grid_engine.deactivate(sym, cancel=True)
                        self.logger.info("Grid DEACTIVATED %s (regime=%s prob_range=%.2f)", sym, regime.label.value, prob_range)
            except Exception as e:
                self.logger.warning("Grid activation %s error: %r", sym, e)

        # NB : la FSM grille (pose TP, dégel, drift, health check) n'est PLUS
        # pilotée ici. Elle tourne dans le thread dédié _grid_loop (cadence ~3s)
        # pour réagir vite aux fills — sinon entre deux ticks lents (30-157s),
        # buy ET sell se remplissent → net szi=0 → gel massif. Cf. _grid_fast_tick.

    def _grid_fast_tick(self) -> None:
        """FSM grille pour chaque symbole actif. Appelée par le thread _grid_loop
        à cadence rapide (port de la boucle dédiée V6). Découple le pilotage grille
        du tick analytique lent (candles/signaux/allocation)."""
        active = list(self.grid_engine.active_symbols())
        if not active:
            return
        # Cache du dernier mid connu : sur ReadTimeout HL (fréquent), on réutilise
        # le dernier prix valide au lieu de skip — sinon le check breakout/drift
        # ne s'évalue jamais quand l'API rame (cas BNB sorti de range non détecté).
        try:
            fresh = self.hl_read.get_all_mids() or {}
        except Exception as e:
            fresh = {}
            self.logger.warning("grid fast tick: get_all_mids error: %r — fallback last mids", e)
        last = getattr(self, "_last_mids", {})
        if fresh:
            last.update(fresh)
            self._last_mids = last
        prices = self._last_mids = last
        try:
            open_oids = {int(o["oid"]) for o in self.exchange.get_open_orders() if o.get("oid")}
        except Exception:
            open_oids = set()
        # Lecture des positions HL réelles (en live) : les fills internes du grid
        # ne passent pas par l'ExecutionEngine → portfolio ne les reflète pas.
        hl_pos_detailed = {}
        if not self.cfg.execution.paper_mode:
            try:
                hl_pos_detailed = self.hl_read.get_positions_detailed()
            except Exception as e:
                self.logger.warning("grid fast tick: positions read error: %r", e)
        for sym in active:
            price = prices.get(sym, 0.0)
            if price <= 0:
                continue
            if hl_pos_detailed:
                szi = float(hl_pos_detailed.get(sym, {}).get("szi", 0.0))
            else:
                cur = self.portfolio.positions.get(sym, 0.0)
                szi = cur / price if price > 0 else 0.0
            try:
                with self._grid_lock:
                    self.grid_engine.on_tick(
                        sym, open_oids=open_oids,
                        current_price=price,
                        position_szi=szi,
                        cache_fresh=True,
                    )
            except Exception as e:
                self.logger.warning("Grid on_tick %s error: %r", sym, e)

    def _avg_vol_ratio(self, market) -> float:
        """Ratio moyen (vol réalisée 24h courante / médiane historique) sur les
        symboles. >1 = vol au-dessus de la normale. Sert à l'enveloppe de sécurité
        high_vol (vol ingérable → flat grilles)."""
        import numpy as np
        ratios = []
        for c in market.candles.values():
            if not c or len(c) < 50:
                continue
            closes = np.array([x.close for x in c], dtype=float)
            rets = np.diff(np.log(np.maximum(closes, 1e-12)))
            if len(rets) < 48:
                continue
            cur = float(np.std(rets[-24:]))
            hist = [float(np.std(rets[i - 24:i])) for i in range(24, len(rets))]
            base = float(np.median(hist)) if hist else 0.0
            if base > 1e-9:
                ratios.append(cur / base)
        return float(np.mean(ratios)) if ratios else 1.0

    def _grid_loop(self) -> None:
        """Thread démon : pilote la FSM grille à cadence rapide (port V6 _grid_loop)."""
        sec = int(getattr(self.cfg.strategies.grid, "fast_loop_sec", 3))
        time.sleep(5)  # délai boot : laisse le 1er tick activer des grilles
        while self._running:
            try:
                self._grid_fast_tick()
            except Exception as e:
                self.logger.warning("grid loop error: %r", e)
            time.sleep(sec)

    def _tick(self) -> None:
        from core.types import MarketSnapshot

        # 1. Snapshot du marché
        prices = self.hl_read.get_all_mids()
        if not prices:
            self.logger.warning("Tick V7 #%d : pas de prix, skip", self._cycle)
            return
        # Met à jour les marks du paper exchange (no-op en live : le write adapter
        # n'a pas update_mark_price → sans ce guard, le tick LIVE entier avortait
        # avec AttributeError, 90 occurrences observées le 31/05).
        if hasattr(self.exchange, "update_mark_price"):
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

        # 5. Allocate — les signaux grid sont EXCLUS : la grille gère son book
        # via sa propre FSM (limits + TP). Les laisser passer faisait dupliquer
        # par l'engine une fraction (poids×conf) de l'inventaire grille en
        # position parallèle → cause racine des 203 HyperliquidClientError
        # "Notional < $10" du 2026-06-02 (audit) : l'engine tentait de
        # refléter ~$7-8 d'inventaire et HL rejetait.
        directional_signals = [s for s in all_signals if s.strategy_id != self.grid.strategy_id]
        target = self.allocator.allocate(directional_signals, regime, self.portfolio, perf_scores)

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
        # Limits bandit en attente (no-op si bandit OFF ou aucune pending) :
        # fills tardifs + fallbacks market après timeout, distribués comme les autres.
        fills += self.exec.poll_pending()

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
                "paper_mode": self.cfg.execution.paper_mode,
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
                # Logique par position (stratégie propriétaire + métrique + intent)
                "positions_logic": self._collect_positions_logic(),
                # État des grilles actives (niveaux par état, drift)
                "grids": self._collect_grids(),
                # Positions HL réelles (live) : szi/entry/PnL/ROE
                "hl_positions": self._collect_hl_positions(),
                "grid_fast_loop": bool(getattr(self.cfg.strategies.grid, "fast_loop_enabled", True)),
            }
            (mem / "v7_state.json").write_text(json.dumps(state, indent=2, default=str))
        except Exception as e:
            self.logger.debug("persist v7_state: %r", e)

    # ─── Collecteurs pour le dashboard ────────────────────────────────────────

    def _collect_positions_logic(self) -> dict:
        """Pour chaque symbole tenu par une stratégie directionnelle : qui le tient,
        son intent (direction/notional/confidence ré-émis chaque tick) et la métrique
        qui justifie la position (z-score MR, slope momentum, direction supertrend)."""
        logic: dict = {}
        metric_key = {"mean_reversion": "z", "momentum": "slope_z", "supertrend": "direction"}
        for sid, strat in (("mean_reversion", self.mr), ("momentum", self.momentum), ("supertrend", self.supertrend)):
            try:
                positions = strat.open_positions()
                intents = getattr(strat, "_intent", {})
                metrics = strat.get_last_metrics()
            except Exception:
                continue
            for sym, pos in positions.items():
                it = intents.get(sym, {})
                m = metrics.get(sym, {})
                logic.setdefault(sym, []).append({
                    "strategy": sid,
                    "side": pos.get("side"),
                    "entry_px": pos.get("entry_px"),
                    "qty": pos.get("qty"),
                    "intent_notional": it.get("target_notional"),
                    "intent_confidence": it.get("confidence"),
                    "metric_name": metric_key.get(sid),
                    "metric_value": m.get(metric_key.get(sid)),
                })
        return logic

    def _collect_grids(self) -> dict:
        """État des grilles actives : center/spacing + comptage des niveaux par état."""
        out: dict = {}
        try:
            with self._grid_lock:
                grids = dict(getattr(self.grid_engine, "_grids", {}))
            for sym, g in grids.items():
                states: dict = {}
                for lvl in g.levels:
                    states[lvl.state] = states.get(lvl.state, 0) + 1
                out[sym] = {
                    "center": g.center,
                    "spacing": g.spacing,
                    "n_levels": len(g.levels),
                    "states": states,
                    "drift": getattr(g, "drift_since", None) is not None,
                    "breakout_limit": getattr(g, "breakout_limit", None),
                }
        except Exception as e:
            self.logger.debug("collect_grids: %r", e)
        return out

    def _collect_hl_positions(self) -> dict:
        """Positions HL réelles (live) : szi signé, entry, mark, ROE. {} en paper."""
        if self.cfg.execution.paper_mode:
            return {}
        try:
            return self.hl_read.get_positions_detailed() or {}
        except Exception as e:
            self.logger.debug("collect_hl_positions: %r", e)
            return {}


def main() -> int:
    _setup_logging()
    bot = V7Bot()
    return bot.run()


if __name__ == "__main__":
    sys.exit(main())

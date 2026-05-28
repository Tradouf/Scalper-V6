"""
Backtester walk-forward V7 — version MVP.

Limitations MVP :
  - Stratégies testées : MR + Momentum (le Grid sera évalué en paper trading P7
    car simuler ses limit orders persistants demande un orderbook simulator).
  - Fills exécutés au close du bar, frais taker uniquement.
  - Funding négligé (≈ 0 sur durée backtest, plus complexe à modéliser fidèlement).
  - Pas de modèle d'impact dynamique (slippage constant).

Flow :
  Pour t = start..end :
    1. Build MarketSnapshot(closes[:t+1], prices=close_t, ...)
    2. regime = detector.detect(market)
    3. signals = [strat.generate_signals(market) for strat in strategies]
    4. perf_scores = scorer.scores()
    5. target = allocator.allocate(signals, regime, portfolio, perf_scores)
    6. projected = risk_manager.project(target, risk_state)
    7. reconcile diff portfolio → projected, simuler fills
    8. distribuer fills aux strats via on_fill
    9. record equity, fills, etc.

Hypothèse forte sur le PnL : entre 2 bars, le PnL d'une position est
(close_t+1 - close_t) × qty (mark-to-market).
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from allocation.allocator import RuleBasedAllocator
from allocation.performance import PerformanceScorer
from backtest.costs import CostModel
from backtest.metrics import BacktestMetrics, compute_metrics
from core.config import V7Config
from core.types import Candle, Fill, MarketSnapshot, Signal, TargetPortfolio
from regime.detector import RuleBasedRegimeDetector
from risk.manager import RiskManager
from risk.state import RiskStateImpl
from strategies.mean_reversion import MeanReversionStrategy
from strategies.momentum import MomentumStrategy

logger = logging.getLogger("v7.backtest")


@dataclass
class BacktestState:
    """État interne du backtester."""

    cash: float
    equity: float
    positions: Dict[str, float] = field(default_factory=dict)  # asset → notional signé
    peak_equity: float = 0.0
    initial_equity: float = 0.0

    def update_equity(self, prices: Dict[str, float], cost_paid: float = 0.0) -> None:
        """Met à jour l'equity en marquant les positions au mark.
        equity = cash + Σ positions[asset] × (price[asset] / entry — simplifié : on
        traite notional comme la valeur courante, ce qui suppose qty fixe et prix variable).

        En réalité une position en notional signé représente la valeur USD courante.
        On adapte en stockant qty (notional/entry) au moment du fill, puis on
        recalcule à chaque update via les prices courants. Pour le MVP, on garde
        notional comme proxy.
        """
        self.peak_equity = max(self.peak_equity, self.equity)


def load_market_data(
    symbols: list[str],
    parquet_dir: Path,
    start: Optional[dt.datetime] = None,
    end: Optional[dt.datetime] = None,
) -> Dict[str, List[Candle]]:
    """Charge les candles 1h pour chaque symbole depuis parquet_dir."""
    out: Dict[str, List[Candle]] = {}
    for sym in symbols:
        path = parquet_dir / f"ohlcv_1h_{sym}.parquet"
        if not path.exists():
            logger.warning("Backtest : pas de parquet pour %s à %s", sym, path)
            continue
        df = pd.read_parquet(path)
        # Cast types numériques (HL renvoie en string)
        for col in ("open", "close", "high", "low", "volume"):
            if col in df.columns:
                df[col] = df[col].astype(float)
        if "ts_open" in df.columns:
            df["ts"] = pd.to_datetime(df["ts_open"], unit="ms")
        if start:
            df = df[df["ts"] >= start]
        if end:
            df = df[df["ts"] <= end]
        candles = [
            Candle(
                ts_open=row["ts"].to_pydatetime(),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume", 0) or 0),
            )
            for _, row in df.iterrows()
        ]
        out[sym] = candles
    return out


class Backtester:
    """Walk-forward backtester pour V7 (MR + Momentum).

    Usage :
        bt = Backtester(cfg, candles_dict, initial_equity=1000.0)
        metrics = bt.run()
        print(metrics.print_report())
    """

    def __init__(
        self,
        cfg: V7Config,
        candles: Dict[str, List[Candle]],
        initial_equity: float = 1000.0,
        warmup_bars: int = 120,
    ) -> None:
        self._cfg = cfg
        self._candles = candles
        self._warmup = warmup_bars
        # Composants V7
        self._detector = RuleBasedRegimeDetector(cfg.regime)
        self._mr = MeanReversionStrategy(cfg.strategies.mean_reversion, list(candles.keys()))
        self._momentum = MomentumStrategy(cfg.strategies.momentum, list(candles.keys()))
        self._strategies = [self._mr, self._momentum]
        self._allocator = RuleBasedAllocator(cfg.allocation)
        self._risk = RiskManager(cfg.risk)
        self._scorer = PerformanceScorer(
            mult_min=cfg.allocation.mult_min,
            mult_max=cfg.allocation.mult_max,
            halflife_days=cfg.allocation.perf_halflife_days,
            min_days_for_score=7,
        )
        # Coût
        self._costs = CostModel()
        # État
        self._state = BacktestState(cash=initial_equity, equity=initial_equity, initial_equity=initial_equity, peak_equity=initial_equity)
        # Journaux
        self._equity_curve: List[float] = []
        self._fills_log: List[dict] = []
        self._regime_history: List[tuple] = []
        # Aligne les candles : prend le min des longueurs (toutes doivent commencer à la même date)
        self._n_bars = min((len(c) for c in candles.values()), default=0)
        logger.info("Backtester init : %d symbols, %d bars, initial=$%.2f", len(candles), self._n_bars, initial_equity)

    def run(self) -> BacktestMetrics:
        if self._n_bars < self._warmup + 10:
            logger.warning("Pas assez de bars (%d) pour backtester (warmup=%d)", self._n_bars, self._warmup)
            return BacktestMetrics()

        for t in range(self._warmup, self._n_bars):
            self._step(t)
            if t % 500 == 0:
                logger.info(
                    "BT t=%d/%d  equity=$%.2f  positions=%d  fills=%d",
                    t, self._n_bars, self._state.equity, len(self._state.positions), len(self._fills_log),
                )

        return compute_metrics(
            equity_curve=self._equity_curve,
            fills_log=self._fills_log,
            funding_log=None,
            bars_per_year=365 * 24,
        )

    # ─── Pas-à-pas ────────────────────────────────────────────────────────────

    def _step(self, t: int) -> None:
        # Build snapshot
        candles_at_t = {sym: c[: t + 1] for sym, c in self._candles.items()}
        prices = {sym: c[t].close for sym, c in self._candles.items()}
        ts = self._candles[next(iter(self._candles))][t].ts_open
        market = MarketSnapshot(timestamp=ts, candles=candles_at_t, prices=prices)

        # 1. Régime
        regime = self._detector.detect(market)
        self._regime_history.append((ts, regime.label, regime.confidence))

        # 2. Signaux
        all_signals: List[Signal] = []
        for strat in self._strategies:
            try:
                all_signals.extend(strat.generate_signals(market))
            except Exception as e:
                logger.warning("Strategy %s error: %r", strat.strategy_id, e)

        # 3. Perf scores
        perf_scores = self._scorer.scores()

        # 4. Allocate + project
        # Portfolio dummy : on injecte juste l'equity (pour les caps)
        from dataclasses import dataclass as _dc

        @_dc
        class _Pf:
            positions: dict
            equity: float

        pf = _Pf(positions=dict(self._state.positions), equity=self._state.equity)
        target = self._allocator.allocate(all_signals, regime, pf, perf_scores)
        risk_state = RiskStateImpl(
            equity=self._state.equity,
            current_drawdown=max(0.0, 1.0 - self._state.equity / max(self._state.peak_equity, 1e-9)),
            daily_pnl_pct=0.0,  # MVP : on ne tracke pas daily PnL séparément
        )
        projected = self._risk.project(target, risk_state)

        # 5. Reconcile : pour chaque position cible vs courante, calculer le diff et exécuter
        # Inclut aussi les CLOSE signals : pour les assets où on a une position MAIS
        # une stratégie a émis CLOSE, on doit fermer.
        close_intentions = {
            s.asset for s in all_signals
            if s.target_notional == 0.0 and s.direction == 0.0
        }
        target_by_asset = {p.asset: p for p in projected.positions}
        current_by_asset = dict(self._state.positions)
        all_assets = set(target_by_asset.keys()) | set(current_by_asset.keys()) | close_intentions

        for asset in all_assets:
            target_n = target_by_asset.get(asset)
            current_n = current_by_asset.get(asset, 0.0)
            wanted = target_n.target_notional if target_n else 0.0
            # Si CLOSE explicite, on force la cible à 0
            if asset in close_intentions:
                wanted = 0.0
            diff = wanted - current_n
            if abs(diff) < 1.0:  # bande de non-trade : < 1 USD
                continue
            # Détermine la stratégie qui contribue le plus à ce diff
            if target_n and target_n.contributing_strategies:
                top_strat = max(target_n.contributing_strategies.items(), key=lambda kv: abs(kv[1]))[0]
            elif asset in close_intentions:
                # CLOSE : on attribue à la stratégie qui détient la position
                top_strat = self._infer_owning_strategy(asset)
            else:
                top_strat = "_unknown"

            price = prices.get(asset, 0.0)
            if price <= 0:
                continue

            # Simuler le fill
            fill_notional_signed = diff
            fee = self._costs.fee(abs(fill_notional_signed), is_taker=True)
            slippage = self._costs.slippage(abs(fill_notional_signed))
            cost = fee + slippage
            self._state.cash -= cost
            self._state.positions[asset] = wanted
            if abs(wanted) < 1e-9:
                self._state.positions.pop(asset, None)

            # Approxime closedPnl : si on inverse une position, le PnL réalisé
            # est (price - entry_avg) × qty. Pour le MVP simple, on tracke pas
            # l'entry_avg → on attribue le PnL via le mark-to-market global et
            # closedPnl=0 sur le fill (= comme HL closedPnl sur des ouvertures).
            # Pour les CLOSE de position, le closedPnl != 0 :
            closed_pnl = 0.0
            if abs(wanted) < abs(current_n):  # réduction de position
                # Approxime : on prend la fraction réduite × variation de prix sur les
                # N derniers bars. Trop imprécis pour MVP → on garde 0 et on dépend
                # uniquement de la dérive equity entre bars.
                pass

            fill = Fill(
                order_id=f"bt_{t}_{asset}",
                asset=asset,
                notional=fill_notional_signed,
                price=price,
                fee=cost,
                strategy_id=top_strat,
                timestamp=ts,
            )
            self._fills_log.append({
                "asset": asset, "notional": fill_notional_signed,
                "price": price, "fee": cost, "strategy_id": top_strat,
                "closedPnl": closed_pnl, "timestamp": ts,
            })
            # Distribuer aux stratégies
            for strat in self._strategies:
                if strat.strategy_id == top_strat:
                    try:
                        strat.on_fill(fill)
                    except Exception as e:
                        logger.warning("Strategy %s on_fill error: %r", strat.strategy_id, e)

        # 6. Marquer le portefeuille au mark (PnL inter-bars)
        if t + 1 < self._n_bars:
            next_prices = {sym: c[t + 1].close for sym, c in self._candles.items()}
            pnl_step = 0.0
            for asset, notional in self._state.positions.items():
                p_now = prices.get(asset, 0.0)
                p_next = next_prices.get(asset, p_now)
                if p_now <= 0 or p_next <= 0:
                    continue
                # notional est en USD au prix p_now. La quantité = notional / p_now.
                # PnL = qty × (p_next - p_now) = notional × (p_next/p_now - 1).
                pnl_step += notional * (p_next / p_now - 1)
            self._state.cash += pnl_step
            # Ajuste les notionals des positions au nouveau prix (rééchelonnement)
            # car notional représente la valeur USD courante.
            for asset, notional in list(self._state.positions.items()):
                p_now = prices.get(asset, 0.0)
                p_next = next_prices.get(asset, p_now)
                if p_now > 0 and p_next > 0:
                    self._state.positions[asset] = notional * (p_next / p_now)

        # 7. Equity
        gross_positions = sum(abs(n) for n in self._state.positions.values())
        # equity = cash + valeur nette des positions (déjà au mark via cash update)
        # Pour le MVP simple : equity = cash (les positions sont au mark via cash update à chaque step)
        self._state.equity = self._state.cash
        self._state.peak_equity = max(self._state.peak_equity, self._state.equity)
        self._equity_curve.append(self._state.equity)

    def _infer_owning_strategy(self, asset: str) -> str:
        """Trouve la stratégie qui détient une position sur cet asset."""
        for strat in self._strategies:
            try:
                opens = getattr(strat, "open_positions", lambda: {})()
                if asset in opens:
                    return strat.strategy_id
            except Exception:
                pass
        return "_unknown"

    # ─── Accessors ────────────────────────────────────────────────────────────

    @property
    def equity_curve(self) -> List[float]:
        return list(self._equity_curve)

    @property
    def fills_log(self) -> List[dict]:
        return list(self._fills_log)

    @property
    def regime_history(self) -> List[tuple]:
        return list(self._regime_history)

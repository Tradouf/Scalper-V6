"""
SupertrendStrategy — trend-following classique avec stop trailing intégré.

Indicateur Supertrend :
  - ATR(period) avec Wilder smoothing
  - median = (high+low)/2
  - upper_band = median + multiplier × ATR  ;  lower_band = median - multiplier × ATR
  - if close > prev_st : st = max(lower_band, prev_st) ; direction = +1
  - if close < prev_st : st = min(upper_band, prev_st) ; direction = -1

Signal :
  - FLIP de direction à l'instant t (dir[-1] != dir[-2]) →
        direction nouvelle = +1 → LONG  ;  -1 → SHORT
  - stop_price = valeur courante du supertrend (= stop trailing dynamique)
  - Aucun signal si pas de flip OU si position MOM/MR existante sur ce symbole

Sortie :
  - Flip inverse → CLOSE (target_notional=0, direction=0)
  - SL natif posé par l'Execution Engine au stop_price suit le supertrend

Sizing :
  - Risque par trade = risk_per_trade_pct × equity
  - qty = risk_per_trade_pct × equity / (mark - stop_price)
  - notional = qty × mark, clampé dans [notional_min, notional_max]
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Dict, Optional

import numpy as np

from core.config import SupertrendStrategyConfig
from core.types import Fill, MarketSnapshot, Signal
from regime.features import supertrend_with_history

logger = logging.getLogger("v7.strategy.supertrend")


class SupertrendStrategy:
    """StrategyAgent déterministe Supertrend ATR-based."""

    def __init__(
        self,
        cfg: SupertrendStrategyConfig,
        symbols: list[str],
        equity_callback=None,  # callable() -> float (pour sizing dynamique)
        strategy_id: str = "supertrend",
    ) -> None:
        self._cfg = cfg
        self._symbols = list(symbols)
        self._strategy_id = strategy_id
        self._equity_cb = equity_callback
        # Dernière direction observée par symbole → détection de FLIP
        self._last_direction: Dict[str, int] = {}
        # Cooldown post-flip
        self._last_signal_ts: Dict[str, float] = {}
        # Tracking interne des positions ST ouvertes
        self._positions: Dict[str, Dict] = {}
        # Intention d'allocation à ré-émettre tant que la position est tenue
        # (V7 level-triggered : un HOLD silencieux = fermeture par l'allocateur).
        self._intent: Dict[str, Dict] = {}  # symbol → {direction, target_notional, confidence}
        # Snapshot debug
        self._last_metrics: Dict[str, Dict] = {}

    @property
    def strategy_id(self) -> str:
        return self._strategy_id

    def generate_signals(self, market: MarketSnapshot) -> list[Signal]:
        signals: list[Signal] = []
        now_ts = market.timestamp.timestamp() if isinstance(market.timestamp, dt.datetime) else time.time()
        for sym in self._symbols:
            sig = self._evaluate(sym, market, now_ts)
            if sig is not None:
                signals.append(sig)
        return signals

    def on_fill(self, fill: Fill) -> None:
        sym = fill.asset
        if sym not in self._symbols:
            return
        existing = self._positions.get(sym)
        if existing is None:
            self._positions[sym] = {
                "side": "buy" if fill.notional > 0 else "sell",
                "qty": abs(fill.notional / fill.price),
                "entry_px": fill.price,
                "opened_ts": fill.timestamp.timestamp() if isinstance(fill.timestamp, dt.datetime) else time.time(),
            }
        else:
            signed_existing = (1 if existing["side"] == "buy" else -1) * existing["qty"] * existing["entry_px"]
            new_signed = signed_existing + fill.notional
            if abs(new_signed) < 0.01:
                self._positions.pop(sym, None)
                self._intent.pop(sym, None)
            else:
                existing["qty"] = abs(new_signed / fill.price)
                existing["side"] = "buy" if new_signed > 0 else "sell"

    def sync_positions(self, net_by_asset: Dict[str, float], dust: float = 1.0) -> None:
        """Purge les positions tracées fermées hors stratégie (EmergencyExit, SL,
        liquidation). Empêche le maintien de ré-ouvrir une position fermée."""
        for sym in list(self._positions.keys()):
            net = net_by_asset.get(sym, 0.0)
            side = self._positions[sym]["side"]
            if abs(net) < dust or (side == "buy" and net < 0) or (side == "sell" and net > 0):
                self._positions.pop(sym, None)
                self._intent.pop(sym, None)

    # ─── Évaluation ───────────────────────────────────────────────────────────

    def _evaluate(self, sym: str, market: MarketSnapshot, now_ts: float) -> Optional[Signal]:
        candles = market.candles.get(sym)
        if not candles or len(candles) < self._cfg.period + 5:
            return None
        closes = np.array([c.close for c in candles], dtype=float)
        highs = np.array([c.high for c in candles], dtype=float)
        lows = np.array([c.low for c in candles], dtype=float)

        res = supertrend_with_history(highs, lows, closes, period=self._cfg.period, multiplier=self._cfg.multiplier)
        if res is None:
            return None
        st_arr, dir_arr, last_st, last_dir = res

        # On a besoin d'au moins 2 valeurs de direction définies pour détecter un flip
        prev_dir = self._last_direction.get(sym, dir_arr[-2] if len(dir_arr) >= 2 else 0)

        mark = float(closes[-1])
        self._last_metrics[sym] = {
            "st": last_st, "direction": last_dir, "mark": mark, "prev_dir_seen": prev_dir,
        }

        # Mise à jour de la mémoire de direction
        self._last_direction[sym] = last_dir

        # Position ST ouverte → check exit
        if sym in self._positions:
            pos_side = self._positions[sym]["side"]
            pos_dir = 1 if pos_side == "buy" else -1
            if last_dir != pos_dir:
                # Flip contre la position → CLOSE — (b) purge l'intent.
                self._intent.pop(sym, None)
                return Signal(
                    strategy_id=self._strategy_id,
                    asset=sym,
                    direction=0.0,
                    target_notional=0.0,
                    expected_edge_bps=0.0,
                    confidence=1.0,
                    stop_price=None,
                    horizon_bars=1,
                    timestamp=market.timestamp,
                )
            # HOLD (direction conforme) → ré-émettre l'exposition tenue.
            intent = self._intent.get(sym)
            if intent is not None:
                return self._maintain_signal(sym, intent, market)
            return None  # position non tracée → ne pas piloter

        # Pas de position → check entry (flip vs direction précédente connue)
        if prev_dir == 0 or last_dir == prev_dir:
            return None  # pas de flip détectable

        # Flip détecté
        # Cooldown
        last = self._last_signal_ts.get(sym, 0.0)
        if now_ts - last < self._cfg.cooldown_sec:
            return None

        # Stop = niveau supertrend courant. Pour LONG, st_value est en dessous ;
        # pour SHORT, au-dessus. Distance au stop = |mark - last_st|.
        stop_distance = abs(mark - last_st)
        if stop_distance <= 0:
            return None

        # Sizing dynamique ATR-based : qty × stop_distance = risk_per_trade × equity
        equity = float(self._equity_cb()) if self._equity_cb is not None else 1000.0
        if equity <= 0:
            equity = 1000.0
        risk_usd = self._cfg.risk_per_trade_pct * equity
        qty_raw = risk_usd / stop_distance
        notional = qty_raw * mark
        # Clamp dans [min, max]
        notional = max(self._cfg.notional_min_usdc, min(self._cfg.notional_max_usdc, notional))

        direction = 1.0 if last_dir == 1 else -1.0
        # Confidence basée sur la fraîcheur du flip (= 1.0 par défaut, robust signal)
        confidence = 0.8

        self._last_signal_ts[sym] = now_ts
        self._intent[sym] = {
            "direction": direction,
            "target_notional": float(notional),
            "confidence": confidence,
        }

        return Signal(
            strategy_id=self._strategy_id,
            asset=sym,
            direction=direction,
            target_notional=float(notional),
            expected_edge_bps=50.0,  # heuristique post-flip, à ajuster post-backtest
            confidence=confidence,
            stop_price=float(last_st),
            horizon_bars=int(self._cfg.period),  # ordre de grandeur
            timestamp=market.timestamp,
        )

    def _maintain_signal(self, sym: str, intent: Dict, market: MarketSnapshot) -> Signal:
        """Ré-émet l'exposition tenue (HOLD) pour la maintenir dans la cible.

        stop_price=None : le supertrend trailing est recalculé au flip ; le SL
        natif d'entrée n'est pas retouché à chaque tick de maintien.
        """
        return Signal(
            strategy_id=self._strategy_id,
            asset=sym,
            direction=float(intent["direction"]),
            target_notional=float(intent["target_notional"]),
            expected_edge_bps=0.0,
            confidence=float(intent["confidence"]),
            stop_price=None,
            horizon_bars=1,
            timestamp=market.timestamp,
        )

    # ─── Debug / accès ────────────────────────────────────────────────────────

    def get_last_metrics(self) -> dict:
        return dict(self._last_metrics)

    def open_positions(self) -> dict:
        return dict(self._positions)

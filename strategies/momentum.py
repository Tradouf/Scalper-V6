"""
MomentumStrategy — TS momentum déterministe.

Logique :
  - Calcule la z-score de la pente du log-prix sur N bars (lookback_bars).
  - Si |z| ≥ entry_zscore → signal directionnel (LONG si z>0, SHORT sinon).
  - SL ancré au mark à 2×ATR (mark - 2×ATR pour LONG, +2×ATR pour SHORT).
  - Pas de TP natif — sortie via signal CLOSE quand z repasse en deçà de
    entry_zscore × 0.5 (zone neutre).

Filtres :
  - cooldown post-signal (anti-retrigger)
  - ATR cap (rejette les marchés trop volatils)

Le sizing : MR-style → notional fixe × confiance proportionnelle au z.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Dict, Optional

import numpy as np

from core.config import MomentumStrategyConfig
from core.types import Fill, MarketSnapshot, Signal
from regime.features import atr, returns_slope_zscore

logger = logging.getLogger("v7.strategy.momentum")


class MomentumStrategy:
    """StrategyAgent déterministe pour TS momentum.

    Entrée :
      |slope_zscore| ≥ entry_zscore + filtres ATR + pas de position existante.
    Sortie :
      |slope_zscore| < entry_zscore × 0.5 (zone neutre).
    """

    def __init__(
        self,
        cfg: MomentumStrategyConfig,
        symbols: list[str],
        strategy_id: str = "momentum",
        cooldown_sec: int = 1800,
        max_atr_pct: float = 0.05,  # ATR > 5% du prix → skip (trop volatil)
    ) -> None:
        self._cfg = cfg
        self._symbols = list(symbols)
        self._strategy_id = strategy_id
        self._cooldown_sec = cooldown_sec
        self._max_atr_pct = max_atr_pct
        self._last_signal_ts: Dict[str, float] = {}
        # Tracking des positions ouvertes
        self._positions: Dict[str, Dict] = {}  # symbol → {side, qty, entry_px, opened_ts}
        # Intention d'allocation à ré-émettre tant que la position est tenue
        # (V7 level-triggered : un HOLD silencieux = fermeture par l'allocateur).
        self._intent: Dict[str, Dict] = {}  # symbol → {direction, target_notional, confidence}
        # Snapshot dernières métriques pour debug/dashboard
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
            existing_signed = (1 if existing["side"] == "buy" else -1) * existing["qty"] * existing["entry_px"]
            new_signed = existing_signed + fill.notional
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
        if not candles or len(candles) < self._cfg.lookback_bars + 5:
            return None
        closes = np.array([c.close for c in candles], dtype=float)
        highs = np.array([c.high for c in candles], dtype=float)
        lows = np.array([c.low for c in candles], dtype=float)

        # Slope z-score
        slope_z = returns_slope_zscore(closes, window=self._cfg.lookback_bars)
        if slope_z is None:
            return None
        # ATR pour sizing + filtre
        atr_val = atr(highs, lows, closes, period=14)
        if atr_val is None or atr_val <= 0:
            return None
        mark = float(closes[-1])
        atr_pct = atr_val / mark

        self._last_metrics[sym] = {"slope_z": slope_z, "atr": atr_val, "atr_pct": atr_pct, "mark": mark}

        # Filtre ATR cap
        if atr_pct > self._max_atr_pct:
            return None

        # Position MOM ouverte → check exit
        if sym in self._positions:
            exit_threshold = self._cfg.entry_zscore * 0.5
            pos_side = self._positions[sym]["side"]
            # On ferme si le slope ne soutient plus la direction (signe opposé OU magnitude faible)
            if abs(slope_z) < exit_threshold or \
               (pos_side == "buy" and slope_z < 0) or \
               (pos_side == "sell" and slope_z > 0):
                # CLOSE — (b) purge l'intent ; _positions vidé par on_fill.
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
            # HOLD → ré-émettre l'exposition tenue (anti-whipsaw).
            intent = self._intent.get(sym)
            if intent is not None:
                return self._maintain_signal(sym, intent, market)
            return None  # position non tracée → ne pas piloter

        # Pas de position → check entry
        last = self._last_signal_ts.get(sym, 0.0)
        if now_ts - last < self._cooldown_sec:
            return None

        if abs(slope_z) < self._cfg.entry_zscore:
            return None  # signal trop faible

        side = "buy" if slope_z > 0 else "sell"
        direction = 1.0 if side == "buy" else -1.0

        # Sizing : notional fixé. Confidence ∈ [entry_z, 2×entry_z] → [0.5, 1.0]
        excess = abs(slope_z) - self._cfg.entry_zscore
        confidence = min(1.0, 0.5 + excess / max(self._cfg.entry_zscore, 1e-9) * 0.5)
        notional = float(self._cfg.notional_usdc)

        # SL : 2 × ATR en prix
        sl_distance = 2.0 * atr_val
        sl_price = mark - sl_distance if side == "buy" else mark + sl_distance

        self._last_signal_ts[sym] = now_ts
        self._intent[sym] = {
            "direction": direction,
            "target_notional": notional,
            "confidence": confidence,
        }

        return Signal(
            strategy_id=self._strategy_id,
            asset=sym,
            direction=direction,
            target_notional=notional,
            expected_edge_bps=40.0,  # estimation heuristique, à ajuster post P5
            confidence=confidence,
            stop_price=float(sl_price),
            horizon_bars=self._cfg.lookback_bars // 2,  # ordre de grandeur
            timestamp=market.timestamp,
        )

    def _maintain_signal(self, sym: str, intent: Dict, market: MarketSnapshot) -> Signal:
        """Ré-émet l'exposition tenue (HOLD) pour la maintenir dans la cible."""
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

    # ─── Accès pour dashboard / tests ─────────────────────────────────────────

    def get_last_metrics(self) -> dict:
        return dict(self._last_metrics)

    def open_positions(self) -> dict:
        return dict(self._positions)

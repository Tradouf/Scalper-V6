"""
AwesomeOscillatorStrategy — momentum BTC 5m basé sur l'Awesome Oscillator (AO).

Indicateur AO (standard TradingView / Hyperliquid) :
  - median   = (high + low) / 2
  - AO       = SMA(median, fast=5) − SMA(median, slow=34)
  - couleur de barre : VERTE si AO[t] > AO[t-1] (croît), ROUGE si AO[t] < AO[t-1].

Règles d'ENTRÉE (sur la dernière bougie CLÔTURÉE) :
  - LONG  : barre AO rouge ∧ AO < −x_long ∧ bougie de prix verte (close > open).
  - SHORT : barre AO verte ∧ AO > +x_short ∧ bougie de prix rouge (close < open).

Règle de SORTIE :
  - TP SEUL pour l'instant (décision francois 2026-06-17). Pas de SL.
    Le take-profit est géré par la stratégie : à chaque tick, si le mark atteint
    entry × (1 ± tp_pct), elle émet un Signal de fermeture (target_notional=0).

Sizing :
  - Notional fixe par trade (notional_usdc). Pas de sizing au risque (pas de SL).

Intégration V7 (level-triggered) :
  - Comme Supertrend : tant que la position est tenue, on RÉ-ÉMET l'exposition
    (maintain) pour que l'allocateur/reconcile la conserve ; un HOLD silencieux
    serait interprété comme une fermeture.
  - La stratégie est HORS allocateur (cf. main.py) : BTC lui est réservé.

Bar de signal : on évalue la dernière bougie CLÔTURÉE (index -2), la bougie -1
étant celle en cours de formation côté HL (candleSnapshot inclut la bougie vive).
Cela garantit la parité avec le backtest (signaux sur barres fermées). Le TP, lui,
réagit au mark LIVE (pas besoin d'attendre une clôture pour sécuriser un gain).
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Dict, Optional

import numpy as np

from core.config import AwesomeOscillatorStrategyConfig
from core.types import Fill, MarketSnapshot, Signal

logger = logging.getLogger("v7.strategy.ao")


def compute_ao(highs: np.ndarray, lows: np.ndarray, fast: int, slow: int) -> Optional[np.ndarray]:
    """Awesome Oscillator = SMA(median, fast) − SMA(median, slow).

    Retourne un tableau aligné sur l'entrée, avec NaN pour les indices < slow-1.
    None si pas assez de données.
    """
    n = len(highs)
    if n < slow:
        return None
    median = (highs + lows) / 2.0
    # SMA via somme glissante (cumsum) — O(n). cs[k] = Σ median[0:k], longueur n+1.
    cs = np.concatenate(([0.0], np.cumsum(median)))

    def sma(window: int) -> np.ndarray:
        out = np.full(n, np.nan, dtype=float)
        # SMA terminant à l'indice i = (cs[i+1] − cs[i+1-window]) / window, i ≥ window-1.
        out[window - 1:] = (cs[window:] - cs[: n - window + 1]) / window
        return out

    ao = sma(fast) - sma(slow)
    return ao


class AwesomeOscillatorStrategy:
    """StrategyAgent déterministe Awesome Oscillator (BTC 5m, entrées seuillées, TP seul)."""

    def __init__(
        self,
        cfg: AwesomeOscillatorStrategyConfig,
        symbols: Optional[list[str]] = None,
        strategy_id: str = "awesome_oscillator",
    ) -> None:
        self._cfg = cfg
        self._symbols = list(symbols if symbols is not None else cfg.symbols)
        self._strategy_id = strategy_id
        # Position tracée par symbole : {side, qty, entry_px, opened_ts}
        self._positions: Dict[str, Dict] = {}
        # Intention à ré-émettre tant que la position est tenue (level-triggered).
        self._intent: Dict[str, Dict] = {}  # symbol → {direction, target_notional, confidence}
        # Anti-rafale : ts_open de la dernière bougie ayant produit une entrée.
        self._last_entry_bar_ts: Dict[str, float] = {}
        # Snapshot debug.
        self._last_metrics: Dict[str, Dict] = {}

    @property
    def strategy_id(self) -> str:
        return self._strategy_id

    @property
    def symbols(self) -> list[str]:
        return list(self._symbols)

    def generate_signals(self, market: MarketSnapshot) -> list[Signal]:
        """`market` DOIT contenir des bougies au timeframe AO (5m) et le mark live."""
        signals: list[Signal] = []
        for sym in self._symbols:
            sig = self._evaluate(sym, market)
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
        """Purge les positions fermées hors stratégie (emergency, liquidation).
        Empêche le maintien de ré-ouvrir une position déjà fermée."""
        for sym in list(self._positions.keys()):
            net = net_by_asset.get(sym, 0.0)
            side = self._positions[sym]["side"]
            if abs(net) < dust or (side == "buy" and net < 0) or (side == "sell" and net > 0):
                self._positions.pop(sym, None)
                self._intent.pop(sym, None)

    # ─── Évaluation ───────────────────────────────────────────────────────────

    def _evaluate(self, sym: str, market: MarketSnapshot) -> Optional[Signal]:
        candles = market.candles.get(sym)
        # slow + 3 : AO défini à partir de slow-1, +1 pour la couleur (AO[-1] vs AO[-2]),
        # +1 pour ignorer la bougie en formation (on évalue l'index -2).
        if not candles or len(candles) < self._cfg.slow + 3:
            return None

        mark = float(market.prices.get(sym) or candles[-1].close)

        # Position tenue → check TP, sinon maintain.
        if sym in self._positions:
            return self._manage_open(sym, market, mark)

        # Pas de position → check entry sur la dernière bougie CLÔTURÉE (index -2).
        highs = np.array([c.high for c in candles], dtype=float)
        lows = np.array([c.low for c in candles], dtype=float)
        ao = compute_ao(highs, lows, self._cfg.fast, self._cfg.slow)
        if ao is None:
            return None

        j = len(candles) - 2  # dernière bougie clôturée
        ao_now, ao_prev = ao[j], ao[j - 1]
        if not (np.isfinite(ao_now) and np.isfinite(ao_prev)):
            return None

        bar = candles[j]
        bar_ts = bar.ts_open.timestamp() if isinstance(bar.ts_open, dt.datetime) else float(bar.ts_open)
        candle_green = bar.close > bar.open
        candle_red = bar.close < bar.open
        ao_red = ao_now < ao_prev   # AO décroît
        ao_green = ao_now > ao_prev  # AO croît

        self._last_metrics[sym] = {
            "ao": float(ao_now), "ao_prev": float(ao_prev),
            "bar_color": "green" if ao_green else "red",
            "x_long": self._cfg.x_long, "x_short": self._cfg.x_short, "mark": mark,
        }

        long_signal = ao_red and (ao_now < -self._cfg.x_long) and candle_green
        short_signal = ao_green and (ao_now > self._cfg.x_short) and candle_red
        if not (long_signal or short_signal):
            return None

        # Anti-rafale : une seule entrée par bougie clôturée.
        if self._last_entry_bar_ts.get(sym) == bar_ts:
            return None
        self._last_entry_bar_ts[sym] = bar_ts

        direction = 1.0 if long_signal else -1.0
        notional = float(self._cfg.notional_usdc)
        confidence = 0.8
        self._intent[sym] = {
            "direction": direction,
            "target_notional": notional,
            "confidence": confidence,
        }
        logger.info(
            "AO %s ENTRY %s : AO=%.2f (prev %.2f, %s) seuil=%.0f mark=%.4f notional=$%.0f",
            sym, "LONG" if direction > 0 else "SHORT", ao_now, ao_prev,
            "rouge" if ao_red else "verte",
            self._cfg.x_long if direction > 0 else self._cfg.x_short, mark, notional,
        )
        return Signal(
            strategy_id=self._strategy_id,
            asset=sym,
            direction=direction,
            target_notional=notional,
            expected_edge_bps=float(self._cfg.tp_pct * 10000.0),  # cible TP en bps
            confidence=confidence,
            stop_price=None,  # TP seul, pas de SL natif
            horizon_bars=12,
            timestamp=market.timestamp,
        )

    def _manage_open(self, sym: str, market: MarketSnapshot, mark: float) -> Optional[Signal]:
        pos = self._positions[sym]
        side = pos["side"]
        entry = float(pos["entry_px"])
        tp = self._cfg.tp_pct
        sl = self._cfg.sl_pct
        hit_tp = (side == "buy" and mark >= entry * (1.0 + tp)) or (
            side == "sell" and mark <= entry * (1.0 - tp)
        )
        # SL géré au mark (sl_pct=0 → désactivé). Le backtest a montré qu'un TP seul
        # verrouille le book sur une position à contre-tendance → SL nécessaire.
        hit_sl = sl > 0 and (
            (side == "buy" and mark <= entry * (1.0 - sl))
            or (side == "sell" and mark >= entry * (1.0 + sl))
        )
        if hit_tp or hit_sl:
            self._intent.pop(sym, None)
            reason = "TP" if hit_tp else "SL"
            pct = tp if hit_tp else -sl
            logger.info(
                "AO %s %s atteint : side=%s entry=%.4f mark=%.4f (%+.2f%%) → close",
                sym, reason, side, entry, mark, pct * 100.0,
            )
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
        # HOLD → ré-émet l'exposition tenue (maintain).
        intent = self._intent.get(sym)
        if intent is None:
            return None  # position non tracée par un intent → ne pas piloter
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

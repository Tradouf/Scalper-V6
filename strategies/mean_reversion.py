"""
MeanReversionStrategy — wrap V7 de l'agent MR V6.

Différences V6 → V7 :
  - Implémente StrategyAgent (Protocol) au lieu de gérer ses ouvertures/fermetures
    elle-même via le main loop.
  - generate_signals() émet des Signal LONG/SHORT/CLOSE. C'est l'Execution
    Engine (P6) qui exécutera (via market order avec strategy_id="mean_reversion").
  - Le calcul z-score / half-life / sizing reste identique.

La position MR est trackée en interne (entry, sl, side, qty) pour gérer la
sortie (CLOSE quand revert) — mais l'exécution physique passe par l'engine.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Dict, List, Optional

from core.config import MeanReversionStrategyConfig
from core.types import Fill, MarketSnapshot, Signal
from utils.stats import half_life, rolling_mean_std, zscore

logger = logging.getLogger("v7.strategy.mr")


class MeanReversionStrategy:
    """StrategyAgent déterministe pour mean reversion.

    Génère un Signal :
      - LONG  : si pas de position MR ET z < -entry_z ET filtres passent
      - SHORT : si pas de position MR ET z > +entry_z ET filtres passent
      - CLOSE (target_notional=0, direction=0) : si position MR ET |z| < exit_z

    Le sizing est porté par target_notional = MR_NOTIONAL × size_factor (où
    size_factor dépend de la half-life).

    Le SL n'est PAS émis par cette stratégie ; il est calculé et joint au
    Signal via stop_price (mark - buffer*std pour BUY, +buffer*std pour SELL).
    L'Execution Engine pose le SL natif côté exchange à partir de stop_price.
    """

    def __init__(
        self,
        cfg: MeanReversionStrategyConfig,
        symbols: list[str],
        strategy_id: str = "mean_reversion",
    ) -> None:
        self._cfg = cfg
        self._symbols = list(symbols)
        self._strategy_id = strategy_id
        # Cooldown par symbole (anti-retrigger après signal ou close)
        self._last_signal_ts: Dict[str, float] = {}
        self._last_close_ts: Dict[str, float] = {}
        # Tracking interne des positions MR ouvertes (sera mis à jour via on_fill)
        # key: symbol → {side, entry_px, qty, sl_price, opened_ts}
        self._positions: Dict[str, Dict] = {}
        # Intention d'allocation à ré-émettre tant que la position est tenue
        # (V7 est level-triggered : l'allocateur ferme tout actif absent de la
        # cible. Une position « HOLD » doit donc ré-exprimer son exposition à
        # chaque tick, sinon elle est fermée 1 tick après son ouverture).
        # key: symbol → {direction, target_notional, confidence}
        self._intent: Dict[str, Dict] = {}
        # Snapshot dernières métriques (pour dashboard / debug)
        self._last_metrics: Dict[str, Dict] = {}

    # ─── StrategyAgent contract ───────────────────────────────────────────────

    @property
    def strategy_id(self) -> str:
        return self._strategy_id

    def generate_signals(self, market: MarketSnapshot) -> list[Signal]:
        signals: list[Signal] = []
        now = market.timestamp.timestamp() if isinstance(market.timestamp, dt.datetime) else time.time()
        for sym in self._symbols:
            sig = self._evaluate_symbol(sym, market, now)
            if sig is not None:
                signals.append(sig)
        return signals

    def on_fill(self, fill: Fill) -> None:
        """Met à jour le tracking interne des positions MR.

        Hypothèse : l'Execution Engine attribue le fill avec strategy_id correct.
        Un fill avec notional > 0 ouvre/agrandit ; notional < 0 ferme/réduit.
        """
        sym = fill.asset
        if sym not in self._symbols:
            return
        existing = self._positions.get(sym)
        if existing is None:
            # Nouvelle position
            self._positions[sym] = {
                "side": "buy" if fill.notional > 0 else "sell",
                "entry_px": fill.price,
                "qty": abs(fill.notional / fill.price),
                "opened_ts": fill.timestamp.timestamp() if isinstance(fill.timestamp, dt.datetime) else time.time(),
            }
        else:
            # Si direction opposée → close ; sinon agrandit (rare en MR)
            existing_signed_notional = (1 if existing["side"] == "buy" else -1) * existing["qty"] * existing["entry_px"]
            new_notional = existing_signed_notional + fill.notional
            if abs(new_notional) < 0.01:
                # Fermée
                self._positions.pop(sym, None)
                self._intent.pop(sym, None)
                self._last_close_ts[sym] = fill.timestamp.timestamp() if isinstance(fill.timestamp, dt.datetime) else time.time()
            else:
                existing["qty"] = abs(new_notional / fill.price)
                existing["side"] = "buy" if new_notional > 0 else "sell"

    def sync_positions(self, net_by_asset: Dict[str, float], dust: float = 1.0) -> None:
        """Purge les positions tracées qui n'existent plus côté exchange (fermées
        par EmergencyExit, SL natif, liquidation) ou dont le sens a changé.

        SÉCURITÉ : sans cette synchro, le maintien (ré-émission HOLD) ré-ouvrirait
        une position fermée hors stratégie → boucle de réouverture. `net_by_asset`
        = {asset → notional signé} réel (HL en live, portfolio en paper)."""
        for sym in list(self._positions.keys()):
            net = net_by_asset.get(sym, 0.0)
            side = self._positions[sym]["side"]
            if abs(net) < dust or (side == "buy" and net < 0) or (side == "sell" and net > 0):
                self._positions.pop(sym, None)
                self._intent.pop(sym, None)

    # ─── Évaluation par symbole ───────────────────────────────────────────────

    def _evaluate_symbol(self, sym: str, market: MarketSnapshot, now_ts: float) -> Optional[Signal]:
        candles = market.candles.get(sym)
        if not candles or len(candles) < self._cfg.window + 5:
            return None
        closes = [c.close for c in candles]

        # Indicateurs
        z = zscore(closes, self._cfg.window)
        hl = half_life(closes)
        mu, std = rolling_mean_std(closes, self._cfg.window)
        if z is None or hl is None or std is None:
            return None
        self._last_metrics[sym] = {"z": z, "hl": hl, "mean": mu, "std": std}

        # Filtre half-life
        if hl <= 0 or hl < self._cfg.hl_min or hl > self._cfg.hl_max:
            return None

        # Position MR ouverte → check exit (CLOSE) ou maintien
        if sym in self._positions:
            if abs(z) < self._cfg.exit_z:
                # CLOSE signal : direction 0, target_notional 0. (b) On purge
                # l'intent dès la décision de fermeture pour cesser de ré-émettre
                # le maintien ; _positions sera vidé par on_fill sur le fill réel
                # (attribué grâce au correctif (a) côté allocateur).
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
            # HOLD : pas encore de revert → ré-émettre l'exposition tenue pour
            # que l'allocateur garde la position dans la cible (anti-whipsaw).
            intent = self._intent.get(sym)
            if intent is not None:
                return self._maintain_signal(sym, intent, market)
            return None  # position non tracée (pré-existante) → ne pas piloter

        # Pas de position → check entry
        # Cooldown
        last = max(self._last_signal_ts.get(sym, 0.0), self._last_close_ts.get(sym, 0.0))
        if now_ts - last < self._cfg.cooldown_sec:
            return None

        if abs(z) < self._cfg.entry_z:
            return None  # HOLD : z dans la bande

        # Signal LONG ou SHORT
        side = "buy" if z < 0 else "sell"
        direction = 1.0 if side == "buy" else -1.0

        # Sizing : facteur dépendant de la half-life
        size_factor = self._size_factor(hl)
        notional = float(self._cfg.notional_usdc) * size_factor

        # Stop loss : ancré au mark avec buffer std
        mark = float(candles[-1].close)
        z_entry = abs(z)
        sl_buffer = max(self._cfg.sl_z - z_entry, self._cfg.min_sl_buffer_std) * std
        sl_price = mark - sl_buffer if side == "buy" else mark + sl_buffer

        self._last_signal_ts[sym] = now_ts
        conf = min(1.0, abs(z) / (self._cfg.entry_z * 2.0))
        # Mémorise l'intention pour la ré-émettre à l'identique pendant le HOLD
        # (mêmes direction/notional/confidence → l'allocateur reproduit la même
        # cible → reconcile sous le seuil → position conservée, pas de churn).
        self._intent[sym] = {
            "direction": direction,
            "target_notional": notional,
            "confidence": conf,
        }

        return Signal(
            strategy_id=self._strategy_id,
            asset=sym,
            direction=direction,
            target_notional=notional,
            expected_edge_bps=50.0,  # estimation heuristique, à ajuster post P5
            confidence=conf,
            stop_price=float(sl_price),
            horizon_bars=int(hl),
            timestamp=market.timestamp,
        )

    def _maintain_signal(self, sym: str, intent: Dict, market: MarketSnapshot) -> Signal:
        """Ré-émet l'exposition tenue (HOLD) pour la maintenir dans la cible.

        stop_price=None : le SL natif est posé une fois à l'entrée ; on ne le
        retouche pas à chaque tick de maintien.
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

    def _size_factor(self, hl: float) -> float:
        """Facteur ∈ [0.3, 1.0] décroissant avec la half-life.
        hl=hl_min → 1.0  /  hl=hl_max → 0.3."""
        if hl <= self._cfg.hl_min:
            return 1.0
        if hl >= self._cfg.hl_max:
            return 0.3
        raw = (self._cfg.hl_max - hl) / (self._cfg.hl_max - self._cfg.hl_min)
        return max(0.3, min(1.0, raw))

    # ─── Accès pour dashboard / tests ─────────────────────────────────────────

    def get_last_metrics(self) -> dict:
        return dict(self._last_metrics)

    def open_positions(self) -> dict:
        return dict(self._positions)

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

    def engaged_symbols(self) -> set:
        """Symboles où MR est engagée : position tenue OU intention active.
        Utilisé par la préemption range (2026-06-07) — la grille ne doit ni
        rester ni se réactiver sur ces symboles tant que MR les occupe."""
        return set(self._positions) | set(self._intent)

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
                # SL natif (2026-06-17) : niveau calculé à l'entrée, mémorisé dans
                # l'intent → repris ici pour que NativeStopManager pose le stop.
                "sl_price": (self._intent.get(sym) or {}).get("sl_price"),
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

    def adopt_position(
        self,
        sym: str,
        side: str,
        entry_px: float,
        qty: float,
        opened_ts: Optional[float] = None,
        confidence: float = 0.6,
    ) -> bool:
        """Adopte une position pré-existante (orpheline après restart) pour la
        RENDRE GÉRÉE par MR : maintien (HOLD ré-émis à chaque tick), sortie au
        retour à la moyenne (|z| < exit_z) et SL natif posé au 1er maintien.

        Sans adoption, une position absente de `_positions` est ignorée par
        `_evaluate_symbol` (« non tracée → ne pas piloter ») et n'est plus coupée
        qu'au stop d'urgence -5 % (toujours en perte). C'est la cause racine du
        bleed orphelines V7 (BootReconciler n'hydratait que le portfolio, pas les
        stratégies — 2026-06-17). Retourne True si adoptée.
        """
        side = (side or "").lower()
        if sym not in self._symbols or side not in ("buy", "sell"):
            return False
        entry_px = float(entry_px)
        qty = abs(float(qty))
        notional = qty * entry_px
        if notional <= 0:
            return False
        self._positions[sym] = {
            "side": side,
            "entry_px": entry_px,
            "qty": qty,
            "opened_ts": float(opened_ts) if opened_ts is not None else time.time(),
            "adopted": True,
            "sl_pending": True,  # SL natif posé au 1er maintien (_maintain_signal)
        }
        self._intent[sym] = {
            "direction": 1.0 if side == "buy" else -1.0,
            "target_notional": notional,
            "confidence": float(confidence),
        }
        return True

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
            "sl_price": float(sl_price),  # repris par on_fill → desired_stops()
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

        stop_price=None en régime normal : le SL natif est posé une fois à
        l'entrée, on ne le retouche pas à chaque tick. EXCEPTION : une position
        ADOPTÉE au boot (sl_pending) n'a jamais eu d'entrée dans cette session →
        on pose son SL natif au 1er maintien, ancré sur le prix d'entrée réel
        avec le buffer std standard, puis on retombe sur None (2026-06-17).
        """
        stop_price = None
        pos = self._positions.get(sym)
        if pos is not None and pos.get("sl_pending"):
            m = self._last_metrics.get(sym, {})
            std = m.get("std")
            entry = pos.get("entry_px")
            if std and std > 0 and entry:
                buf = max(self._cfg.sl_z - self._cfg.entry_z, self._cfg.min_sl_buffer_std) * std
                stop_price = entry - buf if pos.get("side") == "buy" else entry + buf
                pos["sl_pending"] = False  # posé une seule fois
                pos["sl_price"] = float(stop_price)  # → desired_stops() (SL natif)
        return Signal(
            strategy_id=self._strategy_id,
            asset=sym,
            direction=float(intent["direction"]),
            target_notional=float(intent["target_notional"]),
            expected_edge_bps=0.0,
            confidence=float(intent["confidence"]),
            stop_price=float(stop_price) if stop_price is not None else None,
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

    def desired_stops(self) -> Dict[str, Dict]:
        """SL souhaités pour les positions MR tenues, consommé par
        NativeStopManager (2026-06-17). {sym → {stop_px, side, qty}} où `side`
        est le sens de la POSITION (l'ordre de stop sera dans le sens opposé,
        reduce_only). Une position sans sl_price (adoptée pas encore maintenue,
        ou std indispo) est absente → pas de stop ce tick."""
        out: Dict[str, Dict] = {}
        for sym, p in self._positions.items():
            sl = p.get("sl_price")
            if sl and sl > 0 and p.get("qty", 0) > 0:
                out[sym] = {"stop_px": float(sl), "side": p["side"], "qty": float(p["qty"])}
        return out

    # ─── Accès pour dashboard / tests ─────────────────────────────────────────

    def get_last_metrics(self) -> dict:
        return dict(self._last_metrics)

    def open_positions(self) -> dict:
        return dict(self._positions)

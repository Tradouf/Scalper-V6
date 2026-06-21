"""
TsmomStrategy — Time-Series Momentum (trend following) sur timeframe LONG (1d).

Recherche 2026-06-20 (STRATEGY_HYPOTHESES.md #10, mémoire project_tsmom_winner) : 1er
mécanisme à VRAIE espérance positive nette de frais (walk-forward poolé t=+2,48, 27× le
frais). Le scalping mourait des frais ; l'HORIZON 1d les rend négligeables. Vol-targeted,
le Sharpe ~0,85 n'écrase pas le buy&hold en bull, mais le drawdown est 4-5× plus petit.

Mécanique (parité STRICTE avec backtest.backtester._signals_tsmom + run_tsmom_portfolio) :
  - Signal  : état persistant = signe du rendement trailing sur `lookback` barres CLÔTURÉES.
              `band` = zone morte → flat si |rendement| ≤ band.
  - Sizing  : vol-targeting equal-risk. Chaque coin pèse 1/N du capital, leveré par
              scalar = clip(target_vol_bar / vol_réalisée, 0, scalar_cap).
              notional_i = equity × (1/N) × scalar_i, signé par la direction.
  - Sortie  : retournement du signe (stop-and-reverse). PAS de TP/SL (le backtest a réfuté
              la barrière ; l'exit est le flip de tendance).
  - Garde-fou DUR : Σ|notional| ≤ max_gross_frac × equity (rescale homogène si dépassé).

Intégration V7 (level-triggered, comme l'AO/Supertrend) : on RÉ-ÉMET la cible désirée à
chaque tick. Entre deux barres journalières la cible ne bouge pas → la bande de non-trade de
l'ExecutionEngine évite tout churn. Un coin que l'on veut FLAT reçoit un Signal direction=0,
target=0 (ferme proprement + attribue le fill de close). HORS allocateur : ses symboles sont
réservés (cf. main.py).

Bar de signal : dernière bougie CLÔTURÉE = index -2 (la -1 se forme encore côté HL), comme
l'AwesomeOscillator → parité avec le backtest (signaux sur barres fermées).
"""
from __future__ import annotations

import logging
import math
from typing import Callable, Dict, Optional

import numpy as np

from core.config import TsmomStrategyConfig
from core.types import Fill, MarketSnapshot, Signal

logger = logging.getLogger("v7.strategy.tsmom")

# Barres par an selon le timeframe (annualisation de la vol cible).
_BARS_PER_YEAR = {"1d": 365.0, "12h": 2 * 365.0, "4h": 6 * 365.0, "1h": 24 * 365.0}


class TsmomStrategy:
    """StrategyAgent déterministe time-series momentum vol-targeted (trend following 1d)."""

    def __init__(
        self,
        cfg: TsmomStrategyConfig,
        symbols: Optional[list[str]] = None,
        equity_callback: Optional[Callable[[], float]] = None,
        strategy_id: str = "tsmom",
    ) -> None:
        self._cfg = cfg
        self._symbols = list(symbols if symbols is not None else cfg.symbols)
        self._equity_cb = equity_callback or (lambda: 0.0)
        self._strategy_id = strategy_id
        self._bars_per_year = _BARS_PER_YEAR.get(cfg.interval, 365.0)
        # Horizons de l'ensemble (recherche 2026-06-20) : si `lookbacks` est fourni on
        # combine le signe sur plusieurs horizons (direction CONTINUE = conviction) ; sinon
        # on retombe sur le single `lookback` (rétro-compat). On déduplique/trie pour un
        # `need` (historique requis) déterministe.
        self._lookbacks = sorted(set(int(x) for x in cfg.lookbacks)) or [int(cfg.lookback)]
        # Dernières cibles désirées (signées) par symbole — debug / dashboard.
        self._desired: Dict[str, float] = {}
        self._last_metrics: Dict[str, Dict] = {}

    @property
    def strategy_id(self) -> str:
        return self._strategy_id

    @property
    def symbols(self) -> list[str]:
        return list(self._symbols)

    # ─── Génération ─────────────────────────────────────────────────────────────

    def generate_signals(self, market: MarketSnapshot) -> list[Signal]:
        """`market` DOIT contenir des bougies au timeframe de la stratégie (1d)."""
        equity = float(self._equity_cb() or 0.0)
        n = len(self._symbols)
        if equity <= 0.0 or n == 0:
            return []

        target_vol_bar = float(self._cfg.target_vol_annual) / math.sqrt(self._bars_per_year)
        weight = 1.0 / n
        # 1er passage : direction + notional brut (avant garde-fou de gross).
        raw: Dict[str, tuple[float, float]] = {}  # sym → (direction, |notional|)
        for sym in self._symbols:
            d, notion = self._eval_symbol(sym, market, equity, weight, target_vol_bar)
            if d is None:
                continue  # données insuffisantes → on n'émet rien (reste/devient flat)
            raw[sym] = (d, notion)

        # 2e passage : garde-fou DUR sur l'exposition brute totale.
        gross = sum(notion for _, notion in raw.values())
        cap = float(self._cfg.max_gross_frac) * equity
        scale = (cap / gross) if gross > cap and gross > 0 else 1.0

        signals: list[Signal] = []
        for sym, (d, notion) in raw.items():
            notion *= scale
            # Sous le minimum HL → flat (sinon ordre rejeté < $10).
            if d == 0.0 or notion < float(self._cfg.min_notional_usdc):
                d, notion = 0.0, 0.0
            self._desired[sym] = d * notion
            signals.append(Signal(
                strategy_id=self._strategy_id,
                asset=sym,
                direction=float(d),
                target_notional=float(notion),
                expected_edge_bps=0.0,         # edge diffus (horizon long), pas un TP ciblé
                confidence=0.6,
                stop_price=None,               # pas de SL natif (exit = flip de tendance)
                horizon_bars=int(self._cfg.lookback),
                timestamp=market.timestamp,
            ))
        if scale < 1.0:
            logger.info("TSMOM garde-fou gross : %.0f$ > cap %.0f$ → rescale ×%.2f",
                        gross, cap, scale)
        return signals

    def _eval_symbol(self, sym, market, equity, weight, target_vol_bar):
        """Retourne (direction∈{-1,0,+1}, |notional|) ou (None, 0) si données insuffisantes.

        Ensemble multi-lookback : la CONVICTION = moyenne des signes du rendement trailing sur
        chaque horizon ∈ [-1,+1]. `direction` = signe de la conviction (pour l'attribution du
        fill, dans {-1,0,+1}), |conviction| repliée dans le notional (parité run_tsmom_ensemble :
        pos = pos_dir_continu × scalar). Single-lookback (un seul horizon) = cas particulier."""
        candles = market.candles.get(sym)
        need = max(self._lookbacks) + int(self._cfg.vol_win) + 3
        if not candles or len(candles) < need:
            return None, 0.0
        # Closes jusqu'à la dernière bougie CLÔTURÉE (-2 ; -1 se forme).
        closes = np.array([c.close for c in candles], dtype=float)[:-1]
        band = float(self._cfg.band)
        # État TSMOM ensemble : moyenne des signes sur les horizons (conviction continue).
        signs = []
        for lb in self._lookbacks:
            tr = closes[-1] / closes[-1 - lb] - 1.0
            signs.append(1.0 if tr > band else (-1.0 if tr < -band else 0.0))
        conviction = float(np.mean(signs))          # ∈ [-1, +1]
        direction = 1.0 if conviction > 0 else (-1.0 if conviction < 0 else 0.0)
        trail_ret = closes[-1] / closes[-1 - self._lookbacks[-1]] - 1.0  # plus long horizon (debug)
        # Vol réalisée : écart-type des rendements quotidiens sur vol_win (barres fermées).
        rets = np.diff(closes) / closes[:-1]
        realized = float(np.std(rets[-int(self._cfg.vol_win):], ddof=1))
        if not np.isfinite(realized) or realized <= 0.0:
            return None, 0.0
        scalar = min(target_vol_bar / realized, float(self._cfg.scalar_cap))
        # Conviction repliée dans la taille : un coin long sur 3/4 horizons pèse 3/4 de la taille.
        notional = equity * weight * scalar * abs(conviction)
        self._last_metrics[sym] = {
            "trail_ret": float(trail_ret), "direction": float(direction),
            "conviction": conviction, "realized_vol_bar": realized, "scalar": float(scalar),
            "notional": float(direction * notional),
        }
        return direction, float(notional)

    # ─── Fills / état ───────────────────────────────────────────────────────────

    def on_fill(self, fill: Fill) -> None:
        """Pilotage par CIBLE (la cible est ré-émise chaque tick) → pas d'état de position
        à maintenir ici. No-op hormis le log d'attribution."""
        if fill.asset in self._symbols:
            logger.debug("TSMOM fill %s notional=%.2f px=%.4f", fill.asset, fill.notional, fill.price)

    def sync_positions(self, net_by_asset: Dict[str, float], dust: float = 1.0) -> None:
        """Compat avec le routage AO/Supertrend (purge externe). Pilotage par cible →
        rien à purger, mais on garde la signature pour un appel homogène depuis main."""
        return None

    # ─── Debug / dashboard ────────────────────────────────────────────────────────

    def get_last_metrics(self) -> dict:
        return dict(self._last_metrics)

    def desired_positions(self) -> dict:
        return dict(self._desired)

"""
RotationStrategy — méta-allocateur d'ensemble de stratégies à tilt CONTRARIAN, vol-targeted (1d).

Recherche 2026-06-21 (cf. mémoire project_strategy_rotation) : la performance des stratégies est
ANTI-persistante (ρ≤0) → chasser le gagnant récent perd ; le geste rentable = tenir un ensemble
profond de stratégies décorrélées et rééquilibrer DOUCEMENT vers les PERDANTS récents (tilt
contrarian), empilé sur un panier de coins. Validé walk-forward (12 coins × 4 folds, t-stat 4-5,
robuste aux params) PUIS dans le cadre VOL-TARGETED du live : Sharpe portefeuille ~1,5 / maxDD ~4%
vs mono-TSMOM 0,64 / 14% (≈2× le Sharpe, ⅓ du drawdown, même CAGR).

Mécanique (parité STRICTE avec backtest.run_rotation_voltarget à R=1) :
  - Pour chaque coin, sur les bougies CLÔTURÉES :
      1. construit le pool profond (strategies.strategy_pool.build_pool_deep) → 43 positions ∈[-1,1] ;
      2. score chaque stratégie = Sharpe de son rendement net sur les `L` dernières barres ;
      3. poids CONTRARIAN = softmax(−z(score)/temp) (vers les perdants récents) ;
      4. position méta = Σ poids·position(dernière barre) ∈ [-1,1] (direction × conviction) ;
  - Vol-targeting À LA TSMOM : notional = |méta| × equity × (1/N) × clip(target_vol/vol_réalisée, cap),
    direction = signe(méta). Garde-fou de gross dur + min notional HL. Level-triggered (ré-émet la
    cible chaque tick ; la bande non-trade de l'ExecutionEngine évite le churn entre deux bougies).
  - SANS ÉTAT : tout se recalcule depuis les bougies (R=1, reweight quotidien) → pas de bug orphelines.

Intégration V7 (hors allocateur, comme l'AO/TSMOM) : symboles RÉSERVÉS, cible injectée via
_merge_reserved_target, exemptée de l'emergency exit ROE (tient des semaines).
"""
from __future__ import annotations

import logging
import math
from typing import Callable, Dict, Optional

import numpy as np
import pandas as pd

from core.config import RotationStrategyConfig
from core.types import Fill, MarketSnapshot, Signal
from strategies.strategy_pool import build_pool, build_pool_deep, build_pool_xdeep, strat_returns

logger = logging.getLogger("v7.strategy.rotation")

_POOL_BUILDERS = {"base": build_pool, "deep": build_pool_deep, "xdeep": build_pool_xdeep}

_BARS_PER_YEAR = {"1d": 365.0, "12h": 2 * 365.0, "4h": 6 * 365.0, "1h": 24 * 365.0}


class RotationStrategy:
    """Méta-allocateur ensemble-contrarian vol-targeted (trend/MR rotation 1d)."""

    def __init__(
        self,
        cfg: RotationStrategyConfig,
        symbols: Optional[list[str]] = None,
        equity_callback: Optional[Callable[[], float]] = None,
        strategy_id: str = "rotation",
    ) -> None:
        self._cfg = cfg
        self._symbols = list(symbols if symbols is not None else cfg.symbols)
        self._equity_cb = equity_callback or (lambda: 0.0)
        self._strategy_id = strategy_id
        self._bars_per_year = _BARS_PER_YEAR.get(cfg.interval, 365.0)
        self._build_pool = _POOL_BUILDERS.get(cfg.pool, build_pool_deep)
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
        equity = float(self._equity_cb() or 0.0)
        n = len(self._symbols)
        if equity <= 0.0 or n == 0:
            return []

        target_vol_bar = float(self._cfg.target_vol_annual) / math.sqrt(self._bars_per_year)
        weight = 1.0 / n
        raw: Dict[str, tuple[float, float]] = {}
        for sym in self._symbols:
            d, notion = self._eval_symbol(sym, market, equity, weight, target_vol_bar)
            if d is None:
                continue
            raw[sym] = (d, notion)

        gross = sum(notion for _, notion in raw.values())
        cap = float(self._cfg.max_gross_frac) * equity
        scale = (cap / gross) if gross > cap and gross > 0 else 1.0

        signals: list[Signal] = []
        for sym, (d, notion) in raw.items():
            notion *= scale
            if d == 0.0 or notion < float(self._cfg.min_notional_usdc):
                d, notion = 0.0, 0.0
            self._desired[sym] = d * notion
            signals.append(Signal(
                strategy_id=self._strategy_id,
                asset=sym,
                direction=float(d),
                target_notional=float(notion),
                expected_edge_bps=0.0,
                confidence=0.6,
                stop_price=None,
                horizon_bars=int(self._cfg.L),
                timestamp=market.timestamp,
            ))
        if scale < 1.0:
            logger.info("Rotation garde-fou gross : %.0f$ > cap %.0f$ → rescale ×%.2f", gross, cap, scale)
        return signals

    # ─── Cœur : position méta contrarian d'un coin ──────────────────────────────

    def _meta_position(self, df: pd.DataFrame) -> Optional[float]:
        """Position méta ∈[-1,1] (direction × conviction) à la DERNIÈRE barre de `df` (déjà
        clôturée). Parité run_rotation_voltarget (R=1). None si données insuffisantes."""
        pool = self._build_pool(df)
        if not pool:
            return None
        L = int(self._cfg.L)
        names = list(pool)
        n = len(df)
        scores = np.empty(len(names))
        last_pos = np.empty(len(names))
        for i, nm in enumerate(names):
            r = strat_returns(df, pool[nm])
            # Parité weighted_ensemble à R=1 : score = Sharpe de rets[t-L:t] avec t = dernière barre
            # (n-1) → exclut le rendement de la barre courante (causal : poids connus avant la barre).
            seg = r[n - 1 - L:n - 1]
            sd = np.std(seg)
            scores[i] = (np.mean(seg) / sd) if sd > 0 else 0.0
            last_pos[i] = float(pool[nm].iloc[-1])
        # Tilt CONTRARIAN : softmax(−z(score)/temp) → vers les perdants récents.
        sd = scores.std() or 1e-9
        z = -(scores - scores.mean()) / sd / float(self._cfg.temp)
        w = np.exp(z - z.max())
        w /= w.sum()
        return float(np.dot(w, last_pos))

    def _eval_symbol(self, sym, market, equity, weight, target_vol_bar):
        candles = market.candles.get(sym)
        # Besoin : plus long lookback du pool (200 = ma_50_200) + L (scoring) + marge.
        need = 200 + int(self._cfg.L) + int(self._cfg.vol_win) + 5
        if not candles or len(candles) < need:
            return None, 0.0
        # Bougies CLÔTURÉES (la dernière -1 se forme encore) → DataFrame OHLCV.
        cc = candles[:-1]
        df = pd.DataFrame({
            "open": [c.open for c in cc], "high": [c.high for c in cc],
            "low": [c.low for c in cc], "close": [c.close for c in cc],
            "volume": [c.volume for c in cc],
        })
        meta = self._meta_position(df)
        if meta is None or not np.isfinite(meta):
            return None, 0.0
        closes = df["close"].to_numpy(dtype=float)
        rets = np.diff(closes) / closes[:-1]
        realized = float(np.std(rets[-int(self._cfg.vol_win):], ddof=1))
        if not np.isfinite(realized) or realized <= 0.0:
            return None, 0.0
        # Overlay régime high-vol (opt-in) : le contrarian n'a pas d'edge dans le tiers HAUT de
        # vol → flat. Percentile causal = rang de la vol réalisée courante dans `vol_regime_window`.
        cut = float(self._cfg.vol_regime_cut_pct)
        vol_pct = 0.0
        if cut < 1.0:
            vw = int(self._cfg.vol_win)
            rv = pd.Series(rets).rolling(vw).std().dropna().to_numpy()
            win = rv[-int(self._cfg.vol_regime_window):]
            if len(win) >= 30:
                vol_pct = float((win <= win[-1]).mean())
                if vol_pct > cut:
                    meta = 0.0
        direction = 1.0 if meta > 0 else (-1.0 if meta < 0 else 0.0)
        scalar = min(target_vol_bar / realized, float(self._cfg.scalar_cap))
        notional = equity * weight * scalar * abs(meta)   # conviction repliée dans la taille
        self._last_metrics[sym] = {
            "meta": float(meta), "direction": float(direction),
            "realized_vol_bar": realized, "scalar": float(scalar),
            "vol_pct": vol_pct, "notional": float(direction * notional),
        }
        return direction, float(notional)

    # ─── Fills / état ───────────────────────────────────────────────────────────

    def on_fill(self, fill: Fill) -> None:
        if fill.asset in self._symbols:
            logger.debug("Rotation fill %s notional=%.2f px=%.4f", fill.asset, fill.notional, fill.price)

    def sync_positions(self, net_by_asset: Dict[str, float], dust: float = 1.0) -> None:
        return None

    # ─── Debug / dashboard ────────────────────────────────────────────────────────

    def get_last_metrics(self) -> dict:
        return dict(self._last_metrics)

    def desired_positions(self) -> dict:
        return dict(self._desired)

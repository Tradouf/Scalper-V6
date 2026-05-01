"""
SalleDesMarches V5 — agents/feature_engine.py

V5.9.0-dev
- moteur déterministe de features avancées pré-LLM
- calcule des features robustes par symbole à chaque cycle
- s'appuie d'abord sur les données déjà disponibles (technical + orderbook)
- maintient un historique local léger pour calculs de pente, persistance, z-scores
- ne dépend pas d'un format unique de technical: lecture tolérante des clés
- dégradation douce: retourne toujours un dict exploitable

Features principales
- slope_multi_horizon
- volatility_compression / expansion
- distance_to_vwap
- micro_trend
- orderbook_imbalance enrichi
- quality / readiness scores utilisables par technical/scalper/consensus

Notes de dev V5.9.0-dev
- priorité au déterministe robuste live, pas au sophisticationnisme fragile
- pas d'appel LLM ici
- pas de dépendance obligatoire à pandas/numpy
- historique volontairement court et borné pour éviter la dérive mémoire
"""

from __future__ import annotations

import logging
import math
import statistics
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("sdm.feature_engine")


class FeatureEngine:
    def __init__(self, memory=None, exchange=None, history_limit: int = 240) -> None:
        self.memory = memory
        self.exchange = exchange
        self.history_limit = max(60, int(history_limit))

        self._price_hist: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self.history_limit)
        )
        self._atr_hist: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self.history_limit)
        )
        self._bbw_hist: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self.history_limit)
        )
        self._vwap_dist_hist: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self.history_limit)
        )
        self._spread_hist: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self.history_limit)
        )
        self._imb_hist: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self.history_limit)
        )
        self._regime_hist: Dict[str, Deque[str]] = defaultdict(
            lambda: deque(maxlen=self.history_limit)
        )
        self._tick_ts: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self.history_limit)
        )

    # ─────────────────────────────────────────────────────
    # API publique
    # ─────────────────────────────────────────────────────

    def compute(
        self,
        symbol: str,
        technical: Dict,
        orderbook: Optional[Dict] = None,
        regime: Optional[Dict] = None,
        previous: Optional[Dict] = None,
    ) -> Dict:
        technical = technical or {}
        orderbook = orderbook or {}
        regime = regime or {}
        previous = previous or {}

        indicators = self._extract_indicators(technical)

        price = self._safe_float(
            indicators.get("price"),
            technical.get("price"),
            technical.get("last_price"),
            technical.get("close"),
        )
        atr = self._safe_float(
            indicators.get("atr"),
            indicators.get("atr14"),
            indicators.get("atr_14"),
        )
        vwap = self._safe_float(
            indicators.get("vwap"),
            indicators.get("session_vwap"),
            indicators.get("anchored_vwap"),
        )
        volume_ratio = self._safe_float(
            indicators.get("vol_ratio"),
            indicators.get("volratio"),
            indicators.get("volume_ratio"),
            default=1.0,
        )
        bb_upper = self._safe_float(indicators.get("bb_upper"), indicators.get("boll_upper"))
        bb_lower = self._safe_float(indicators.get("bb_lower"), indicators.get("boll_lower"))
        bb_mid = self._safe_float(indicators.get("bb_mid"), indicators.get("boll_mid"), vwap)
        rsi = self._safe_float(indicators.get("rsi"), default=50.0)

        ob_imb = self._safe_float(
            orderbook.get("bidaskimbalance"),
            orderbook.get("bid_ask_imbalance"),
            orderbook.get("imbalance"),
        )
        spread_pct = self._safe_float(orderbook.get("spreadpct"), default=0.0)
        bid_depth = self._safe_float(orderbook.get("biddepthusdt"), default=0.0)
        ask_depth = self._safe_float(orderbook.get("askdepthusdt"), default=0.0)
        best_bid = self._safe_float(orderbook.get("bestbid"))
        best_ask = self._safe_float(orderbook.get("bestask"))
        recommended_entry = self._safe_float(orderbook.get("recommendedentryprice"), price)

        now = time.time()

        if price > 0:
            self._price_hist[symbol].append(price)
            self._tick_ts[symbol].append(now)
        if atr > 0:
            self._atr_hist[symbol].append(atr)
        if spread_pct >= 0:
            self._spread_hist[symbol].append(spread_pct)
        if ob_imb is not None:
            self._imb_hist[symbol].append(ob_imb)

        bbw = self._compute_bollinger_width(price, bb_upper, bb_lower, bb_mid)
        if bbw is not None:
            self._bbw_hist[symbol].append(bbw)

        vwap_dist_pct = self._compute_distance_pct(price, vwap)
        if vwap_dist_pct is not None:
            self._vwap_dist_hist[symbol].append(vwap_dist_pct)

        inferred_regime = str(regime.get("trend", previous.get("trend_regime", "range")) or "range")
        self._regime_hist[symbol].append(inferred_regime)

        slope_pack = self._compute_multi_horizon_slopes(
            prices=list(self._price_hist[symbol]),
            atr=atr,
        )

        vol_pack = self._compute_volatility_pack(
            atr=atr,
            price=price,
            bbw=bbw,
            atr_hist=list(self._atr_hist[symbol]),
            bbw_hist=list(self._bbw_hist[symbol]),
        )

        vwap_pack = self._compute_vwap_pack(
            price=price,
            vwap=vwap,
            dist_hist=list(self._vwap_dist_hist[symbol]),
        )

        micro_pack = self._compute_micro_trend_pack(
            prices=list(self._price_hist[symbol]),
            atr=atr,
            rsi=rsi,
        )

        ob_pack = self._compute_orderbook_pack(
            imbalance=ob_imb,
            spread_pct=spread_pct,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            best_bid=best_bid,
            best_ask=best_ask,
            recommended_entry=recommended_entry,
            price=price,
            imb_hist=list(self._imb_hist[symbol]),
            spread_hist=list(self._spread_hist[symbol]),
        )

        readiness = self._compute_trade_readiness(
            slope_score=slope_pack["slope_alignment_score"],
            vol_state=vol_pack["volatility_state"],
            micro_score=micro_pack["micro_trend_score"],
            liquidity_score=ob_pack["liquidity_score"],
            imbalance_score=ob_pack["imbalance_score"],
            volume_ratio=volume_ratio,
            vwap_reversion_score=vwap_pack["vwap_reversion_score"],
        )

        result = {
            "symbol": symbol,
            "ts": int(now),

            "price": price,
            "atr": atr,
            "vwap": vwap,
            "volume_ratio": volume_ratio,

            "slope_5": slope_pack["slope_5"],
            "slope_10": slope_pack["slope_10"],
            "slope_20": slope_pack["slope_20"],
            "slope_40": slope_pack["slope_40"],
            "slope_5_atr_norm": slope_pack["slope_5_atr_norm"],
            "slope_10_atr_norm": slope_pack["slope_10_atr_norm"],
            "slope_20_atr_norm": slope_pack["slope_20_atr_norm"],
            "slope_40_atr_norm": slope_pack["slope_40_atr_norm"],
            "slope_alignment_score": slope_pack["slope_alignment_score"],
            "slope_multi_horizon": slope_pack["label"],

            "atr_pct": vol_pack["atr_pct"],
            "bollinger_width_pct": vol_pack["bollinger_width_pct"],
            "atr_regime_zscore": vol_pack["atr_regime_zscore"],
            "bbw_regime_zscore": vol_pack["bbw_regime_zscore"],
            "volatility_compression": vol_pack["volatility_compression"],
            "volatility_expansion": vol_pack["volatility_expansion"],
            "volatility_state": vol_pack["volatility_state"],
            "volatility_state_score": vol_pack["volatility_state_score"],

            "distance_to_vwap": vwap_pack["distance_to_vwap"],
            "distance_to_vwap_abs": abs(vwap_pack["distance_to_vwap"]),
            "distance_to_vwap_zscore": vwap_pack["distance_to_vwap_zscore"],
            "vwap_location": vwap_pack["vwap_location"],
            "vwap_reversion_score": vwap_pack["vwap_reversion_score"],

            "micro_trend": micro_pack["micro_trend"],
            "micro_trend_score": micro_pack["micro_trend_score"],
            "micro_higher_highs": micro_pack["higher_highs"],
            "micro_higher_lows": micro_pack["higher_lows"],
            "micro_lower_highs": micro_pack["lower_highs"],
            "micro_lower_lows": micro_pack["lower_lows"],
            "micro_impulse_ratio": micro_pack["impulse_ratio"],
            "micro_pullback_ratio": micro_pack["pullback_ratio"],

            "orderbook_imbalance": ob_pack["orderbook_imbalance"],
            "orderbook_imbalance_zscore": ob_pack["orderbook_imbalance_zscore"],
            "imbalance_score": ob_pack["imbalance_score"],
            "spread_pct": ob_pack["spread_pct"],
            "spread_regime_zscore": ob_pack["spread_regime_zscore"],
            "spread_state": ob_pack["spread_state"],
            "liquidity_score": ob_pack["liquidity_score"],
            "depth_asymmetry": ob_pack["depth_asymmetry"],
            "depth_total_usdt": ob_pack["depth_total_usdt"],
            "entry_edge_bps": ob_pack["entry_edge_bps"],
            "orderbook_pressure": ob_pack["orderbook_pressure"],

            "trend_quality_score": readiness["trend_quality_score"],
            "mean_reversion_score": readiness["mean_reversion_score"],
            "breakout_readiness_score": readiness["breakout_readiness_score"],
            "scalp_readiness_score": readiness["scalp_readiness_score"],
            "feature_quality_score": readiness["feature_quality_score"],
            "feature_regime_hint": readiness["feature_regime_hint"],
        }

        logger.info(
            "FEATURE_ENGINE %s | slope=%s vol=%s micro=%s ob=%.3f read=%.2f",
            symbol,
            result["slope_multi_horizon"],
            result["volatility_state"],
            result["micro_trend"],
            result["orderbook_imbalance"],
            result["scalp_readiness_score"],
        )

        return result

    # ─────────────────────────────────────────────────────
    # Extraction / helpers
    # ─────────────────────────────────────────────────────

    @staticmethod
    def _extract_indicators(technical: Dict) -> Dict:
        if not isinstance(technical, dict):
            return {}
        indicators = technical.get("indicators", {})
        return indicators if isinstance(indicators, dict) else {}

    @staticmethod
    def _safe_float(*values, default: float = 0.0) -> float:
        for v in values:
            if v is None:
                continue
            try:
                return float(v)
            except Exception:
                continue
        return float(default)

    @staticmethod
    def _clip(x: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, float(x)))

    @staticmethod
    def _sign_label(x: float, pos_th: float = 0.15, neg_th: float = -0.15) -> str:
        if x >= pos_th:
            return "positive"
        if x <= neg_th:
            return "negative"
        return "neutral"

    @staticmethod
    def _mean(values: Iterable[float], default: float = 0.0) -> float:
        vals = [float(v) for v in values if v is not None and math.isfinite(float(v))]
        return sum(vals) / len(vals) if vals else default

    @staticmethod
    def _std(values: Iterable[float], default: float = 0.0) -> float:
        vals = [float(v) for v in values if v is not None and math.isfinite(float(v))]
        if len(vals) < 2:
            return default
        try:
            return statistics.pstdev(vals)
        except Exception:
            return default

    def _zscore(self, value: float, hist: List[float]) -> float:
        if value is None or not math.isfinite(value):
            return 0.0
        if len(hist) < 8:
            return 0.0
        mu = self._mean(hist, default=value)
        sd = self._std(hist, default=0.0)
        if sd <= 1e-12:
            return 0.0
        return (value - mu) / sd

    @staticmethod
    def _pct_change(a: float, b: float) -> float:
        if b == 0:
            return 0.0
        return (a - b) / abs(b)

    @staticmethod
    def _compute_distance_pct(price: float, ref: float) -> Optional[float]:
        if price <= 0 or ref <= 0:
            return None
        return (price - ref) / ref

    @staticmethod
    def _compute_bollinger_width(
        price: float,
        bb_upper: float,
        bb_lower: float,
        bb_mid: float,
    ) -> Optional[float]:
        base = bb_mid if bb_mid and bb_mid > 0 else price
        if base <= 0 or bb_upper <= 0 or bb_lower <= 0 or bb_upper < bb_lower:
            return None
        return (bb_upper - bb_lower) / base

    # ─────────────────────────────────────────────────────
    # Pentes
    # ─────────────────────────────────────────────────────

    def _linear_slope(self, values: List[float], window: int) -> float:
        if len(values) < window or window < 2:
            return 0.0
        y = values[-window:]
        x = list(range(window))
        x_mean = sum(x) / window
        y_mean = sum(y) / window
        num = sum((xi - x_mean) * (yi - y_mean) for xi, yi in zip(x, y))
        den = sum((xi - x_mean) ** 2 for xi in x)
        if den == 0:
            return 0.0
        return num / den

    def _compute_multi_horizon_slopes(self, prices: List[float], atr: float) -> Dict:
        s5 = self._linear_slope(prices, 5)
        s10 = self._linear_slope(prices, 10)
        s20 = self._linear_slope(prices, 20)
        s40 = self._linear_slope(prices, 40)

        norm = atr if atr and atr > 1e-12 else max(prices[-1] * 0.002, 1e-12) if prices else 1.0

        n5 = s5 / norm
        n10 = s10 / norm
        n20 = s20 / norm
        n40 = s40 / norm

        score = (
            self._clip(n5, -2.0, 2.0) * 0.35
            + self._clip(n10, -2.0, 2.0) * 0.30
            + self._clip(n20, -2.0, 2.0) * 0.20
            + self._clip(n40, -2.0, 2.0) * 0.15
        )

        aligned_up = n5 > 0 and n10 > 0 and n20 > 0
        aligned_down = n5 < 0 and n10 < 0 and n20 < 0

        if aligned_up and score > 0.25:
            label = "bull_aligned"
        elif aligned_down and score < -0.25:
            label = "bear_aligned"
        elif abs(n5) > abs(n20) * 1.25 and self._sign_label(n5) != self._sign_label(n20):
            label = "short_term_divergence"
        elif abs(score) < 0.10:
            label = "flat"
        else:
            label = "mixed"

        return {
            "slope_5": round(s5, 8),
            "slope_10": round(s10, 8),
            "slope_20": round(s20, 8),
            "slope_40": round(s40, 8),
            "slope_5_atr_norm": round(n5, 6),
            "slope_10_atr_norm": round(n10, 6),
            "slope_20_atr_norm": round(n20, 6),
            "slope_40_atr_norm": round(n40, 6),
            "slope_alignment_score": round(self._clip(score, -2.0, 2.0), 6),
            "label": label,
        }

    # ─────────────────────────────────────────────────────
    # Volatilité
    # ─────────────────────────────────────────────────────

    def _compute_volatility_pack(
        self,
        atr: float,
        price: float,
        bbw: Optional[float],
        atr_hist: List[float],
        bbw_hist: List[float],
    ) -> Dict:
        atr_pct = (atr / price) if price > 0 and atr > 0 else 0.0
        bbw_pct = bbw if bbw is not None else 0.0

        atr_z = self._zscore(atr_pct, [x / price for x in atr_hist if price > 0 and x > 0]) if price > 0 else 0.0
        bbw_z = self._zscore(bbw_pct, bbw_hist) if bbw is not None else 0.0

        compression = 0.0
        expansion = 0.0

        if atr_z < -0.4:
            compression += min(1.0, abs(atr_z) / 2.5)
        if bbw is not None and bbw_z < -0.4:
            compression += min(1.0, abs(bbw_z) / 2.5)
        compression = self._clip(compression / 2.0, 0.0, 1.0)

        if atr_z > 0.4:
            expansion += min(1.0, atr_z / 2.5)
        if bbw is not None and bbw_z > 0.4:
            expansion += min(1.0, bbw_z / 2.5)
        expansion = self._clip(expansion / 2.0, 0.0, 1.0)

        if compression >= 0.60:
            state = "compressed"
            score = -compression
        elif expansion >= 0.60:
            state = "expanding"
            score = expansion
        elif atr_pct > 0 and atr_pct < 0.0035:
            state = "quiet"
            score = -0.15
        elif atr_pct > 0.012:
            state = "high_vol"
            score = 0.45
        else:
            state = "normal"
            score = 0.0

        return {
            "atr_pct": round(atr_pct, 6),
            "bollinger_width_pct": round(bbw_pct, 6),
            "atr_regime_zscore": round(atr_z, 6),
            "bbw_regime_zscore": round(bbw_z, 6),
            "volatility_compression": round(compression, 6),
            "volatility_expansion": round(expansion, 6),
            "volatility_state": state,
            "volatility_state_score": round(score, 6),
        }

    # ─────────────────────────────────────────────────────
    # VWAP
    # ─────────────────────────────────────────────────────

    def _compute_vwap_pack(
        self,
        price: float,
        vwap: float,
        dist_hist: List[float],
    ) -> Dict:
        dist = self._compute_distance_pct(price, vwap)
        if dist is None:
            dist = 0.0
        z = self._zscore(dist, dist_hist)

        if vwap <= 0 or price <= 0:
            loc = "unknown"
        elif dist > 0.003:
            loc = "above_vwap"
        elif dist < -0.003:
            loc = "below_vwap"
        else:
            loc = "near_vwap"

        # plus on est extrême, plus le score mean reversion augmente
        reversion = self._clip(abs(z) / 3.0, 0.0, 1.0)

        return {
            "distance_to_vwap": round(dist, 6),
            "distance_to_vwap_zscore": round(z, 6),
            "vwap_location": loc,
            "vwap_reversion_score": round(reversion, 6),
        }

    # ─────────────────────────────────────────────────────
    # Micro-trend
    # ─────────────────────────────────────────────────────

    def _compute_micro_trend_pack(self, prices: List[float], atr: float, rsi: float) -> Dict:
        if len(prices) < 6:
            return {
                "micro_trend": "unknown",
                "micro_trend_score": 0.0,
                "higher_highs": 0,
                "higher_lows": 0,
                "lower_highs": 0,
                "lower_lows": 0,
                "impulse_ratio": 0.0,
                "pullback_ratio": 0.0,
            }

        recent = prices[-6:]
        deltas = [recent[i] - recent[i - 1] for i in range(1, len(recent))]
        ups = [d for d in deltas if d > 0]
        downs = [abs(d) for d in deltas if d < 0]

        higher_highs = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i - 1])
        lower_lows = sum(1 for i in range(1, len(recent)) if recent[i] < recent[i - 1])

        # approximation simple du "structurel"
        higher_lows = sum(
            1 for i in range(2, len(recent))
            if min(recent[i - 1], recent[i]) > min(recent[i - 2], recent[i - 1])
        )
        lower_highs = sum(
            1 for i in range(2, len(recent))
            if max(recent[i - 1], recent[i]) < max(recent[i - 2], recent[i - 1])
        )

        impulse = sum(ups)
        pullback = sum(downs)
        total = impulse + pullback
        impulse_ratio = impulse / total if total > 0 else 0.0
        pullback_ratio = pullback / total if total > 0 else 0.0

        atr_norm = atr if atr and atr > 1e-12 else max(recent[-1] * 0.002, 1e-12)
        net_move_score = (recent[-1] - recent[0]) / atr_norm

        score = (
            ((higher_highs - lower_lows) / 5.0) * 0.45
            + ((higher_lows - lower_highs) / 4.0) * 0.25
            + self._clip(net_move_score / 3.0, -1.0, 1.0) * 0.20
            + self._clip((rsi - 50.0) / 30.0, -1.0, 1.0) * 0.10
        )

        if score > 0.35:
            label = "bull_microtrend"
        elif score < -0.35:
            label = "bear_microtrend"
        elif abs(score) <= 0.12:
            label = "chop"
        else:
            label = "transition"

        return {
            "micro_trend": label,
            "micro_trend_score": round(self._clip(score, -1.5, 1.5), 6),
            "higher_highs": higher_highs,
            "higher_lows": higher_lows,
            "lower_highs": lower_highs,
            "lower_lows": lower_lows,
            "impulse_ratio": round(impulse_ratio, 6),
            "pullback_ratio": round(pullback_ratio, 6),
        }

    # ─────────────────────────────────────────────────────
    # Orderbook enrichi
    # ─────────────────────────────────────────────────────

    def _compute_orderbook_pack(
        self,
        imbalance: float,
        spread_pct: float,
        bid_depth: float,
        ask_depth: float,
        best_bid: float,
        best_ask: float,
        recommended_entry: float,
        price: float,
        imb_hist: List[float],
        spread_hist: List[float],
    ) -> Dict:
        total_depth = max(0.0, bid_depth) + max(0.0, ask_depth)
        asym = ((bid_depth - ask_depth) / total_depth) if total_depth > 0 else 0.0
        imb = imbalance if imbalance is not None else asym

        imb_z = self._zscore(imb, imb_hist)
        spread_z = self._zscore(spread_pct, spread_hist)

        if spread_pct <= 0.03:
            spread_state = "tight"
        elif spread_pct <= 0.12:
            spread_state = "normal"
        else:
            spread_state = "wide"

        liquidity_score = 0.0
        if total_depth > 0:
            liquidity_score += min(1.0, total_depth / 20000.0)
        if spread_state == "tight":
            liquidity_score += 0.35
        elif spread_state == "normal":
            liquidity_score += 0.15
        else:
            liquidity_score -= 0.20
        liquidity_score = self._clip(liquidity_score, 0.0, 1.0)

        imbalance_score = self._clip(imb, -1.0, 1.0)

        edge_bps = 0.0
        if price > 0 and recommended_entry > 0:
            edge_bps = ((price - recommended_entry) / price) * 10000.0

        if imb > 0.18 and spread_state != "wide":
            pressure = "bid_pressure"
        elif imb < -0.18 and spread_state != "wide":
            pressure = "ask_pressure"
        elif spread_state == "wide":
            pressure = "spread_dominant"
        else:
            pressure = "balanced"

        return {
            "orderbook_imbalance": round(imb, 6),
            "orderbook_imbalance_zscore": round(imb_z, 6),
            "imbalance_score": round(imbalance_score, 6),
            "spread_pct": round(spread_pct, 6),
            "spread_regime_zscore": round(spread_z, 6),
            "spread_state": spread_state,
            "liquidity_score": round(liquidity_score, 6),
            "depth_asymmetry": round(asym, 6),
            "depth_total_usdt": round(total_depth, 2),
            "entry_edge_bps": round(edge_bps, 4),
            "orderbook_pressure": pressure,
        }

    # ─────────────────────────────────────────────────────
    # Scores composites
    # ─────────────────────────────────────────────────────

    def _compute_trade_readiness(
        self,
        slope_score: float,
        vol_state: str,
        micro_score: float,
        liquidity_score: float,
        imbalance_score: float,
        volume_ratio: float,
        vwap_reversion_score: float,
    ) -> Dict:
        vol_bonus_breakout = {
            "compressed": 0.45,
            "normal": 0.10,
            "quiet": 0.05,
            "expanding": 0.35,
            "high_vol": 0.15,
        }.get(vol_state, 0.0)

        trend_quality = self._clip(
            (abs(slope_score) * 0.55)
            + (abs(micro_score) * 0.30)
            + (max(0.0, volume_ratio - 1.0) * 0.25),
            0.0,
            1.5,
        )

        mean_reversion = self._clip(
            vwap_reversion_score * 0.65 + (1.0 - min(1.0, abs(slope_score))) * 0.25,
            0.0,
            1.0,
        )

        breakout = self._clip(
            vol_bonus_breakout * 0.40
            + liquidity_score * 0.25
            + min(1.0, abs(imbalance_score)) * 0.15
            + min(1.0, max(0.0, volume_ratio - 0.8)) * 0.20,
            0.0,
            1.0,
        )

        scalp = self._clip(
            trend_quality * 0.30
            + breakout * 0.30
            + liquidity_score * 0.25
            + min(1.0, max(0.0, volume_ratio)) * 0.15,
            0.0,
            1.0,
        )

        feature_quality = self._clip(
            liquidity_score * 0.25
            + min(1.0, abs(slope_score)) * 0.20
            + min(1.0, abs(micro_score)) * 0.20
            + min(1.0, vwap_reversion_score) * 0.10
            + min(1.0, max(0.0, volume_ratio / 2.0)) * 0.25,
            0.0,
            1.0,
        )

        if breakout >= 0.62 and abs(slope_score) > 0.20:
            hint = "breakout_bias"
        elif mean_reversion >= 0.58 and abs(slope_score) < 0.30:
            hint = "mean_reversion_bias"
        elif slope_score > 0.30 and micro_score > 0.20:
            hint = "trend_follow_long_bias"
        elif slope_score < -0.30 and micro_score < -0.20:
            hint = "trend_follow_short_bias"
        else:
            hint = "mixed_bias"

        return {
            "trend_quality_score": round(trend_quality, 6),
            "mean_reversion_score": round(mean_reversion, 6),
            "breakout_readiness_score": round(breakout, 6),
            "scalp_readiness_score": round(scalp, 6),
            "feature_quality_score": round(feature_quality, 6),
            "feature_regime_hint": hint,
        }

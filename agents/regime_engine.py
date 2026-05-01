"""
SalleDesMarches V5 — agents/regime_engine.py

V5.9.0-dev
- moteur déterministe de régime marché
- consomme les advanced features calculées en amont
- fournit:
  * trend regime: bull / bear / range
  * volatility regime: low / medium / high
  * persistence / age / stability
  * Markov transition estimates
  * latent state smoothing (pseudo-HMM robuste live)
  * risk / directive exploitables par orchestrator / scalper / consensus

Philosophie
- live first: robustesse > sophistication fragile
- pas de dépendance ML lourde
- pas de vrai HMM probabiliste entraîné offline ici
- pseudo-HMM = fusion d'observations + inertie + transitions + hysteresis
"""

from __future__ import annotations

import logging
import math
import statistics
import time
from collections import defaultdict, deque
from typing import Deque, Dict, List, Optional, Tuple

logger = logging.getLogger("sdm.regime_engine")


class RegimeEngine:
    def __init__(self, memory=None, history_limit: int = 300) -> None:
        self.memory = memory
        self.history_limit = max(80, int(history_limit))

        self._trend_obs_hist: Dict[str, Deque[str]] = defaultdict(
            lambda: deque(maxlen=self.history_limit)
        )
        self._vol_obs_hist: Dict[str, Deque[str]] = defaultdict(
            lambda: deque(maxlen=self.history_limit)
        )
        self._trend_state_hist: Dict[str, Deque[str]] = defaultdict(
            lambda: deque(maxlen=self.history_limit)
        )
        self._vol_state_hist: Dict[str, Deque[str]] = defaultdict(
            lambda: deque(maxlen=self.history_limit)
        )
        self._trend_score_hist: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self.history_limit)
        )
        self._vol_score_hist: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self.history_limit)
        )
        self._state_ts_hist: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self.history_limit)
        )

        self._current_state: Dict[str, Dict] = {}

    # ─────────────────────────────────────────────────────
    # API publique
    # ─────────────────────────────────────────────────────

    def assess(
        self,
        symbol: str,
        features: Dict,
        previous_regime: Optional[Dict] = None,
    ) -> Dict:
        previous_regime = previous_regime or self._load_previous_regime(symbol) or {}
        now = time.time()

        obs = self._compute_observations(symbol=symbol, features=features)
        prev_trend = str(previous_regime.get("trend", "range") or "range")
        prev_vol = str(previous_regime.get("volatility", "medium") or "medium")

        trend_state, trend_meta = self._apply_trend_hysteresis(
            symbol=symbol,
            previous_state=prev_trend,
            obs_score=obs["trend_score"],
            obs_label=obs["trend_observation"],
        )
        vol_state, vol_meta = self._apply_vol_hysteresis(
            symbol=symbol,
            previous_state=prev_vol,
            obs_score=obs["vol_score"],
            obs_label=obs["vol_observation"],
        )

        self._trend_obs_hist[symbol].append(obs["trend_observation"])
        self._vol_obs_hist[symbol].append(obs["vol_observation"])
        self._trend_state_hist[symbol].append(trend_state)
        self._vol_state_hist[symbol].append(vol_state)
        self._trend_score_hist[symbol].append(obs["trend_score"])
        self._vol_score_hist[symbol].append(obs["vol_score"])
        self._state_ts_hist[symbol].append(now)

        trend_age = self._compute_state_age(list(self._trend_state_hist[symbol]))
        vol_age = self._compute_state_age(list(self._vol_state_hist[symbol]))

        trend_stability = self._compute_stability(list(self._trend_state_hist[symbol]), trend_state)
        vol_stability = self._compute_stability(list(self._vol_state_hist[symbol]), vol_state)

        trend_markov = self._markov_transition_stats(
            states=list(self._trend_state_hist[symbol]),
            current_state=trend_state,
        )
        vol_markov = self._markov_transition_stats(
            states=list(self._vol_state_hist[symbol]),
            current_state=vol_state,
        )

        latent = self._compute_latent_state(
            trend_state=trend_state,
            trend_obs_score=obs["trend_score"],
            trend_stability=trend_stability,
            trend_markov_stay=trend_markov["stay_probability"],
            vol_state=vol_state,
            vol_obs_score=obs["vol_score"],
            vol_stability=vol_stability,
            vol_markov_stay=vol_markov["stay_probability"],
            previous_regime=previous_regime,
        )

        persistence = self._compute_persistence_pack(
            trend_age=trend_age,
            vol_age=vol_age,
            trend_stability=trend_stability,
            vol_stability=vol_stability,
            trend_stay_prob=trend_markov["stay_probability"],
            vol_stay_prob=vol_markov["stay_probability"],
        )

        risk = self._compute_risk_and_directive(
            trend_state=trend_state,
            vol_state=vol_state,
            latent_state=latent["latent_market_state"],
            latent_confidence=latent["latent_confidence"],
            features=features,
        )

        result = {
            "symbol": symbol,
            "ts": int(now),

            "trend": trend_state,
            "volatility": vol_state,
            "risk": risk["risk"],
            "directive": risk["directive"],

            "trend_observation": obs["trend_observation"],
            "trend_observation_score": round(obs["trend_score"], 6),
            "trend_flip_blocked": trend_meta["flip_blocked"],
            "trend_change_confirmed": trend_meta["change_confirmed"],

            "volatility_observation": obs["vol_observation"],
            "volatility_observation_score": round(obs["vol_score"], 6),
            "vol_flip_blocked": vol_meta["flip_blocked"],
            "vol_change_confirmed": vol_meta["change_confirmed"],

            "trend_persistence_bars": trend_age,
            "volatility_persistence_bars": vol_age,
            "trend_stability": round(trend_stability, 6),
            "volatility_stability": round(vol_stability, 6),
            "regime_persistence_score": round(persistence["regime_persistence_score"], 6),
            "regime_age_score": round(persistence["regime_age_score"], 6),
            "recent_change_flag": persistence["recent_change_flag"],

            "trend_markov_stay_probability": round(trend_markov["stay_probability"], 6),
            "trend_markov_switch_probability": round(trend_markov["switch_probability"], 6),
            "trend_markov_next_bull": round(trend_markov["next_probs"].get("bull", 0.0), 6),
            "trend_markov_next_bear": round(trend_markov["next_probs"].get("bear", 0.0), 6),
            "trend_markov_next_range": round(trend_markov["next_probs"].get("range", 0.0), 6),

            "vol_markov_stay_probability": round(vol_markov["stay_probability"], 6),
            "vol_markov_switch_probability": round(vol_markov["switch_probability"], 6),
            "vol_markov_next_low": round(vol_markov["next_probs"].get("low", 0.0), 6),
            "vol_markov_next_medium": round(vol_markov["next_probs"].get("medium", 0.0), 6),
            "vol_markov_next_high": round(vol_markov["next_probs"].get("high", 0.0), 6),

            "latent_trend_state": latent["latent_trend_state"],
            "latent_volatility_state": latent["latent_volatility_state"],
            "latent_market_state": latent["latent_market_state"],
            "latent_confidence": round(latent["latent_confidence"], 6),
            "hmm_like_transition_risk": round(latent["transition_risk"], 6),

            "regime_confidence": round(
                self._clip(
                    (
                        abs(obs["trend_score"]) * 0.30
                        + trend_stability * 0.20
                        + trend_markov["stay_probability"] * 0.15
                        + abs(obs["vol_score"]) * 0.10
                        + vol_stability * 0.10
                        + latent["latent_confidence"] * 0.15
                    ),
                    0.0,
                    1.0,
                ),
                6,
            ),
        }

        self._current_state[symbol] = result

        logger.info(
            "REGIME_ENGINE %s | trend=%s vol=%s latent=%s conf=%.2f risk=%s dir=%s",
            symbol,
            result["trend"],
            result["volatility"],
            result["latent_market_state"],
            result["regime_confidence"],
            result["risk"],
            result["directive"],
        )

        return result

    # ─────────────────────────────────────────────────────
    # Récup previous régime
    # ─────────────────────────────────────────────────────

    def _load_previous_regime(self, symbol: str) -> Dict:
        if symbol in self._current_state:
            return dict(self._current_state[symbol])

        if self.memory is None:
            return {}

        try:
            if hasattr(self.memory, "get_regime_for_symbol"):
                data = self.memory.get_regime_for_symbol(symbol)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass

        try:
            if hasattr(self.memory, "getregime"):
                data = self.memory.getregime()
                if isinstance(data, dict):
                    return data
        except Exception:
            pass

        return {}

    # ─────────────────────────────────────────────────────
    # Observations
    # ─────────────────────────────────────────────────────

    def _compute_observations(self, symbol: str, features: Dict) -> Dict:
        slope_score = self._safe_float(features.get("slope_alignment_score"))
        micro_score = self._safe_float(features.get("micro_trend_score"))
        vwap_dist_z = self._safe_float(features.get("distance_to_vwap_zscore"))
        vwap_dist = self._safe_float(features.get("distance_to_vwap"))
        imbalance_score = self._safe_float(features.get("imbalance_score"))
        breakout_score = self._safe_float(features.get("breakout_readiness_score"))
        trend_quality = self._safe_float(features.get("trend_quality_score"))
        feature_hint = str(features.get("feature_regime_hint", "mixed_bias") or "mixed_bias")

        vol_state_score = self._safe_float(features.get("volatility_state_score"))
        compression = self._safe_float(features.get("volatility_compression"))
        expansion = self._safe_float(features.get("volatility_expansion"))
        atr_pct = self._safe_float(features.get("atr_pct"))
        bbw_z = self._safe_float(features.get("bbw_regime_zscore"))
        atr_z = self._safe_float(features.get("atr_regime_zscore"))

        trend_score = (
            slope_score * 0.34
            + micro_score * 0.26
            + imbalance_score * 0.12
            + self._clip(vwap_dist_z / 3.0, -1.0, 1.0) * 0.08
            + breakout_score * 0.10 * (1 if slope_score >= 0 else -1)
            + trend_quality * 0.10 * (1 if slope_score >= 0 else -1)
        )

        if feature_hint == "trend_follow_long_bias":
            trend_score += 0.10
        elif feature_hint == "trend_follow_short_bias":
            trend_score -= 0.10
        elif feature_hint == "mean_reversion_bias":
            trend_score *= 0.80

        if trend_score > 0.28:
            trend_obs = "bull"
        elif trend_score < -0.28:
            trend_obs = "bear"
        else:
            trend_obs = "range"

        vol_score = (
            vol_state_score * 0.40
            + expansion * 0.30
            - compression * 0.30
            + self._clip(atr_z / 3.0, -1.0, 1.0) * 0.15
            + self._clip(bbw_z / 3.0, -1.0, 1.0) * 0.15
        )

        if vol_score > 0.42 or atr_pct > 0.012:
            vol_obs = "high"
        elif vol_score < -0.20 or compression > 0.55:
            vol_obs = "low"
        else:
            vol_obs = "medium"

        return {
            "trend_score": self._clip(trend_score, -2.0, 2.0),
            "trend_observation": trend_obs,
            "vol_score": self._clip(vol_score, -2.0, 2.0),
            "vol_observation": vol_obs,
        }

    # ─────────────────────────────────────────────────────
    # Hysteresis
    # ─────────────────────────────────────────────────────

    def _apply_trend_hysteresis(
        self,
        symbol: str,
        previous_state: str,
        obs_score: float,
        obs_label: str,
    ) -> Tuple[str, Dict]:
        previous_state = previous_state if previous_state in {"bull", "bear", "range"} else "range"

        # seuils différenciés pour éviter les flip-flops
        enter_bull = 0.38
        enter_bear = -0.38
        exit_to_range_from_bull = 0.10
        exit_to_range_from_bear = -0.10
        direct_flip_buffer = 0.62

        new_state = previous_state
        change_confirmed = False
        flip_blocked = False

        if previous_state == "bull":
            if obs_score < -direct_flip_buffer and obs_label == "bear":
                # flip direct très exigeant
                new_state = "bear"
                change_confirmed = True
            elif obs_score < exit_to_range_from_bull:
                new_state = "range"
                change_confirmed = True

        elif previous_state == "bear":
            if obs_score > direct_flip_buffer and obs_label == "bull":
                new_state = "bull"
                change_confirmed = True
            elif obs_score > exit_to_range_from_bear:
                new_state = "range"
                change_confirmed = True

        else:  # previous range
            if obs_score > enter_bull and obs_label == "bull":
                new_state = "bull"
                change_confirmed = True
            elif obs_score < enter_bear and obs_label == "bear":
                new_state = "bear"
                change_confirmed = True

        # filtre confirmation 2 ticks si historique court dispo
        recent_obs = list(self._trend_obs_hist[symbol])[-2:]
        if change_confirmed and recent_obs:
            if obs_label not in recent_obs and previous_state != "range":
                flip_blocked = True
                new_state = previous_state
                change_confirmed = False

        return new_state, {
            "flip_blocked": flip_blocked,
            "change_confirmed": change_confirmed,
        }

    def _apply_vol_hysteresis(
        self,
        symbol: str,
        previous_state: str,
        obs_score: float,
        obs_label: str,
    ) -> Tuple[str, Dict]:
        previous_state = previous_state if previous_state in {"low", "medium", "high"} else "medium"

        new_state = previous_state
        change_confirmed = False
        flip_blocked = False

        if previous_state == "high":
            if obs_score < -0.30 and obs_label == "low":
                new_state = "medium"
                change_confirmed = True
            elif obs_score < 0.10 and obs_label == "medium":
                new_state = "medium"
                change_confirmed = True

        elif previous_state == "low":
            if obs_score > 0.55 and obs_label == "high":
                new_state = "medium"
                change_confirmed = True
            elif obs_score > -0.02 and obs_label == "medium":
                new_state = "medium"
                change_confirmed = True

        else:  # medium
            if obs_score > 0.45 and obs_label == "high":
                new_state = "high"
                change_confirmed = True
            elif obs_score < -0.18 and obs_label == "low":
                new_state = "low"
                change_confirmed = True

        recent_obs = list(self._vol_obs_hist[symbol])[-2:]
        if change_confirmed and recent_obs:
            if obs_label not in recent_obs and previous_state != "medium":
                flip_blocked = True
                new_state = previous_state
                change_confirmed = False

        return new_state, {
            "flip_blocked": flip_blocked,
            "change_confirmed": change_confirmed,
        }

    # ─────────────────────────────────────────────────────
    # Persistance / stabilité
    # ─────────────────────────────────────────────────────

    @staticmethod
    def _compute_state_age(states: List[str]) -> int:
        if not states:
            return 0
        current = states[-1]
        age = 0
        for s in reversed(states):
            if s == current:
                age += 1
            else:
                break
        return age

    @staticmethod
    def _compute_stability(states: List[str], current_state: str, lookback: int = 20) -> float:
        if not states:
            return 0.0
        sample = states[-lookback:]
        if not sample:
            return 0.0
        matches = sum(1 for s in sample if s == current_state)
        return matches / len(sample)

    def _compute_persistence_pack(
        self,
        trend_age: int,
        vol_age: int,
        trend_stability: float,
        vol_stability: float,
        trend_stay_prob: float,
        vol_stay_prob: float,
    ) -> Dict:
        age_score = self._clip((min(trend_age, 20) / 20.0) * 0.6 + (min(vol_age, 20) / 20.0) * 0.4, 0.0, 1.0)
        persistence_score = self._clip(
            trend_stability * 0.35
            + vol_stability * 0.20
            + trend_stay_prob * 0.25
            + vol_stay_prob * 0.20,
            0.0,
            1.0,
        )

        recent_change = trend_age <= 2 or vol_age <= 2

        return {
            "regime_age_score": age_score,
            "regime_persistence_score": persistence_score,
            "recent_change_flag": recent_change,
        }

    # ─────────────────────────────────────────────────────
    # Markov discret
    # ─────────────────────────────────────────────────────

    def _markov_transition_stats(
        self,
        states: List[str],
        current_state: str,
    ) -> Dict:
        if not states or len(states) < 3:
            return {
                "stay_probability": 0.5,
                "switch_probability": 0.5,
                "next_probs": {},
            }

        all_states = sorted(set(states))
        counts: Dict[str, Dict[str, int]] = {
            s: {t: 1 for t in all_states} for s in all_states
        }  # Laplace smoothing

        for i in range(1, len(states)):
            prev_s = states[i - 1]
            next_s = states[i]
            counts.setdefault(prev_s, {})
            counts[prev_s][next_s] = counts[prev_s].get(next_s, 1) + 1

        row = counts.get(current_state, {})
        total = sum(row.values()) if row else 0
        if total <= 0:
            return {
                "stay_probability": 0.5,
                "switch_probability": 0.5,
                "next_probs": {},
            }

        next_probs = {k: v / total for k, v in row.items()}
        stay_prob = next_probs.get(current_state, 0.0)

        return {
            "stay_probability": stay_prob,
            "switch_probability": 1.0 - stay_prob,
            "next_probs": next_probs,
        }

    # ─────────────────────────────────────────────────────
    # Pseudo-HMM / état latent
    # ─────────────────────────────────────────────────────

    def _compute_latent_state(
        self,
        trend_state: str,
        trend_obs_score: float,
        trend_stability: float,
        trend_markov_stay: float,
        vol_state: str,
        vol_obs_score: float,
        vol_stability: float,
        vol_markov_stay: float,
        previous_regime: Dict,
    ) -> Dict:
        prev_latent_trend = str(previous_regime.get("latent_trend_state", trend_state) or trend_state)
        prev_latent_vol = str(previous_regime.get("latent_volatility_state", vol_state) or vol_state)

        trend_conf = self._clip(
            abs(trend_obs_score) * 0.45 + trend_stability * 0.30 + trend_markov_stay * 0.25,
            0.0,
            1.0,
        )
        vol_conf = self._clip(
            abs(vol_obs_score) * 0.40 + vol_stability * 0.30 + vol_markov_stay * 0.30,
            0.0,
            1.0,
        )

        # inertie latente: on ne change pas si la confiance n'est pas suffisante
        latent_trend = trend_state
        if trend_conf < 0.42 and prev_latent_trend in {"bull", "bear", "range"}:
            latent_trend = prev_latent_trend

        latent_vol = vol_state
        if vol_conf < 0.40 and prev_latent_vol in {"low", "medium", "high"}:
            latent_vol = prev_latent_vol

        if latent_trend == "bull" and latent_vol == "high":
            latent_market_state = "bull_volatile"
        elif latent_trend == "bull" and latent_vol in {"low", "medium"}:
            latent_market_state = "bull_orderly"
        elif latent_trend == "bear" and latent_vol == "high":
            latent_market_state = "bear_volatile"
        elif latent_trend == "bear" and latent_vol in {"low", "medium"}:
            latent_market_state = "bear_orderly"
        elif latent_trend == "range" and latent_vol == "high":
            latent_market_state = "range_chaotic"
        elif latent_trend == "range" and latent_vol == "low":
            latent_market_state = "range_compressed"
        else:
            latent_market_state = "range_balanced"

        latent_conf = self._clip(trend_conf * 0.60 + vol_conf * 0.40, 0.0, 1.0)
        transition_risk = self._clip(
            (1.0 - trend_markov_stay) * 0.45
            + (1.0 - vol_markov_stay) * 0.25
            + (1.0 - trend_stability) * 0.20
            + (1.0 - vol_stability) * 0.10,
            0.0,
            1.0,
        )

        return {
            "latent_trend_state": latent_trend,
            "latent_volatility_state": latent_vol,
            "latent_market_state": latent_market_state,
            "latent_confidence": latent_conf,
            "transition_risk": transition_risk,
        }

    # ─────────────────────────────────────────────────────
    # Risk / directive
    # ─────────────────────────────────────────────────────

    def _compute_risk_and_directive(
        self,
        trend_state: str,
        vol_state: str,
        latent_state: str,
        latent_confidence: float,
        features: Dict,
    ) -> Dict:
        breakout = self._safe_float(features.get("breakout_readiness_score"))
        mean_rev = self._safe_float(features.get("mean_reversion_score"))
        scalp = self._safe_float(features.get("scalp_readiness_score"))
        liquidity = self._safe_float(features.get("liquidity_score"))
        spread_state = str(features.get("spread_state", "normal") or "normal")

        risk_score = 0.0

        if vol_state == "high":
            risk_score += 0.45
        elif vol_state == "medium":
            risk_score += 0.20
        else:
            risk_score += 0.08

        if spread_state == "wide":
            risk_score += 0.25
        if liquidity < 0.35:
            risk_score += 0.20
        if latent_confidence < 0.35:
            risk_score += 0.15

        risk_score = self._clip(risk_score, 0.0, 1.0)

        if risk_score >= 0.72:
            risk = "high"
        elif risk_score >= 0.38:
            risk = "medium"
        else:
            risk = "low"

        if latent_state in {"bull_orderly"} and breakout >= 0.55 and scalp >= 0.50:
            directive = "prefer_buy_pullback_or_breakout"
        elif latent_state in {"bear_orderly"} and breakout >= 0.55 and scalp >= 0.50:
            directive = "prefer_sell_rally_or_breakdown"
        elif latent_state in {"range_compressed", "range_balanced"} and mean_rev >= 0.55:
            directive = "prefer_mean_reversion_wait_confirmation"
        elif latent_state in {"range_chaotic", "bull_volatile", "bear_volatile"}:
            directive = "reduce_aggressiveness_wait_cleaner_setup"
        else:
            directive = "mixed_wait_or_selective"

        return {
            "risk": risk,
            "directive": directive,
        }

    # ─────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────

    @staticmethod
    def _clip(x: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, float(x)))

    @staticmethod
    def _safe_float(value, default: float = 0.0) -> float:
        try:
            if value is None:
                return float(default)
            return float(value)
        except Exception:
            return float(default)

"""
Détecteur de régime probabiliste par règles + softmax.

Cf. spec §3 et §6.4 : sortie en probabilités (régime doux), hystérésis sur le
label dominant pour éviter le whipsaw.

Algorithme :
  1. Pour chaque symbole de la watchlist, extraire features (ADX, Hurst,
     vol_percentile, autocorr_lag1, returns_slope_zscore).
  2. Moyenne des features sur les symboles (cap-weight simple = égal poids).
  3. Score brut par régime via combinaisons linéaires :
       trend_up    : +slope*ADX_norm + Hurst
       trend_down  : -slope*ADX_norm + Hurst
       range       : -ADX_norm - |Hurst-0.5|
       high_vol    : +vol_percentile (override si > seuil)
  4. Softmax(temperature) → probabilités.
  5. argmax → label candidat. Hystérésis : on ne change le label "courant"
     qu'après min_dwell ticks consécutifs où l'argmax diffère.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Optional

import numpy as np

from core.config import RegimeConfig
from core.types import Candle, MarketSnapshot, Regime, RegimeState
from regime.features import (
    adx,
    autocorr_lag1,
    hurst_rs,
    returns_slope_zscore,
    vol_percentile,
)


def _extract_arrays(candles: list[Candle]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    h = np.array([c.high for c in candles], dtype=float)
    l = np.array([c.low for c in candles], dtype=float)
    c = np.array([c.close for c in candles], dtype=float)
    return h, l, c


def _softmax(scores: dict[Regime, float], temperature: float = 1.0) -> dict[Regime, float]:
    """Softmax stable numériquement."""
    keys = list(scores.keys())
    vals = np.array([scores[k] / temperature for k in keys], dtype=float)
    vals = vals - vals.max()  # stabilité
    exps = np.exp(vals)
    s = exps.sum()
    if s <= 0:
        # tout à -inf → uniforme
        n = len(keys)
        return {k: 1.0 / n for k in keys}
    probs = exps / s
    return {k: float(p) for k, p in zip(keys, probs)}


class RuleBasedRegimeDetector:
    """Détecteur de régime règle-based avec sortie probabiliste et hystérésis."""

    def __init__(self, config: RegimeConfig, softmax_temperature: float = 0.5) -> None:
        self._cfg = config
        self._temperature = softmax_temperature
        # État interne pour l'hystérésis (label GLOBAL agrégé, cf. detect())
        self._current_label: Optional[Regime] = None
        self._candidate_label: Optional[Regime] = None
        self._candidate_dwell: int = 0
        # Hystérésis PAR SYMBOLE (cf. detect_per_symbol()) : un marché peut avoir
        # BTC en trend_up et SOL en range simultanément. Chaque symbole garde son
        # propre état sticky. clé = symbole → {current, candidate, dwell}.
        self._sym_hyst: dict[str, dict] = {}
        # Pour le test no-leak et le debug
        self._last_features: dict = {}

    # ─── API publique ─────────────────────────────────────────────────────────

    def _symbol_features(self, candles: list[Candle]) -> Optional[dict]:
        """Calcule le vecteur de features d'un symbole, ou None si insuffisant/NaN."""
        if len(candles) < 30:  # pas assez pour ADX(14)
            return None
        h, l, c = _extract_arrays(candles)
        feat = {
            "adx": adx(h, l, c, period=14),
            "hurst": hurst_rs(c, min_chunk=8),
            "vol_p": vol_percentile(c, window=24, lookback=min(100, len(c) - 25)),
            "autocorr": autocorr_lag1(c, window=min(50, len(c) - 2)),
            "slope_z": returns_slope_zscore(c, window=min(48, len(c) - 2)),
        }
        # filtre les NaN/None
        if any(v is None or (isinstance(v, float) and not math.isfinite(v)) for v in feat.values()):
            return None
        return feat

    def _features_to_probas(self, feat: dict) -> dict[Regime, float]:
        """Scoring rule-based + softmax sur un vecteur de features (agrégé OU
        par symbole). Pure fonction de `feat` → probabilités par régime."""
        adx_norm = feat["adx"] / 100.0  # ADX ∈ [0,100] → [0,1]
        slope = feat["slope_z"]          # signé, magnitude libre (~ -3..+3)
        hurst = feat["hurst"]            # ~ 0..1 (théorique 0.5 RW)
        vol_p = feat["vol_p"]            # [0, 1]
        autoc = feat["autocorr"]         # [-1, 1]

        # Scoring rule-based avec amplification :
        # Sur crypto 1h, le slope_z reste typiquement faible (<0.3 en magnitude)
        # même sur trends visibles. On amplifie × SLOPE_GAIN pour faire ressortir
        # les signaux directionnels modérés. ADX > 25 (= adx_norm > 0.25) signale
        # un marché directionnel ; on en fait un seuil "soft".
        SLOPE_GAIN = 5.0
        ADX_TREND_THRESHOLD = 0.25
        SLOPE_TREND_THRESHOLD = 0.10  # |slope_z| > 0.10 amorce le trend

        # Force directionnelle : slope amplifié × bonus si ADX au-dessus du seuil
        adx_bonus = max(0.0, adx_norm - ADX_TREND_THRESHOLD) * 4.0  # [0, ~3]
        directional_amplitude = abs(slope) * SLOPE_GAIN + adx_bonus
        # Confidence trend : amorcée à partir de SLOPE_TREND_THRESHOLD
        trend_amorce = max(0.0, abs(slope) - SLOPE_TREND_THRESHOLD)

        if slope >= 0:
            score_up = directional_amplitude * (1.0 + trend_amorce) + 0.5 * max(0.0, autoc)
            score_down = -directional_amplitude * 0.3  # pénalité au sens opposé
        else:
            score_down = directional_amplitude * (1.0 + trend_amorce) + 0.5 * max(0.0, autoc)
            score_up = -directional_amplitude * 0.3

        # Range : ADX bas, slope faible, Hurst proche de 0.5
        range_strength = max(0.0, ADX_TREND_THRESHOLD - adx_norm) * 4.0 + \
                         max(0.0, SLOPE_TREND_THRESHOLD - abs(slope)) * 5.0
        score_range = 1.0 + range_strength - abs(hurst - 0.5) + 0.3 * max(0.0, -autoc)

        # High vol : déclenche au-dessus du seuil de percentile, sinon malus.
        if vol_p > self._cfg.high_vol_atr_percentile:
            score_hv = 2.0 + (vol_p - self._cfg.high_vol_atr_percentile) * 5.0
        else:
            score_hv = -2.0

        scores = {
            Regime.TREND_UP: score_up,
            Regime.TREND_DOWN: score_down,
            Regime.RANGE: score_range,
            Regime.HIGH_VOL: score_hv,
        }
        return _softmax(scores, temperature=self._temperature)

    def detect(self, market: MarketSnapshot) -> RegimeState:
        """Régime GLOBAL agrégé (moyenne des features sur la watchlist).

        Conservé pour le gouverneur de risque, le stratège et le logging — qui
        raisonnent sur une posture d'ensemble. Le pilotage par instrument
        (allocation, grille) utilise detect_per_symbol().
        """
        # 1. Agrégation des features sur tous les symboles disponibles
        per_symbol: list[dict] = [
            feat for candles in market.candles.values()
            if (feat := self._symbol_features(candles)) is not None
        ]

        if not per_symbol:
            # Pas de données utiles → uniforme + label prudent.
            uniform = {r: 0.25 for r in Regime}
            return RegimeState(
                timestamp=market.timestamp,
                probabilities=uniform,
                label=Regime.RANGE,  # défaut prudent
                confidence=0.25,
            )

        # Moyenne simple sur les symboles
        keys = per_symbol[0].keys()
        avg = {k: float(np.mean([d[k] for d in per_symbol])) for k in keys}
        self._last_features = avg

        probas = self._features_to_probas(avg)
        argmax_label = max(probas, key=probas.get)

        # Hystérésis sur le label dominant. La confidence reportée suit le label
        # EFFECTIF (sticky), pas l'argmax brut — sinon en transition on affichait
        # "label=trend_up conf=0.51" où 0.51 était en fait P(range).
        effective_label = self._apply_hysteresis(argmax_label)

        return RegimeState(
            timestamp=market.timestamp,
            probabilities=probas,
            label=effective_label,
            confidence=probas[effective_label],
        )

    def detect_per_symbol(self, market: MarketSnapshot) -> dict[str, RegimeState]:
        """Régime PAR INSTRUMENT : un RegimeState distinct par symbole, chacun
        avec sa propre hystérésis sticky. Permet à BTC d'être trend_up pendant
        que SOL est range — l'allocation et la grille gatent alors par symbole.

        Les symboles sans données suffisantes sont absents du dict (l'appelant
        retombe sur le régime global).
        """
        out: dict[str, RegimeState] = {}
        for sym, candles in market.candles.items():
            feat = self._symbol_features(candles)
            if feat is None:
                continue
            probas = self._features_to_probas(feat)
            argmax_label = max(probas, key=probas.get)
            effective_label = self._apply_hysteresis_sym(sym, argmax_label)
            out[sym] = RegimeState(
                timestamp=market.timestamp,
                probabilities=probas,
                label=effective_label,
                confidence=probas[effective_label],
            )
        return out

    def reset(self) -> None:
        """Reset état interne (pour tests / backtests)."""
        self._current_label = None
        self._candidate_label = None
        self._candidate_dwell = 0
        self._sym_hyst.clear()
        self._last_features = {}

    @property
    def last_features(self) -> dict:
        return dict(self._last_features)

    # ─── Hystérésis ───────────────────────────────────────────────────────────

    def _apply_hysteresis(self, argmax_label: Regime) -> Regime:
        """N'autorise un changement de label que si le nouveau argmax est
        stable pendant min_dwell_bars ticks consécutifs.

        Premier appel : adopte directement argmax (pas d'hystérésis au démarrage).
        """
        if self._current_label is None:
            # Premier appel
            self._current_label = argmax_label
            self._candidate_label = argmax_label
            self._candidate_dwell = 0
            return argmax_label

        if argmax_label == self._current_label:
            # Argmax confirme le label courant → reset candidat
            self._candidate_label = argmax_label
            self._candidate_dwell = 0
            return self._current_label

        # Argmax diffère du courant → on accumule des "votes" pour le candidat
        if argmax_label == self._candidate_label:
            self._candidate_dwell += 1
        else:
            self._candidate_label = argmax_label
            self._candidate_dwell = 1

        if self._candidate_dwell >= self._cfg.min_dwell_bars:
            # Transition autorisée
            self._current_label = self._candidate_label
            self._candidate_dwell = 0
            return self._current_label

        # Pas encore assez : on garde le label courant (mais on renvoie les
        # probas brutes — c'est le label qui est sticky, pas les probas).
        return self._current_label

    def _apply_hysteresis_sym(self, sym: str, argmax_label: Regime) -> Regime:
        """Même logique que _apply_hysteresis mais avec un état dédié par symbole
        (self._sym_hyst[sym]). Évite qu'un symbole hérite du label sticky d'un
        autre."""
        st = self._sym_hyst.get(sym)
        if st is None:
            # Premier passage sur ce symbole : adopte directement l'argmax.
            self._sym_hyst[sym] = {"current": argmax_label, "candidate": argmax_label, "dwell": 0}
            return argmax_label

        if argmax_label == st["current"]:
            st["candidate"] = argmax_label
            st["dwell"] = 0
            return st["current"]

        if argmax_label == st["candidate"]:
            st["dwell"] += 1
        else:
            st["candidate"] = argmax_label
            st["dwell"] = 1

        if st["dwell"] >= self._cfg.min_dwell_bars:
            st["current"] = st["candidate"]
            st["dwell"] = 0

        return st["current"]

"""
HMM SuperBot (SPEC §4) — Gaussian HMM à deux niveaux, hmmlearn.

  - MARCHÉ  : BTC 4h, K=4 états (bull_orderly / bear_orderly / range_compressed /
              high_vol_chaotic), features 4D → state/hmm/market.pkl
  - SYMBOLE : chaque coin actif, K=3 (trending_up / trending_down / choppy),
              features 5D au timeframe de sa sleeve → state/hmm/{SYMBOL}.pkl

Anti-overfit obligatoire à l'entraînement (walk-forward 70/30) :
  1. la log-vraisemblance OUT-OF-SAMPLE (par échantillon) ne doit pas se
     dégrader de plus de HMM_MAX_LL_DEGRADATION vs in-sample ;
  2. les états doivent rester SÉPARÉS (distance min inter-centroïdes, espace
     standardisé) ;
  3. pas d'état dégénéré (> HMM_MAX_STATE_OCCUPANCY du temps en train) ;
  4. minimum de données (HMM_MIN_BARS bougies).
Échec → pas de modèle sauvé, l'appelant retombe sur le fallback ADX.

Labels par centroïdes (jamais par index d'état, arbitraire chez hmmlearn) :
  marché  : high_vol_chaotic = ATR% max ; range_compressed = ADX min des
            restants ; bull/bear = return max/min des deux derniers.
  symbole : trending_up = return max ; trending_down = return min ;
            le restant = choppy.

Inférence online : standardisation avec les moments du train, posteriors par
forward filtering (predict_proba), confiance = max, transition_risk =
1 - transmat[état, état].
"""

from __future__ import annotations

import logging
import pickle
import time
from typing import Dict, List, Optional

import numpy as np

from superbot import config
from superbot.indicators import build_market_features, build_symbol_features

logger = logging.getLogger("sdm.superbot.hmm")

MARKET_LABELS = ("bull_orderly", "bear_orderly", "range_compressed", "high_vol_chaotic")
SYMBOL_LABELS = ("trending_up", "trending_down", "choppy")

# Seuils de validation (surchargeables par env — voir config)
HMM_MAX_LL_DEGRADATION = 0.10     # -10 % max de LL/échantillon OOS vs train
HMM_MIN_STATE_SEP = 0.25          # distance min inter-centroïdes (standardisé)
HMM_MAX_STATE_OCCUPANCY = 0.80    # un état > 80 % du train = dégénéré
HMM_MIN_BARS = 60                 # rejet si moins de bougies (SPEC §4)


class _FittedModel:
    """Modèle sérialisable : GaussianHMM + moments de standardisation + labels."""

    def __init__(self, model, mean, std, label_map, kind, timeframe=None):
        self.model = model
        self.mean = mean                  # np.array (n_features,)
        self.std = std
        self.label_map = label_map        # {state_index: label}
        self.kind = kind                  # "market" | "symbol"
        self.timeframe = timeframe
        self.trained_at = time.time()

    def standardize(self, X: np.ndarray) -> np.ndarray:
        return (X - self.mean) / self.std

    def infer(self, X_raw: np.ndarray) -> Dict:
        """Posterior de la DERNIÈRE observation d'une fenêtre (forward filtering)."""
        Xs = self.standardize(np.asarray(X_raw, dtype=float))
        probs = self.model.predict_proba(Xs)[-1]
        idx = int(np.argmax(probs))
        return {
            "state": self.label_map[idx],
            "confidence": float(probs[idx]),
            "transition_risk": float(1.0 - self.model.transmat_[idx, idx]),
            "state_probs": {self.label_map[i]: round(float(p), 4)
                            for i, p in enumerate(probs)},
            "source": "hmm",
        }


def _standardize_fit(X: np.ndarray):
    mean = X.mean(axis=0)
    std = X.std(axis=0)
    std[std < 1e-12] = 1.0            # feature constante (ex. funding absent)
    return mean, std


def _fit_gaussian(X: np.ndarray, n_states: int, n_iter: int):
    from hmmlearn.hmm import GaussianHMM
    model = GaussianHMM(n_components=n_states, covariance_type="diag",
                        n_iter=n_iter, random_state=42)
    model.fit(X)
    return model


def _validate(model, X_train: np.ndarray, X_valid: np.ndarray) -> Optional[str]:
    """None si le modèle passe le walk-forward, sinon la raison du rejet."""
    ll_train = model.score(X_train) / max(len(X_train), 1)
    ll_valid = model.score(X_valid) / max(len(X_valid), 1)
    denom = abs(ll_train) if abs(ll_train) > 1e-9 else 1.0
    degradation = (ll_train - ll_valid) / denom
    if degradation > HMM_MAX_LL_DEGRADATION:
        return f"ll_oos_degrade_{degradation:.2f}"

    means = model.means_
    n = len(means)
    min_sep = min(
        float(np.linalg.norm(means[i] - means[j]))
        for i in range(n) for j in range(i + 1, n)
    )
    if min_sep < HMM_MIN_STATE_SEP:
        return f"etats_confondus_sep_{min_sep:.2f}"

    states = model.predict(X_train)
    occup = np.bincount(states, minlength=n) / max(len(states), 1)
    if float(occup.max()) > HMM_MAX_STATE_OCCUPANCY:
        return f"etat_degenere_{occup.max():.2f}"
    return None


def _label_market(model, mean, std) -> Dict[int, str]:
    """Centroïdes DÉ-standardisés → interprétation (features: ret, atr%, adx, fund)."""
    raw = model.means_ * std + mean
    remaining = set(range(len(raw)))
    labels: Dict[int, str] = {}
    chaotic = max(remaining, key=lambda i: raw[i][1])          # ATR% max
    labels[chaotic] = "high_vol_chaotic"; remaining.discard(chaotic)
    compressed = min(remaining, key=lambda i: raw[i][2])       # ADX min
    labels[compressed] = "range_compressed"; remaining.discard(compressed)
    bull = max(remaining, key=lambda i: raw[i][0])             # return max
    labels[bull] = "bull_orderly"; remaining.discard(bull)
    labels[remaining.pop()] = "bear_orderly"
    return labels


def _label_symbol(model, mean, std) -> Dict[int, str]:
    """trending_up = centroïde return max, trending_down = min, restant = choppy."""
    raw = model.means_ * std + mean
    remaining = set(range(len(raw)))
    labels: Dict[int, str] = {}
    up = max(remaining, key=lambda i: raw[i][0])
    labels[up] = "trending_up"; remaining.discard(up)
    down = min(remaining, key=lambda i: raw[i][0])
    labels[down] = "trending_down"; remaining.discard(down)
    labels[remaining.pop()] = "choppy"
    return labels


class HMMRegimeEngine:
    """API unifiée (SPEC §4) : fit/infer marché et symboles + prune."""

    def __init__(self, hmm_dir=None):
        self.hmm_dir = hmm_dir or config.HMM_DIR
        self._cache: Dict[str, _FittedModel] = {}

    # ── chemins / persistance ────────────────────────────────────────────────

    def _path(self, name: str):
        return self.hmm_dir / f"{name}.pkl"

    def _save(self, name: str, fitted: _FittedModel) -> None:
        """Écriture ATOMIQUE (tmp + rename) : le trader peut charger le .pkl à
        tout instant — une écriture directe l'exposait à un pickle tronqué
        (observé au premier démarrage : load silencieux → fallback ADX à tort)."""
        import os
        self.hmm_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path(name).with_suffix(".tmp")
        with open(tmp, "wb") as f:
            pickle.dump(fitted, f)
        os.replace(tmp, self._path(name))
        self._cache[name] = fitted

    def load(self, name: str) -> Optional[_FittedModel]:
        if name in self._cache:
            return self._cache[name]
        try:
            with open(self._path(name), "rb") as f:
                fitted = pickle.load(f)
            self._cache[name] = fitted
            return fitted
        except Exception:
            return None

    def has_model(self, name: str) -> bool:
        return self.load(name) is not None

    # ── entraînement (offline, walk-forward 70/30) ───────────────────────────

    def _fit(self, name: str, X_rows: List[List[float]], n_states: int,
             n_iter: int, labeler, kind: str, timeframe=None) -> Optional[_FittedModel]:
        X = np.asarray(X_rows, dtype=float)
        if len(X) < max(HMM_MIN_BARS, n_states * 10):
            logger.info("HMM %s: données insuffisantes (%d bougies)", name, len(X))
            return None
        split = int(len(X) * 0.70)
        mean, std = _standardize_fit(X[:split])
        Xs = (X - mean) / std
        try:
            model = _fit_gaussian(Xs[:split], n_states, n_iter)
        except Exception as e:
            logger.warning("HMM %s: fit échoué (%r)", name, e)
            return None
        reason = _validate(model, Xs[:split], Xs[split:])
        if reason is not None:
            logger.info("HMM %s: validation OOS rejetée — %s", name, reason)
            self.delete(name)
            return None
        fitted = _FittedModel(model, mean, std, labeler(model, mean, std),
                              kind, timeframe)
        self._save(name, fitted)
        logger.info("HMM %s: entraîné et validé (%d bougies, K=%d) — états %s",
                    name, len(X), n_states, sorted(fitted.label_map.values()))
        return fitted

    def fit_market(self, candles_btc_4h: List[dict],
                   funding: Optional[List[float]] = None) -> Optional[_FittedModel]:
        rows = build_market_features(candles_btc_4h, funding)
        return self._fit("market", rows, config.HMM_MARKET_STATES,
                         200, _label_market, "market", "4h")

    def fit_symbol(self, symbol: str, candles: List[dict],
                   timeframe: str) -> Optional[_FittedModel]:
        rows = build_symbol_features(candles)
        return self._fit(symbol.upper(), rows, config.HMM_SYMBOL_STATES,
                         150, _label_symbol, "symbol", timeframe)

    # ── inférence (online) ───────────────────────────────────────────────────

    def infer_market(self, candles_btc_4h: List[dict],
                     funding: Optional[List[float]] = None) -> Optional[Dict]:
        fitted = self.load("market")
        if fitted is None:
            return None
        rows = build_market_features(candles_btc_4h, funding)
        return fitted.infer(rows)

    def infer_symbol(self, symbol: str, candles: List[dict]) -> Optional[Dict]:
        fitted = self.load(symbol.upper())
        if fitted is None:
            return None
        rows = build_symbol_features(candles)
        out = fitted.infer(rows)
        out["timeframe"] = fitted.timeframe
        return out

    # ── entretien ────────────────────────────────────────────────────────────

    def delete(self, name: str) -> None:
        self._cache.pop(name, None)
        try:
            self._path(name).unlink()
        except FileNotFoundError:
            pass

    def prune_stale(self, active_symbols: set) -> List[str]:
        """Supprime les .pkl des symboles devenus inactifs (market préservé)."""
        removed = []
        if not self.hmm_dir.exists():
            return removed
        keep = {s.upper() for s in active_symbols} | {"market"}
        for p in self.hmm_dir.glob("*.pkl"):
            if p.stem not in keep:
                p.unlink()
                self._cache.pop(p.stem, None)
                removed.append(p.stem)
        if removed:
            logger.info("HMM prune: modèles supprimés %s", removed)
        return removed

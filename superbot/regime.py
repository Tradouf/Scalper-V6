"""
Façade régime SuperBot (SPEC §4) — choisit la source, applique l'hystérésis,
persiste l'état pour l'orchestrateur et le dashboard.

Sélection de la source :
  | market.pkl existe ET confiance >= HMM_MARKET_MIN_CONF | HMM gaussien |
  | modèle absent ou confiance basse                       | fallback ADX |
  | toujours                                               | pseudo-Markov (transition_risk) |

Hystérésis (anti-churn, obligatoire) :
  - marché  : le régime publié ne change que si confiance > MIN_CONF ET le
    nouvel état est majoritaire sur les 2 dernières bougies 4h ET
    transition_risk < 0.45 ;
  - symbole : idem avec MIN_CONF symbole et 2 bougies du TF de la sleeve.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Dict, List, Optional

from simplebot.strategy import ema

from superbot import config
from superbot.hmm import HMMRegimeEngine
from superbot.indicators import adx
from superbot.markov import compute_latent_state

logger = logging.getLogger("sdm.superbot.regime")

MARKET_HYSTERESIS_RISK = 0.45
HYSTERESIS_BARS = 2


# ── Fallbacks ADX (règles déterministes, SPEC §4) ────────────────────────────

def fallback_market_state(candles_btc_4h: List[dict]) -> Dict:
    """BTC 4h : adx<20 → range ; close>ema50>ema200 & adx>=25 → bull ;
    close<ema50<ema200 & adx>=25 → bear ; sinon high_vol_chaotic."""
    closes = [c["close"] for c in candles_btc_4h]
    a = adx(candles_btc_4h, 14)[-1]
    e50 = ema(closes, 50)[-1]
    e200 = ema(closes, 200)[-1]
    close = closes[-1]
    if a < 20:
        state = "range_compressed"
    elif close > e50 > e200 and a >= 25:
        state = "bull_orderly"
    elif close < e50 < e200 and a >= 25:
        state = "bear_orderly"
    else:
        state = "high_vol_chaotic"
    return {"state": state, "confidence": 0.5, "transition_risk": 0.5,
            "state_probs": {}, "source": "fallback_adx"}


def fallback_symbol_state(candles: List[dict]) -> Dict:
    """TF de la sleeve : adx<18 → choppy ; close vs ema50 + adx>=22 → trend."""
    closes = [c["close"] for c in candles]
    a = adx(candles, 14)[-1]
    e50 = ema(closes, 50)[-1]
    close = closes[-1]
    if a < 18:
        state = "choppy"
    elif close > e50 and a >= 22:
        state = "trending_up"
    elif close < e50 and a >= 22:
        state = "trending_down"
    else:
        state = "choppy"
    return {"state": state, "confidence": 0.5, "transition_risk": 0.5,
            "state_probs": {}, "source": "fallback_adx"}


# ── Façade ───────────────────────────────────────────────────────────────────

class RegimeFacade:

    def __init__(self, engine: Optional[HMMRegimeEngine] = None,
                 market_file=None, symbols_file=None):
        self.engine = engine or HMMRegimeEngine()
        self.market_file = market_file or config.REGIME_MARKET_FILE
        self.symbols_file = symbols_file or config.REGIME_SYMBOLS_FILE
        self._market_state = self._load(self.market_file)
        self._symbol_states = self._load(self.symbols_file)

    @staticmethod
    def _load(path) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    @staticmethod
    def _save(path, payload: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning("Sauvegarde régime %s échouée: %r", path, e)

    # ── Hystérésis générique ─────────────────────────────────────────────────

    @staticmethod
    def _apply_hysteresis(raw: Dict, previous: Dict, min_conf: float,
                          max_risk: float) -> Dict:
        """Le régime PUBLIÉ ne bascule que si le brut est confiant, répété sur
        HYSTERESIS_BARS observations consécutives, et loin d'une transition."""
        prev_state = previous.get("state")
        recent = list(previous.get("recent_raw", []))
        recent.append(raw["state"])
        recent = recent[-HYSTERESIS_BARS:]

        out = dict(raw)
        out["recent_raw"] = recent
        if prev_state is None:
            return out
        if raw["state"] == prev_state:
            return out
        confirmed = (
            raw["confidence"] >= min_conf
            and len(recent) >= HYSTERESIS_BARS
            and all(s == raw["state"] for s in recent)
            and raw["transition_risk"] < max_risk
        )
        if not confirmed:
            out["state"] = prev_state
            out["held_by_hysteresis"] = True
            out["pending_state"] = raw["state"]
        return out

    # ── Marché ───────────────────────────────────────────────────────────────

    def market_regime(self, candles_btc_4h: List[dict],
                      funding: Optional[List[float]] = None) -> Dict:
        raw = None
        if self.engine.has_model("market"):
            try:
                raw = self.engine.infer_market(candles_btc_4h, funding)
            except Exception as e:
                logger.warning("Inférence HMM marché échouée (%r) — fallback", e)
        if raw is None or raw["confidence"] < config.HMM_MARKET_MIN_CONF:
            fb = fallback_market_state(candles_btc_4h)
            if raw is not None:      # HMM peu confiant : trace mais bascule
                fb["hmm_low_conf"] = round(raw["confidence"], 3)
            raw = fb

        out = self._apply_hysteresis(raw, self._market_state,
                                     config.HMM_MARKET_MIN_CONF,
                                     MARKET_HYSTERESIS_RISK)
        # pseudo-Markov : transition_risk composite sur l'historique publié
        history = list(self._market_state.get("history", []))
        latent = compute_latent_state(out["state"], out["confidence"], history,
                                      previous_latent=self._market_state.get("state"))
        out["markov_transition_risk"] = round(latent["transition_risk"], 4)
        history.append(out["state"])
        out["history"] = history[-200:]
        out["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")

        self._market_state = out
        self._save(self.market_file, out)
        return out

    # ── Symbole ──────────────────────────────────────────────────────────────

    def symbol_regime(self, symbol: str, candles: List[dict],
                      timeframe: str) -> Dict:
        sym = symbol.upper()
        raw = None
        if self.engine.has_model(sym):
            try:
                raw = self.engine.infer_symbol(sym, candles)
            except Exception as e:
                logger.warning("Inférence HMM %s échouée (%r) — fallback", sym, e)
        if raw is None or raw["confidence"] < config.HMM_SYMBOL_MIN_CONF:
            fb = fallback_symbol_state(candles)
            if raw is not None:
                fb["hmm_low_conf"] = round(raw["confidence"], 3)
            raw = fb

        prev = self._symbol_states.get(sym, {})
        out = self._apply_hysteresis(raw, prev, config.HMM_SYMBOL_MIN_CONF,
                                     config.HMM_TRANSITION_FREEZE)
        history = list(prev.get("history", []))
        latent = compute_latent_state(out["state"], out["confidence"], history,
                                      previous_latent=prev.get("state"))
        out["markov_transition_risk"] = round(latent["transition_risk"], 4)
        history.append(out["state"])
        out["history"] = history[-200:]
        out["timeframe"] = timeframe
        out["allowed"] = {
            "long": out["state"] == "trending_up",
            "short": out["state"] == "trending_down",
        }
        out["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")

        self._symbol_states[sym] = out
        self._save(self.symbols_file, self._symbol_states)
        return out

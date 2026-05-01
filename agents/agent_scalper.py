"""
AgentScalper V10.2 — garde-fous deterministes avec vraies cles features.
"""
from __future__ import annotations
import logging
from typing import Dict, Optional
from .base_agent import BaseAgent
from memory.shared_memory import SharedMemory

try:
    from config import settings as SETTINGS
except Exception:
    SETTINGS = None

logger = logging.getLogger("sdm.scalper")


class AgentScalper(BaseAgent):
    def __init__(self, memory: SharedMemory) -> None:
        super().__init__("scalper", memory)
        self.default_leverage = float(getattr(SETTINGS, "DEFAULT_LEVERAGE", 3) or 3)
        self.tp_pnl_pct = float(getattr(SETTINGS, "SCALP_TP_PNL_PCT", 0.03) or 0.03)
        self.sl_pnl_pct = float(getattr(SETTINGS, "SCALP_SL_PNL_PCT", 0.015) or 0.015)
        self.min_sl_pct = 0.0010
        self.max_sl_pct = 0.010
        self.min_tp_pct = 0.0015
        self.max_tp_pct = 0.01200
        self.ratio_min_floor = 0.90   # floor absolu (winrate > 70%)
        self.ratio_max = 5.00          # plafond absolu (winrate < 25%)

    def _get_dynamic_ratio_min(self, symbol: str, leverage: float) -> float:
        """
        Calcule le ratio TP/SL minimum dynamique selon le winrate du learner.
        
        Formule: ratio_min = [(1-w)*SL_ROE + fees_ROE] / (w*SL_ROE) × marge_sécurité
        
        Args:
            symbol: Le symbole tradé
            leverage: Levier effectif
            
        Returns:
            Ratio minimum adapté au winrate (avec marge 20%)
        """
        # Frais round-trip en ROE (0.045% taker × 2 côtés × leverage)
        fees_roe_rt = 0.045 * 2.0 * leverage / 100.0
        
        # SL typique en ROE (1.5% standard)
        sl_roe_typical = 1.5
        
        # Lire le profil learner pour ce symbole
        try:
            regime = self.memory.get_regime() or {}
            regime_key = f"{regime.get('trend', 'range')}_{regime.get('volatility', 'medium')}"
            profile = self.memory.get_scalper_profile(symbol, regime_key) or {}
            
            winrate = float(profile.get("winrate", 0.0) or 0.0)
            samples = int(profile.get("samples", 0) or 0)
            
            # Si pas assez d'historique, ratio safe par défaut (bon pour 50% WR)
            if samples < 5:
                logger.debug(
                    "SCALPER %s ratio_min dynamique: samples=%d < 5, fallback ratio=1.67 (safe 50%% WR)",
                    symbol, samples
                )
                return 1.67
            
            # Winrate trop faible = protection maximale
            if winrate < 0.01:
                logger.info(
                    "SCALPER %s ratio_min dynamique: WR=%.1f%% quasi-nul, ratio_max=%.2f",
                    symbol, winrate * 100, self.ratio_max
                )
                return self.ratio_max
            
            # Calcul math du ratio minimum pour break-even
            # Équation: TP × w = SL × (1-w) + fees
            # Ratio = TP/SL = [(1-w) × SL + fees] / (w × SL)
            ratio_theoretical = ((1.0 - winrate) * sl_roe_typical + fees_roe_rt) / (winrate * sl_roe_typical)
            
            # Marge de sécurité 20%
            ratio_with_margin = ratio_theoretical * 1.20
            
            # Clamper entre floor et max
            ratio_final = max(self.ratio_min_floor, min(ratio_with_margin, self.ratio_max))
            
            logger.info(
                "SCALPER %s ratio_min dynamique: WR=%.1f%% n=%d fees_roe=%.3f%% → ratio_theo=%.2f ratio_final=%.2f",
                symbol, winrate * 100, samples, fees_roe_rt, ratio_theoretical, ratio_final
            )
            
            return ratio_final
            
        except Exception as e:
            logger.warning("SCALPER %s erreur calcul ratio dynamique: %r, fallback 1.67", symbol, e)
            return 1.67

    def _safe_parse(self, raw) -> Optional[Dict]:
        if raw is None:
            return None
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                parsed = self.parse_json(raw) if hasattr(self, "parse_json") else self._parse_json(raw)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                return None
        return None

    def _neutral_result(self, reason: str) -> Dict:
        return {"entry": 0.0, "sl": 0.0, "tp": 0.0, "confidence": 0.0, "reason": reason}

    def _target_tp_price_pct(self, leverage: float) -> float:
        return self.tp_pnl_pct / max(1.0, float(leverage or self.default_leverage or 3.0))

    def _target_sl_price_pct(self, leverage: float) -> float:
        return self.sl_pnl_pct / max(1.0, float(leverage or self.default_leverage or 3.0))

    def _clamp_sl_dist(self, entry: float, sl_dist: float, atr: float) -> float:
        if entry <= 0:
            return 0.0
        atr_floor = atr * 0.60 if atr > 0 else entry * self.min_sl_pct
        sl_dist = max(sl_dist, atr_floor, entry * self.min_sl_pct)
        sl_dist = min(sl_dist, entry * self.max_sl_pct)
        return sl_dist

    def _build_prices_from_distances(self, side: str, entry: float, sl_dist: float, tp_dist: float) -> Dict:
        side = (side or "").lower()
        if side == "buy":
            return {"entry": round(entry, 8), "sl": round(entry - sl_dist, 8), "tp": round(entry + tp_dist, 8)}
        elif side == "sell":
            return {"entry": round(entry, 8), "sl": round(entry + sl_dist, 8), "tp": round(entry - tp_dist, 8)}
        return {"entry": 0.0, "sl": 0.0, "tp": 0.0}

    def _normalize(
        self,
        symbol: str,
        side: str,
        entry: float,
        sl: float,
        tp: float,
        atr: float,
        leverage: float,
    ) -> Optional[Dict]:
        if entry <= 0:
            return None

        side = (side or "").lower()
        if side not in ("buy", "sell"):
            return None

        sl = float(sl or 0)
        tp = float(tp or 0)

        valid_geom = False
        raw_sl_dist = 0.0
        raw_tp_dist = 0.0

        if sl > 0 and tp > 0:
            if side == "buy" and sl < entry < tp:
                valid_geom = True
                raw_sl_dist = entry - sl
                raw_tp_dist = tp - entry
            elif side == "sell" and tp < entry < sl:
                valid_geom = True
                raw_sl_dist = sl - entry
                raw_tp_dist = entry - tp

        if not valid_geom:
            raw_sl_dist = entry * self._target_sl_price_pct(leverage)
            raw_tp_dist = entry * self._target_tp_price_pct(leverage)

        if raw_sl_dist <= 0:
            return None

        sl_dist = self._clamp_sl_dist(entry, raw_sl_dist, atr)

        # Calcul du ratio dynamique selon le winrate du symbole
        ratio_min_dynamic = self._get_dynamic_ratio_min(symbol, leverage)
        
        target_tp_dist = entry * self._target_tp_price_pct(leverage)
        tp_floor = max(entry * self.min_tp_pct, sl_dist * ratio_min_dynamic, target_tp_dist)
        tp_cap = min(entry * self.max_tp_pct, sl_dist * self.ratio_max)
        tp_dist = tp_floor if tp_cap < tp_floor else min(max(raw_tp_dist, tp_floor), tp_cap)

        return self._build_prices_from_distances(side, entry, sl_dist, tp_dist)

    def decide(
    self,
    symbol: str,
    side: str,
    technical: Dict,
    regime: Dict,
    consensus: Dict,
    orderbook: Optional[Dict] = None,
    leverage: Optional[float] = None,
) -> Dict:
        ind = technical.get("indicators", {}) if isinstance(technical, dict) else {}
        price = float(ind.get("price", 0) or technical.get("price", 0) or 0)
        atr = float(ind.get("atr", 0) or 0)
        rsi = float(ind.get("rsi", 50) or 50)
        volratio = float(ind.get("vol_ratio", ind.get("volratio", 1)) or 1)
        bbpos = float(ind.get("bb_position", 50) or 50)
        cons_conf = float(consensus.get("confidence", 0) or 0)
        ob = orderbook or {}
        spreadpct = float(ob.get("spread_pct", 0) or 0)
        is_liquid = bool(ob.get("is_liquid_enough", True))
        book_entry = float(ob.get("recommended_entry_price", price) or price)
        effective_leverage = float(
            leverage
            or getattr(SETTINGS, "MAX_LEVERAGE", None)
            or self.default_leverage
            or 3.0
        )
        if price <= 0:
            return self._neutral_result("prix indisponible")

        # Lecture features depuis SharedMemory (vraies APIs)
        features = {}
        try:
            features = self.memory.get_advanced_features(symbol) or {}
        except Exception:
            features = {}

        # Vraies cles FeatureEngine / RegimeEngine
        slope_multi_horizon      = str(features.get("slope_multi_horizon", "unknown"))
        slope_alignment_score    = float(features.get("slope_alignment_score", 0.0) or 0.0)
        micro_trend              = str(features.get("micro_trend", "unknown"))
        vwap_reversion_score     = float(features.get("vwap_reversion_score", 0.0) or 0.0)
        regime_persistence_score = float(features.get("regime_persistence_score", 0.0) or 0.0)
        latent_trend_state       = str(features.get("latent_trend_state", "unknown"))
        latent_market_state      = str(features.get("latent_market_state", "unknown"))
        latent_confidence        = float(features.get("latent_confidence", 0.0) or 0.0)
        hmm_transition_risk      = float(features.get("hmm_like_transition_risk", 0.0) or 0.0)
        ob_imbalance             = float(ob.get("bid_ask_imbalance", features.get("bid_ask_imbalance", 0.0)) or 0.0)

        # Garde-fous deterministes (seuils sur slope_alignment_score)
        side_lower = (side or "").lower()
        if side_lower == "buy":
            if slope_alignment_score < -0.8 and micro_trend in ("bear_microtrend", "bearish"):
                logger.info("SCALPER %s garde-fou feature_conflict_buy slope_score=%.3f micro=%s", symbol, slope_alignment_score, micro_trend)
                return self._neutral_result("feature_conflict_buy")
            if ob_imbalance < -0.15:
                logger.info("SCALPER %s garde-fou orderbook_conflict_buy imb=%.3f", symbol, ob_imbalance)
                return self._neutral_result("orderbook_conflict_buy")
        elif side_lower == "sell":
            if slope_alignment_score > 0.8 and micro_trend in ("bull_microtrend", "bullish"):
                logger.info("SCALPER %s garde-fou feature_conflict_sell slope_score=%.3f micro=%s", symbol, slope_alignment_score, micro_trend)
                return self._neutral_result("feature_conflict_sell")
            if ob_imbalance > 0.15:
                logger.info("SCALPER %s garde-fou orderbook_conflict_sell imb=%.3f", symbol, ob_imbalance)
                return self._neutral_result("orderbook_conflict_sell")

        logger.info(
            "SCALPER FEATURES %s side=%s slope=%s(%.2f) micro=%s vwap_rev=%.4f persist=%.2f latent=%s/%s conf=%.2f trans_risk=%.3f ob_imb=%.3f",
            symbol, side_lower.upper(), slope_multi_horizon, slope_alignment_score,
            micro_trend, vwap_reversion_score, regime_persistence_score,
            latent_trend_state, latent_market_state, latent_confidence,
            hmm_transition_risk, ob_imbalance,
        )

        atr_pct = (atr / price * 100.0) if atr > 0 else 0.0
        tp_price_pct = self._target_tp_price_pct(effective_leverage) * 100.0
        sl_price_pct = self._target_sl_price_pct(effective_leverage) * 100.0

        system = """
        Tu es un agent scalper crypto professionnel de niveau prop desk.

        Le side est IMPOSE et ne doit jamais être changé.

        Ta mission est de proposer UNIQUEMENT un setup exécutable immédiatement, avec des prix strictement cohérents pour :
        - entryprice
        - slprice
        - tpprice
        - confidence
        - reason

        REGLES ABSOLUES DE GEOMETRIE :
        - Si side = "buy" :
            - slprice doit être strictement inférieur à entryprice
            - tpprice doit être strictement supérieur à entryprice
            - géométrie obligatoire : slprice < entryprice < tpprice

        - Si side = "sell" :
            - tpprice doit être strictement inférieur à entryprice
            - slprice doit être strictement supérieur à entryprice
            - géométrie obligatoire : tpprice < entryprice < slprice

        INTERDICTIONS ABSOLUES :
        - Ne jamais inverser SL et TP
        - Ne jamais mettre slprice = entryprice
        - Ne jamais mettre tpprice = entryprice
        - Ne jamais retourner un prix négatif ou nul
        - Ne jamais modifier le side imposé
        - Ne jamais produire un setup géométriquement invalide

        METHODE OBLIGATOIRE :
        1. Lire le side imposé
        2. Choisir une entryprice réaliste et proche du prix courant
        3. Définir une distance de stop cohérente
        4. Définir une distance de take profit cohérente
        5. Construire les prix autour de l’entrée :
            - buy  => slprice = entryprice - distance_stop ; tpprice = entryprice + distance_tp
            - sell => slprice = entryprice + distance_stop ; tpprice = entryprice - distance_tp
        6. Vérifier mentalement la géométrie finale avant de répondre
        7. Si une seule règle est violée, retourner un setup invalide nul

        FALLBACK OBLIGATOIRE SI LE SETUP N'EST PAS CLAIR OU PAS VALIDE :
        {"entryprice":0,"slprice":0,"tpprice":0,"confidence":0,"reason":"invalid setup"}

        FORMAT DE SORTIE :
        - Réponds UNIQUEMENT en JSON strict
        - Aucun texte hors JSON
        - Aucune explication
        - Aucune balise markdown

        SCHEMA JSON EXACT :
        {
        "entryprice": <float>,
        "slprice": <float>,
        "tpprice": <float>,
        "confidence": <float entre 0 et 1>,
        "reason": "<string court>"
        }
        """.strip()

        user = "\n".join([
            f"Symbole: {symbol}",
            f"Side impose: {side}",
            f"Prix spot actuel: {price:.6f}",
            f"Prix entree suggere orderbook: {book_entry:.6f}",
            f"Confiance consensus: {cons_conf:.2f}",
            f"RSI: {rsi:.1f}",
            f"ATR: {atr:.6f} ({atr_pct:.2f}% du prix)",
            f"Volume ratio: {volratio:.2f}x",
            f"Position Bollinger: {bbpos:.0f}",
            f"Spread: {spreadpct:.3%}",
            f"Liquide: {is_liquid}",
            f"Regime: trend={regime.get('trend','?')} vol={regime.get('volatility','?')} risk={regime.get('risk','?')}",
            f"Levier: {effective_leverage:.1f}x | TP cible: +{self.tp_pnl_pct*100:.2f}% | SL cible: -{self.sl_pnl_pct*100:.2f}%",
            f"Distance TP prix: {tp_price_pct:.3f}% | Distance SL prix: {sl_price_pct:.3f}%",
            f"Slope multi-horizon: {slope_multi_horizon} (score={slope_alignment_score:.3f})",
            f"Micro trend: {micro_trend}",
            f"VWAP reversion score: {vwap_reversion_score:.4f}",
            f"Regime persistence: {regime_persistence_score:.2f}",
            f"Latent state: {latent_trend_state}/{latent_market_state} conf={latent_confidence:.2f}",
            f"Transition risk: {hmm_transition_risk:.3f}",
            f"Orderbook imbalance: {ob_imbalance:.3f}",
            "Scalp court terme. Pas de texte hors JSON.",
            '{"entryprice": 100.0, "slprice": 99.73, "tpprice": 100.4, "confidence": 0.63, "reason": "scalp_buy_example"}',
            '{"entryprice": 100.0, "slprice": 100.4, "tpprice": 99.73, "confidence": 0.63, "reason": "scalp_sell_example"}',

        ])

        raw = None
        try:
            raw = self.llm(system, user, temperature=0.15, max_tokens=300) if hasattr(self, "llm") else self._llm(system, user, temperature=0.15, max_tokens=300)
        except Exception as e:
            logger.warning("SCALPER %s LLM error: %r", symbol, e)
            return self._neutral_result("llm_error")
        
        # ICI : log de la réponse brute du LLM
        logger.info("SCALPER RAW %s %r", symbol, raw)

        parsed = self._safe_parse(raw)
        if not parsed:
            logger.info("SCALPER %s INVALID_JSON %s", symbol, side.upper())
            return self._neutral_result("llm_invalid_json")

        entry = float(parsed.get("entryprice") or parsed.get("entry") or book_entry or price)
        sl = float(parsed.get("slprice") or parsed.get("sl") or 0)
        tp = float(parsed.get("tpprice") or parsed.get("tp") or 0)
        confidence = float(parsed.get("confidence", cons_conf) or cons_conf)
        reason = str(parsed.get("reason", "") or "")[:300]

        norm = self._normalize(
            symbol=symbol,
            side=side,
            entry=entry,
            sl=sl,
            tp=tp,
            atr=atr,
            leverage=effective_leverage,
        )
        if not norm:
            logger.info("SCALPER %s INVALID_PRICES %s entry=%.6f sl=%.6f tp=%.6f", symbol, side.upper(), entry, sl, tp)
            return self._neutral_result("llm_invalid_prices")
        
        logger.info(
        "SCALPER NORM %s %s entry=%.6f sl=%.6f tp=%.6f",
        symbol, side.upper(), norm["entry"], norm["sl"], norm["tp"],
        )

        move_tp = abs(norm["tp"] - norm["entry"])
        roe_tp  = (move_tp / norm["entry"] * effective_leverage) if norm["entry"] > 0 else 0.0
        move_sl = abs(norm["entry"] - norm["sl"])
        roe_sl  = (move_sl / norm["entry"] * effective_leverage) if norm["entry"] > 0 else 0.0

        result = {
            "entry":      norm["entry"],
            "sl":         norm["sl"],
            "tp":         norm["tp"],
            "confidence": max(0.0, min(1.0, confidence)),
            "reason":     reason or f"scalp_{side}",
            "pnl_tp":     round(roe_tp * 100, 4),
            "pnl_sl":     round(roe_sl * 100, 4),
        }
    
        logger.info(
            "SCALPER %s ENTER %s entry=%.6f sl=%.6f tp=%.6f | pnl_tp=%.2f%% pnl_sl=%.2f%% conf=%.2f %s",
            symbol, side.upper(), result["entry"], result["sl"], result["tp"],
            roe_tp * 100, roe_sl * 100, result["confidence"], result["reason"][:120],
        )

        try:
            if hasattr(self.memory, "add_signal"):
                self.memory.add_signal("agent_scalper", symbol=symbol, action="ENTER",
                                       confidence=result["confidence"], reason=result["reason"][:200])
        except Exception:
            pass

        return result

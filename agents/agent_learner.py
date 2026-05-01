"""
AgentLearner V5 — agent_learner.py

Apprend des trades réels (et plus tard des backtests) pour
ajuster les profils de scalping par symbole + régime.
"""

from __future__ import annotations

import logging
from statistics import mean
from typing import Dict, List, Optional

from .base_agent import BaseAgent
from memory.shared_memory import SharedMemory

logger = logging.getLogger("sdm.learner")

_DEFAULT_SL_ATR = 1.8
_DEFAULT_TP_ATR = 3.0
_DEFAULT_TRAIL = 1.2

_MIN_SL_ATR = 0.8
_MAX_SL_ATR = 3.5
_MIN_TP_ATR = 1.2
_MAX_TP_ATR = 8.0

_MIN_TRADES_FOR_PENALTY = 5


class AgentLearner(BaseAgent):
    def __init__(self, memory: SharedMemory) -> None:
        super().__init__("learner", memory)

    def _regime_key(self, regime: Optional[Dict]) -> str:
        if not regime:
            regime = self.memory.get_regime() or {}
        trend = regime.get("trend", "range")
        vol = regime.get("volatility", "medium")
        return f"{trend}_{vol}"

    def _base_defaults(self) -> Dict:
        return {
            "sl_atr_mult": _DEFAULT_SL_ATR,
            "tp_atr_mult": _DEFAULT_TP_ATR,
            "trailing_atr_mult": _DEFAULT_TRAIL,
            "risk_factor": 1.0,
            "winrate": 0.0,
            "avg_pnl": 0.0,
            "samples": 0,
        }

    def learn(self, symbol: Optional[str] = None, lookback: int = 200) -> None:
        """
        Met à jour les profils scalper à partir de l'historique de trades
        + (optionnel) des résultats de backtest.
        """
        all_trades: List[Dict] = self.memory.get("trade_history", [])[-lookback:]
        if not all_trades:
            logger.info("Learner: aucun trade pour apprendre.")
            return

        per_symbol: Dict[str, List[Dict]] = {}
        for t in all_trades:
            sym = t.get("symbol")
            if not sym:
                continue
            if symbol and sym != symbol:
                continue
            per_symbol.setdefault(sym, []).append(t)

        if not per_symbol:
            logger.info("Learner: aucun trade pour le symbole %s", symbol)
            return

        regime = self.memory.get_regime()
        regime_key = self._regime_key(regime)

        for sym, trades in per_symbol.items():
            pnls = [float(t.get("pnl", 0.0) or 0.0) for t in trades]
            if not pnls:
                continue

            winrate = sum(1 for p in pnls if p > 0) / len(pnls)
            avg_pnl = mean(pnls)
            samples = len(trades)

            current = self.memory.get_scalper_profile(sym, regime_key) or self._base_defaults()

            sl = float(current.get("sl_atr_mult", _DEFAULT_SL_ATR) or _DEFAULT_SL_ATR)
            tp = float(current.get("tp_atr_mult", _DEFAULT_TP_ATR) or _DEFAULT_TP_ATR)
            trail = float(current.get("trailing_atr_mult", _DEFAULT_TRAIL) or _DEFAULT_TRAIL)
            risk_factor = float(current.get("risk_factor", 1.0) or 1.0)

            bt = self.memory.get(f"backtest_{sym}", None)
            if bt:
                bt_sl = float(bt.get("sl_atr_mult", sl) or sl)
                bt_tp = float(bt.get("tp_atr_mult", tp) or tp)
                sl = (sl + bt_sl) / 2.0
                tp = (tp + bt_tp) / 2.0

            # -----------------------------------------------------------------
            # NOUVELLE LOGIQUE :
            # On pénalise vraiment les profils perdants au lieu de faire
            # seulement un petit tp -= 0.2.
            # -----------------------------------------------------------------

            if samples >= _MIN_TRADES_FOR_PENALTY:
                # Cas positif : on garde une légère liberté
                if winrate > 0.55 and avg_pnl > 0:
                    tp += 0.2
                    risk_factor = max(risk_factor, 1.10)

                # Cas moyen / négatif : on resserre
                if avg_pnl < 0:
                    sl = min(sl, 1.20)
                    tp = min(tp, 1.80)
                    trail = min(trail, 1.10)
                    risk_factor = min(risk_factor, 0.85)

                # Cas plus mauvais : faible winrate
                if winrate < 0.40:
                    sl = min(sl, 1.00)
                    tp = min(tp, 1.50)
                    trail = min(trail, 1.00)
                    risk_factor = min(risk_factor, 0.75)

                # Très mauvais profil : perdant + faible winrate
                if avg_pnl < 0 and winrate < 0.35:
                    sl = min(sl, 0.80)
                    tp = min(tp, 1.20)
                    trail = min(trail, 0.90)
                    risk_factor = min(risk_factor, 0.60)

                # Si vraiment excellent, on autorise un peu plus de TP
                if avg_pnl > 0 and winrate >= 0.60:
                    sl = max(sl, 1.20)
                    tp = max(tp, 2.20)
                    risk_factor = max(risk_factor, 1.15)

            else:
                # Trop peu d'échantillons : on évite les profils extrêmes
                sl = min(sl, 1.80)
                tp = min(tp, 3.00)
                trail = min(trail, 1.20)
                risk_factor = min(max(risk_factor, 0.90), 1.00)

            # Clamp final de sécurité
            sl = max(_MIN_SL_ATR, min(_MAX_SL_ATR, sl))
            tp = max(max(_MIN_TP_ATR, sl * 1.10), min(_MAX_TP_ATR, tp))
            trail = max(0.8, min(2.0, trail))
            risk_factor = max(0.50, min(1.25, risk_factor))

            new_profile = {
                "sl_atr_mult": sl,
                "tp_atr_mult": tp,
                "trailing_atr_mult": trail,
                "risk_factor": risk_factor,
                "winrate": winrate,
                "avg_pnl": avg_pnl,
                "samples": samples,
            }

            self.memory.update_scalper_profile(sym, regime_key, new_profile)

            logger.info(
                "Learner: profil mis à jour %s[%s] → SL=%.2f ATR, TP=%.2f ATR, TRAIL=%.2f ATR, risk=%.2f, winrate=%.0f%%, avg_pnl=%.4f (n=%d)",
                sym,
                regime_key,
                sl,
                tp,
                trail,
                risk_factor,
                winrate * 100,
                avg_pnl,
                samples,
            )

    # API appelée par main_v6
    def update_profiles(self) -> None:
        """
        Méthode façade pour main_v6: apprend sur tous les trades récents.
        """
        self.learn(symbol=None, lookback=200)

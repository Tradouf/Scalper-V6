"""
AgentMeanReversion — agent déterministe (pas LLM) pour stratégie mean-reversion.

Logique :
  Entrée LONG  : z-score < -ENTRY_Z   (prix anormalement bas)
  Entrée SHORT : z-score > +ENTRY_Z   (prix anormalement haut)
  Sortie       : |z-score| < EXIT_Z   (retour à la moyenne)

Filtres pré-entrée :
  - Régime != "range"           → skip (mean-rev meurt en trend)
  - Half-life hors [MIN, MAX]   → skip (série non stationnaire)
  - Symbole géré par le grid    → skip (cohérence avec Lot 3)
  - Position scalp déjà ouverte → skip (un seul system par symbole)
  - Cooldown récent             → skip

Sizing :
  qty pondérée par la half-life : plus le retour est rapide, plus la taille.
  factor = clamp((MAX_HL - hl) / (MAX_HL - MIN_HL), 0.3, 1.0)

Pas de LLM, pas d'agent_messages. Lit OHLCV depuis l'exchange, écrit son
état dans shared_memory pour le dashboard.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

from utils.stats import zscore, half_life, rolling_mean_std

logger = logging.getLogger("sdm.mean_reversion")


class AgentMeanReversion:
    """Agent déterministe — pas de BaseAgent (pas d'LLM)."""

    def __init__(self, exchange, memory, config: Optional[Dict] = None):
        self._exchange = exchange
        self._memory = memory
        self._cfg = config or {}
        # Dernier signal vu par symbole pour cooldown / éviter retriggers
        self._last_signal_ts: Dict[str, float] = {}
        self._last_close_ts: Dict[str, float] = {}
        # Snapshot interne (debug / dashboard)
        self._last_metrics: Dict[str, Dict] = {}

    # ─── Params (lus dans config/settings.py) ─────────────────────────────────

    @property
    def window(self) -> int:
        return int(self._cfg.get("window", 50))

    @property
    def entry_z(self) -> float:
        return float(self._cfg.get("entry_z", 2.0))

    @property
    def exit_z(self) -> float:
        return float(self._cfg.get("exit_z", 0.4))

    @property
    def hl_min(self) -> float:
        return float(self._cfg.get("hl_min", 5.0))

    @property
    def hl_max(self) -> float:
        return float(self._cfg.get("hl_max", 48.0))

    @property
    def interval(self) -> str:
        return str(self._cfg.get("interval", "1h"))

    @property
    def symbols(self) -> List[str]:
        return list(self._cfg.get("symbols", ["ETH", "SOL", "LINK"]))

    @property
    def cooldown_sec(self) -> int:
        return int(self._cfg.get("cooldown_sec", 1800))  # 30 min

    @property
    def max_positions(self) -> int:
        return int(self._cfg.get("max_positions", 2))

    # ─── API publique ─────────────────────────────────────────────────────────

    def evaluate(
        self,
        symbol: str,
        regime: Dict,
        position_open: bool,
        grid_active: bool,
    ) -> Dict:
        """Calcule un signal pour un symbole.

        Returns dict avec :
          signal : "LONG" | "SHORT" | "CLOSE" | "HOLD" | "SKIP"
          reason : str (motif SKIP/HOLD)
          z, hl, mean, std : métriques calculées
          size_factor : float ∈ [0.3, 1.0] si signal d'entrée
        """
        out: Dict = {
            "symbol": symbol, "signal": "SKIP", "reason": "", "z": None,
            "hl": None, "mean": None, "std": None, "size_factor": 1.0,
        }

        # Filtre 1 : symbole dans la whitelist MR
        if symbol not in self.symbols:
            out["reason"] = "not_in_mr_symbols"
            return out

        # Filtre 2 : régime range only
        trend = str(regime.get("trend", "")).lower()
        if trend != "range":
            out["reason"] = f"regime_not_range ({trend})"
            return out

        # Filtre 3 : grid actif → skip (Lot 3)
        if grid_active:
            out["reason"] = "grid_active"
            return out

        # Filtre 4 : cooldown
        now = time.time()
        last = max(
            self._last_signal_ts.get(symbol, 0.0),
            self._last_close_ts.get(symbol, 0.0),
        )
        if now - last < self.cooldown_sec:
            out["reason"] = f"cooldown ({int(self.cooldown_sec - (now - last))}s)"
            return out

        # Récupère OHLCV
        candles = self._get_closes(symbol, limit=max(150, self.window * 3))
        if candles is None or len(candles) < self.window + 5:
            out["reason"] = "insufficient_data"
            return out

        # Indicateurs
        z = zscore(candles, self.window)
        hl = half_life(candles)
        mu, sd = rolling_mean_std(candles, self.window)
        out.update({"z": z, "hl": hl, "mean": mu, "std": sd})

        if z is None or hl is None:
            out["reason"] = "indicators_unavailable"
            return out

        # Filtre 5 : half-life dans la fenêtre exploitable
        if hl <= 0 or hl < self.hl_min or hl > self.hl_max:
            out["reason"] = f"hl_out_of_range (hl={hl:.1f}, target [{self.hl_min},{self.hl_max}])"
            return out

        # Position déjà ouverte sur ce symbole : on évalue le signal CLOSE
        if position_open:
            if abs(z) < self.exit_z:
                out["signal"] = "CLOSE"
                out["reason"] = f"reverted (|z|={abs(z):.2f} < {self.exit_z})"
                self._last_close_ts[symbol] = now
            else:
                out["signal"] = "HOLD"
                out["reason"] = f"position_open, awaiting revert (z={z:.2f})"
            self._last_metrics[symbol] = dict(out)
            return out

        # Pas de position : on regarde l'entrée
        size_factor = self._size_factor(hl)
        out["size_factor"] = size_factor

        if z < -self.entry_z:
            out["signal"] = "LONG"
            out["reason"] = f"z={z:.2f} < -{self.entry_z}"
            self._last_signal_ts[symbol] = now
        elif z > self.entry_z:
            out["signal"] = "SHORT"
            out["reason"] = f"z={z:.2f} > +{self.entry_z}"
            self._last_signal_ts[symbol] = now
        else:
            out["signal"] = "HOLD"
            out["reason"] = f"z={z:.2f} in band [-{self.entry_z}, +{self.entry_z}]"

        self._last_metrics[symbol] = dict(out)
        return out

    def get_last_metrics(self) -> Dict[str, Dict]:
        """Snapshot pour dashboard."""
        return dict(self._last_metrics)

    # ─── Privé ────────────────────────────────────────────────────────────────

    def _size_factor(self, hl: float) -> float:
        """Pondération sizing : half-life plus rapide = taille plus grande.

        factor = clamp((max_hl - hl) / (max_hl - min_hl), 0.3, 1.0)
        """
        if hl <= self.hl_min:
            return 1.0
        if hl >= self.hl_max:
            return 0.3
        raw = (self.hl_max - hl) / (self.hl_max - self.hl_min)
        return max(0.3, min(1.0, raw))

    def _get_closes(self, symbol: str, limit: int = 150) -> Optional[List[float]]:
        """Récupère les closes via le client HL. Renvoie None si erreur."""
        try:
            candles = self._exchange.get_candles(symbol, interval=self.interval, limit=limit)
            if not candles:
                return None
            return [float(c.get("close", 0) or 0) for c in candles]
        except Exception as e:
            logger.warning("MR %s get_candles error: %r", symbol, e)
            return None

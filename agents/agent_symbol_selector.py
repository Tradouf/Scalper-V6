from __future__ import annotations

import logging
from typing import Dict, List, Any

from memory.shared_memory import SharedMemory

logger = logging.getLogger("sdm.symbol_selector")


class AgentSymbolSelector:
    """
    Sélectionne dynamiquement une liste de symboles actifs à trader.

    Sources :
    - Hyperliquid meta (universe) : tous les marchés disponibles.
    - Profils Learner en mémoire : winrate, avg_pnl, n_trades par symbole/régime.
    """

    def __init__(
        self,
        memory: SharedMemory,
        max_active: int = 20,
        max_explore: int = 5,
        min_trades_confirmed: int = 5,
        min_winrate: float = 0.35,
    ) -> None:
        self.memory = memory
        self.max_active = max_active
        self.max_explore = max_explore
        self.min_trades_confirmed = min_trades_confirmed
        self.min_winrate = min_winrate

    def _get_learner_profiles(self) -> Dict[str, Dict[str, Any]]:
        """
        Récupère les profils du Learner depuis la mémoire partagée.

        On suppose que le Learner stocke un dict du type :
        {
          "BTC": { "bull_medium": {"n": 10, "winrate": 0.5, "avg_pnl": 0.002}, ... },
          "ETH": { ... },
          ...
        }
        """
        profiles = self.memory.get("learner_profiles") or {}
        if not isinstance(profiles, dict):
            return {}
        return profiles

    def _aggregate_symbol_stats(self, symbol: str, regimes: Dict[str, Any]) -> Dict[str, float]:
        """
        Agrège les stats Learner sur tous les régimes pour un symbole.

        Retourne un dict:
        {
          "n_trades": int,
          "winrate": float,
          "avg_pnl": float,
        }
        """
        total_n = 0
        wins = 0.0
        pnl_sum = 0.0

        for _regime, stats in regimes.items():
            try:
                n = int(stats.get("n", 0) or 0)
                winrate = float(stats.get("winrate", 0.0) or 0.0)
                avg_pnl = float(stats.get("avg_pnl", 0.0) or 0.0)
            except Exception:
                continue

            if n <= 0:
                continue

            total_n += n
            wins += winrate * n
            pnl_sum += avg_pnl * n

        if total_n <= 0:
            return {"n_trades": 0, "winrate": 0.0, "avg_pnl": 0.0}

        agg_winrate = wins / total_n
        agg_pnl = pnl_sum / total_n

        return {
            "n_trades": total_n,
            "winrate": agg_winrate,
            "avg_pnl": agg_pnl,
        }

    def _is_tradeable_meta(self, m: Dict[str, Any]) -> bool:
        """
        Filtre technique de base sur les données meta Hyperliquid.

        On s'assure que le marché est listé, avec un levier max raisonnable, etc.
        """
        try:
            is_delisted = bool(m.get("isDelisted", False))
            max_lev = float(m.get("maxLeverage", 3.0) or 3.0)
        except Exception:
            return False

        if is_delisted:
            return False

        # Si tu veux exclure des marchés trop exotiques, tu peux mettre un seuil ici.
        if max_lev < 2.0:
            return False

        return True

    def refresh_from_meta(self, universe_meta: List[Dict[str, Any]]) -> List[str]:
        """
        Construit la liste active_symbols à partir de l'univers Hyperliquid + Learner.

        - universe_meta: liste d'objets meta (un par marché), typiquement récupérés via
          client.info({"type": "meta"})["universe"]
        """
        learner_profiles = self._get_learner_profiles()

        tradeable = [m for m in universe_meta if self._is_tradeable_meta(m)]

        confirmed: List[str] = []
        candidates_new: List[Dict[str, Any]] = []

        for m in tradeable:
            raw_name = m.get("name") or m.get("symbol") or ""
            symbol = str(raw_name).upper()

            regimes = learner_profiles.get(symbol, {})
            if isinstance(regimes, dict) and regimes:
                stats = self._aggregate_symbol_stats(symbol, regimes)
                n_trades = stats["n_trades"]
                winrate = stats["winrate"]
                avg_pnl = stats["avg_pnl"]

                if (
                    n_trades >= self.min_trades_confirmed
                    and (winrate >= self.min_winrate or avg_pnl >= 0.0)
                ):
                    confirmed.append(symbol)
            else:
                # Pas de profil Learner -> candidat à l'exploration
                candidates_new.append(m)

        # Tri des nouveaux candidats par liquidité/volume si dispo
        def vol_key(m: Dict[str, Any]) -> float:
            try:
                return float(m.get("dayNtlVlm", 0.0) or 0.0)
            except Exception:
                return 0.0

        candidates_new.sort(key=vol_key, reverse=True)
        explore: List[str] = [
            str(m.get("name") or m.get("symbol") or "").upper()
            for m in candidates_new[: self.max_explore]
        ]

        # Dé-duplication et tronquage
        active: List[str] = []
        for s in confirmed + explore:
            if s and s not in active:
                active.append(s)
            if len(active) >= self.max_active:
                break

        if not active:
            logger.warning("SymbolSelector: aucune sélection trouvée, on garde la config actuelle")
            return []

        self.memory.update_analysis(
        "__symbol_selector__",  # symbole spécial
        "active_symbols",
        {"symbols": active},
        )
        logger.info(
        "SymbolSelector: %d confirmés + %d explorés => actifs=%s",
        len(confirmed),
        len(explore),
        active,
        )
        return active

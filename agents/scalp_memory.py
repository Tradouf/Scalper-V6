"""
Scalp Memory — apprentissage par expérience (2026-05-08).

Trois objectifs (cf. discussion utilisateur du 2026-05-08) :
  A. Tag de cause sur chaque fermeture (différencier trail-profit, sl-loss,
     emergency, manuel).
  B. Snapshot d'entrée (indicateurs + confs des 3 strates + régime) corrélé
     à l'outcome → permet le calcul de stats "win rate par condition".
  C. (Implémenté côté agents LLM) injection contextuelle des trades récents
     dans les prompts.

Stockage JSON simple, append-only, atomic write. Pas de DB pour rester
portable et auditable à la main.
"""
from __future__ import annotations
import json
import logging
import os
import time
import threading
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("sdm.scalp_memory")

DEFAULT_PATH = "memory/scalp_memory.json"
MAX_TRADES_KEPT = 5000  # rétention raisonnable, > 1 mois de trading


class ScalpMemory:
    """API d'apprentissage par expérience pour le pipeline scalp."""

    # Causes connues de fermeture
    CAUSES = {
        "tp_natif_hit",         # TP natif touché (gain attendu)
        "trail_hit_profit",     # Trail SL touché en gain (a "monté" depuis l'entry)
        "sl_natif_hit_loss",    # SL initial touché (perte attendue)
        "emergency_exit",       # Emergency safety (-2× SL_PCT)
        "manual_or_unknown",    # Fermeture externe non identifiable
        "scalper_exit",         # Le scalper LLM a décidé d'EXIT
        "flip",                 # Position flippée (close + reverse)
    }

    def __init__(self, path: str = DEFAULT_PATH):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data = self._load()

    # ── I/O ─────────────────────────────────────────────────────────────────
    def _load(self) -> Dict:
        if not self._path.exists():
            return {"trades": []}
        try:
            d = json.loads(self._path.read_text(encoding="utf-8"))
            if "trades" not in d or not isinstance(d["trades"], list):
                d = {"trades": []}
            return d
        except Exception as e:
            logger.warning("ScalpMemory load: %r — repart vide", e)
            return {"trades": []}

    def _save(self) -> None:
        # Tronque si dépasse la rétention max
        if len(self._data["trades"]) > MAX_TRADES_KEPT:
            self._data["trades"] = self._data["trades"][-MAX_TRADES_KEPT:]
        tmp = self._path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False, default=str),
                           encoding="utf-8")
            tmp.replace(self._path)
        except Exception as e:
            logger.warning("ScalpMemory save: %r", e)

    # ── API publique ────────────────────────────────────────────────────────
    def record_entry(self, symbol: str, side: str, entry_px: float,
                     snapshot: Dict) -> str:
        """Crée un nouveau trade record. snapshot doit contenir au minimum
        les indicateurs/confs/régime au moment de l'entrée pour B (corrélation)."""
        trade_id = f"{symbol.upper()}-{int(time.time() * 1000)}"
        rec = {
            "id": trade_id,
            "symbol": symbol.upper(),
            "side": str(side).lower(),
            "entry_ts": time.time(),
            "entry_px": float(entry_px),
            "snapshot": snapshot or {},
            # Champs remplis au close :
            "exit_ts": None,
            "exit_px": None,
            "cause": None,
            "pnl_pct": None,
            "pnl_usdt": None,
            "duration_sec": None,
            "qty": float(snapshot.get("qty", 0) or 0) if snapshot else 0,
            "leverage": float(snapshot.get("leverage", 1) or 1) if snapshot else 1,
        }
        with self._lock:
            self._data["trades"].append(rec)
            self._save()
        return trade_id

    def record_exit(self, trade_id: str, exit_px: float, cause: str,
                    pnl_usdt: Optional[float] = None) -> None:
        """Ferme un trade record avec la cause. Si pnl_usdt non fourni il sera
        estimé à partir de qty × delta prix."""
        if cause not in self.CAUSES:
            logger.warning("ScalpMemory: cause inconnue %s — saved as is", cause)
        with self._lock:
            for rec in reversed(self._data["trades"]):
                if rec["id"] == trade_id and rec.get("exit_ts") is None:
                    rec["exit_ts"] = time.time()
                    rec["exit_px"] = float(exit_px)
                    rec["cause"] = cause
                    rec["duration_sec"] = rec["exit_ts"] - rec.get("entry_ts", rec["exit_ts"])
                    # Calcul PnL
                    side = rec.get("side", "buy")
                    entry = float(rec.get("entry_px", exit_px))
                    if entry > 0:
                        if side == "buy":
                            rec["pnl_pct"] = (exit_px - entry) / entry
                        else:
                            rec["pnl_pct"] = (entry - exit_px) / entry
                        if pnl_usdt is None:
                            qty = float(rec.get("qty", 0) or 0)
                            pnl_usdt = abs(qty) * (exit_px - entry) * (1 if side == "buy" else -1)
                        rec["pnl_usdt"] = pnl_usdt
                    self._save()
                    return
            logger.warning("ScalpMemory.record_exit: trade_id %s introuvable", trade_id)

    def find_open_trade_id(self, symbol: str) -> Optional[str]:
        """Cherche le dernier trade ouvert non-fermé pour ce symbole.
        Utile pour le close si on n'a pas le trade_id sous la main."""
        with self._lock:
            for rec in reversed(self._data["trades"]):
                if rec["symbol"] == symbol.upper() and rec.get("exit_ts") is None:
                    return rec["id"]
        return None

    def recent_trades(self, symbol: Optional[str] = None, n: int = 10,
                      only_closed: bool = True) -> List[Dict]:
        """Retourne les N derniers trades (fermés par défaut)."""
        with self._lock:
            data = list(self._data["trades"])
        if symbol:
            data = [t for t in data if t["symbol"] == symbol.upper()]
        if only_closed:
            data = [t for t in data if t.get("exit_ts")]
        return data[-n:]

    def stats_by_cause(self, symbol: Optional[str] = None,
                        hours: float = 24) -> Dict:
        """Compteurs et PnL agrégés par cause de fermeture."""
        cutoff = time.time() - hours * 3600
        with self._lock:
            data = [t for t in self._data["trades"]
                    if t.get("exit_ts") and t["exit_ts"] >= cutoff]
        if symbol:
            data = [t for t in data if t["symbol"] == symbol.upper()]
        out: Dict[str, Dict] = {}
        for t in data:
            c = t.get("cause", "unknown")
            d = out.setdefault(c, {"n": 0, "pnl_usdt": 0.0, "wins": 0, "losses": 0})
            d["n"] += 1
            pnl = float(t.get("pnl_usdt", 0) or 0)
            d["pnl_usdt"] += pnl
            if pnl > 0:
                d["wins"] += 1
            elif pnl < 0:
                d["losses"] += 1
        return out

    def stats_summary(self, symbol: Optional[str] = None,
                      n: int = 20) -> Dict:
        """Résumé pour injection LLM (Phase C). Format compact, max 5 lignes."""
        recent = self.recent_trades(symbol=symbol, n=n)
        if not recent:
            return {"n": 0, "summary": "Aucun trade récent enregistré."}
        wins = sum(1 for t in recent if (t.get("pnl_pct") or 0) > 0)
        losses = sum(1 for t in recent if (t.get("pnl_pct") or 0) < 0)
        wr = wins / len(recent) if recent else 0
        avg_pnl = sum(float(t.get("pnl_pct", 0) or 0) for t in recent) / len(recent)
        causes_count: Dict[str, int] = {}
        for t in recent:
            c = t.get("cause", "unknown")
            causes_count[c] = causes_count.get(c, 0) + 1
        causes_str = ", ".join(f"{c}={n}" for c, n in sorted(causes_count.items(), key=lambda x: -x[1]))
        return {
            "n": len(recent),
            "wins": wins,
            "losses": losses,
            "winrate": wr,
            "avg_pnl_pct": avg_pnl,
            "causes": causes_count,
            "summary": f"n={len(recent)} WR={wr*100:.0f}% avgPnL={avg_pnl*100:+.2f}% | {causes_str}",
        }


# Instance globale partagée
_GLOBAL_MEMORY: Optional[ScalpMemory] = None


def get_scalp_memory() -> ScalpMemory:
    """Singleton partagé entre les modules."""
    global _GLOBAL_MEMORY
    if _GLOBAL_MEMORY is None:
        _GLOBAL_MEMORY = ScalpMemory()
    return _GLOBAL_MEMORY

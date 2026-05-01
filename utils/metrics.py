"""
Métriques V5 — suivi par trade + global.
Thread-safe. Persisté dans memory/metrics_v5.json.
"""
from __future__ import annotations
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from config.settings import METRICS_FILE

logger = logging.getLogger("sdm.metrics")


class MetricsV5:
    _lock = threading.Lock()

    def __init__(self, filepath: str = METRICS_FILE):
        self._path = Path(filepath)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self) -> Dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text())
            except Exception:
                pass
        return {
            "trades": [],                # liste complète des trades
            "daily": {},                 # résumé par date YYYY-MM-DD
            "summary": {
                "total": 0, "wins": 0, "losses": 0,
                "total_pnl": 0.0,
                "hit_1pct": 0,           # trades qui ont atteint +1%
                "trail_extended": 0,     # trades où trailing > +1%
                "avg_duration_min": 0.0,
                "profit_factor": 0.0,
                "winrate": 0.0,
            }
        }

    def _save(self):
        self._path.write_text(json.dumps(self._data, indent=2, default=str))

    # ─────────────────────────────────────────────────────────
    def record_trade(
        self,
        symbol: str,
        side: str,
        entry: float,
        exit_price: float,
        sl_initial: float,
        max_favorable: float,   # MFE : max prix favorable pendant le trade
        duration_min: float,
        hit_target: bool,       # a-t-on atteint +1% avant sortie ?
        trailing_gain: Optional[float] = None,  # gain additionnel après trailing
    ):
        pnl_pct = (
            (exit_price - entry) / entry * 100 if side == "buy"
            else (entry - exit_price) / entry * 100
        )
        mae = abs(sl_initial - entry) / entry * 100  # drawdown max potentiel

        trade = {
            "symbol": symbol,
            "side": side,
            "entry": entry,
            "exit": exit_price,
            "pnl_pct": round(pnl_pct, 4),
            "mae_pct": round(mae, 4),
            "mfe_pct": round(abs(max_favorable - entry) / entry * 100, 4),
            "duration_min": round(duration_min, 1),
            "hit_target_1pct": hit_target,
            "trailing_gain_pct": round(trailing_gain, 4) if trailing_gain else None,
            "ts": datetime.now().isoformat(),
        }

        with self._lock:
            self._data["trades"].append(trade)
            self._data["trades"] = self._data["trades"][-1000:]
            self._update_summary(trade)
            self._update_daily(trade)
            self._save()

        outcome = "WIN" if pnl_pct > 0 else "LOSS"
        logger.info(
            "📊 TRADE %s %s %s | PnL: %+.2f%% | dur: %.0fmin | hit1%%: %s",
            outcome, side.upper(), symbol, pnl_pct, duration_min, hit_target
        )

    def _update_summary(self, t: Dict):
        s = self._data["summary"]
        s["total"] += 1
        if t["pnl_pct"] > 0:
            s["wins"] += 1
        else:
            s["losses"] += 1
        s["total_pnl"] = round(s["total_pnl"] + t["pnl_pct"], 4)
        if t["hit_target_1pct"]:
            s["hit_1pct"] += 1
        if t["trailing_gain_pct"] and t["trailing_gain_pct"] > 0:
            s["trail_extended"] += 1

        # Mise à jour glissante durée moyenne
        all_t = self._data["trades"]
        if all_t:
            s["avg_duration_min"] = round(
                sum(x["duration_min"] for x in all_t) / len(all_t), 1
            )

        # Profit factor
        gross_win  = sum(t["pnl_pct"] for t in all_t if t["pnl_pct"] > 0) or 0.001
        gross_loss = abs(sum(t["pnl_pct"] for t in all_t if t["pnl_pct"] < 0)) or 0.001
        s["profit_factor"] = round(gross_win / gross_loss, 3)
        s["winrate"] = round(s["wins"] / s["total"] * 100, 1) if s["total"] > 0 else 0.0

    def _update_daily(self, t: Dict):
        day = t["ts"][:10]
        if day not in self._data["daily"]:
            self._data["daily"][day] = {"trades": 0, "wins": 0, "pnl": 0.0}
        d = self._data["daily"][day]
        d["trades"] += 1
        if t["pnl_pct"] > 0:
            d["wins"] += 1
        d["pnl"] = round(d["pnl"] + t["pnl_pct"], 4)

    # ─────────────────────────────────────────────────────────
    def get_summary(self) -> Dict:
        with self._lock:
            return dict(self._data["summary"])

    def get_daily_pnl(self, date: Optional[str] = None) -> float:
        day = date or datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            return self._data["daily"].get(day, {}).get("pnl", 0.0)

    def get_recent_trades(self, n: int = 20) -> List[Dict]:
        with self._lock:
            return self._data["trades"][-n:]

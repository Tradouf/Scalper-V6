"""
Mémoire partagée entre tous les agents.
Chaque agent peut lire ET écrire — c'est leur canal de communication.

Base canonique conservée:
    {
        "market_analysis":   { symbol: {technical, sentiment, news, whales} },
        "debate":            { symbol: {bull_args, bear_args, decision} },
        "positions":         { symbol: {side, entry, sl, tp, pnl} },
        "risk_status":       { var, drawdown, daily_pnl, alerts[] },
        "agent_signals":     [ {agent, symbol, action, confidence, reason, ts} ],
        "trade_history":     [ {symbol, side, entry, exit, pnl, ts} ],
        "regime":            { trend: bull/bear/range, volatility: low/med/high },
        "agent_messages":    [ {from, to, content, ts} ],
        "scalper_profiles":  { symbol: { regime_key: {sl_atr_mult, tp_atr_mult, trailing_atr_mult, ...} } }
    }

Extensions minimales pour V5.8.4 / engines ajoutés aujourd'hui:
    - advanced_features   { symbol: payload features déterministes }
    - symbol_regimes      { symbol: régime local enrichi }
    - orderbook_snapshots { symbol: snapshot orderbook enrichi }
    - validation_logs     [ {stage, symbol, level, payload, ts} ]

Le fichier expose aussi des alias de compatibilité pour les modules utilisant
les noms sans underscore (getregime, updateanalysis, etc.).
"""

from __future__ import annotations

import json
import logging
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("sdm.memory")


class SharedMemory:
    """
    Mémoire commune à tous les agents.
    Thread-safe. Persistée sur disque à chaque écriture.
    """

    _lock = threading.Lock()

    def __init__(self, filepath: str = "memory/shared_memory.json"):
        self._path = Path(filepath)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, Any] = self._load()
        self._normalize_schema()

    def _default_data(self) -> Dict[str, Any]:
        return {
            "market_analysis": {},
            "debate": {},
            "positions": {},
            "risk_status": {
                "var": 0.0,
                "drawdown": 0.0,
                "daily_pnl": 0.0,
                "alerts": [],
            },
            "agent_signals": [],
            "trade_history": [],
            "regime": {
                "trend": "unknown",
                "volatility": "medium",
                "risk": "medium",
                "ts": datetime.now().isoformat(),
            },
            "agent_messages": [],
            "scalper_profiles": {},
            "advanced_features": {},
            "symbol_regimes": {},
            "orderbook_snapshots": {},
            "validation_logs": [],
        }

    def _load(self) -> Dict[str, Any]:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("SharedMemory load error: %r", e)
        return self._default_data()

    def _save(self):
        """Écriture atomique : on écrit dans un fichier temporaire puis on renomme."""
        tmp_path = self._path.with_suffix(".json.tmp")
        try:
            tmp_path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            tmp_path.replace(self._path)
        except Exception as e:
            logger.warning("SharedMemory _save atomique échoué: %r", e)
            # Fallback : écriture directe (mieux que rien)
            try:
                self._path.write_text(
                    json.dumps(self._data, indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8",
                )
            except Exception:
                pass

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat()

    @staticmethod
    def _copy(obj: Any) -> Any:
        try:
            return deepcopy(obj)
        except Exception:
            return obj

    def _normalize_schema(self) -> None:
        defaults = self._default_data()
        for key, value in defaults.items():
            if key not in self._data or not isinstance(self._data.get(key), type(value)):
                self._data[key] = deepcopy(value)

        self._data["risk_status"].setdefault("alerts", [])
        self._data["regime"].setdefault("trend", "unknown")
        self._data["regime"].setdefault("volatility", "medium")
        self._data["regime"].setdefault("risk", "medium")
        self._data["regime"].setdefault("ts", self._now())

    # ── Generic KV ───────────────────────────────────

    def get(self, key: str, default=None) -> Any:
        with self._lock:
            return self._copy(self._data.get(key, default))

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._save()

    def set_value(self, key: str, value: Any) -> None:
        self.set(key, value)

    def setvalue(self, key: str, value: Any) -> None:
        self.set(key, value)

    def update_meta(self, key: str, value: Any) -> None:
        self.set(key, value)

    def updatemeta(self, key: str, value: Any) -> None:
        self.set(key, value)

    # ── Lecture ──────────────────────────────────────

    def get_analysis(self, symbol: str) -> Dict:
        with self._lock:
            return self._copy(self._data["market_analysis"].get(symbol, {}))

    def getanalysis(self, symbol: str) -> Dict:
        return self.get_analysis(symbol)

    def get_debate(self, symbol: str) -> Dict:
        with self._lock:
            return self._copy(self._data["debate"].get(symbol, {}))

    def getdebate(self, symbol: str) -> Dict:
        return self.get_debate(symbol)

    def get_positions(self) -> Dict:
        with self._lock:
            return self._copy(self._data["positions"])

    def getpositions(self) -> Dict:
        return self.get_positions()

    def get_regime(self) -> Dict:
        with self._lock:
            return self._copy(self._data["regime"])

    def getregime(self) -> Dict:
        return self.get_regime()

    def get_risk_status(self) -> Dict:
        with self._lock:
            return self._copy(self._data["risk_status"])

    def getriskstatus(self) -> Dict:
        return self.get_risk_status()

    def get_recent_trades(self, n: int = 10) -> List[Dict]:
        with self._lock:
            return self._copy(self._data["trade_history"][-max(1, int(n)):])

    def getrecenttrades(self, n: int = 10) -> List[Dict]:
        return self.get_recent_trades(n)

    def get_trade_history(self, start: Optional[int] = None) -> List[Dict]:
        with self._lock:
            trades = self._data["trade_history"]
            if start is None:
                return self._copy(trades)
            return self._copy(trades[int(start):])

    def gettradehistory(self, start: Optional[int] = None) -> List[Dict]:
        return self.get_trade_history(start)

    def get_messages_for(self, agent: str) -> List[Dict]:
        with self._lock:
            return self._copy(
                [m for m in self._data["agent_messages"] if m.get("to") in (agent, "all")]
            )

    def getmessagesfor(self, agent: str) -> List[Dict]:
        return self.get_messages_for(agent)

    def get_scalper_profile(self, symbol: str, regime_key: str) -> Dict:
        with self._lock:
            return self._copy(
                self._data["scalper_profiles"].get(symbol, {}).get(regime_key, {})
            )

    def getscalperprofile(self, symbol: str, regimekey: str) -> Dict:
        return self.get_scalper_profile(symbol, regimekey)

    def get_advanced_features(self, symbol: str) -> Dict:
        with self._lock:
            return self._copy(self._data["advanced_features"].get(symbol, {}))

    def getadvancedfeatures(self, symbol: str) -> Dict:
        return self.get_advanced_features(symbol)

    def get_all_advanced_features(self) -> Dict[str, Dict]:
        with self._lock:
            return self._copy(self._data["advanced_features"])

    def getalladvancedfeatures(self) -> Dict[str, Dict]:
        return self.get_all_advanced_features()

    def get_regime_for_symbol(self, symbol: str) -> Dict:
        with self._lock:
            local = self._data["symbol_regimes"].get(symbol)
            return self._copy(local if local else self._data["regime"])

    def getregimeforsymbol(self, symbol: str) -> Dict:
        return self.get_regime_for_symbol(symbol)

    def get_symbol_regimes(self) -> Dict[str, Dict]:
        with self._lock:
            return self._copy(self._data["symbol_regimes"])

    def getsymbolregimes(self) -> Dict[str, Dict]:
        return self.get_symbol_regimes()

    def get_orderbook_snapshot(self, symbol: str) -> Dict:
        with self._lock:
            return self._copy(self._data["orderbook_snapshots"].get(symbol, {}))

    def getorderbooksnapshot(self, symbol: str) -> Dict:
        return self.get_orderbook_snapshot(symbol)

    def get_validation_logs(
        self,
        symbol: Optional[str] = None,
        stage: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict]:
        with self._lock:
            logs = self._data["validation_logs"]
            if symbol is not None:
                logs = [x for x in logs if x.get("symbol") == symbol]
            if stage is not None:
                logs = [x for x in logs if x.get("stage") == stage]
            return self._copy(logs[-max(1, int(limit)):])

    def getvalidationlogs(
        self,
        symbol: Optional[str] = None,
        stage: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict]:
        return self.get_validation_logs(symbol=symbol, stage=stage, limit=limit)

    # ── Écriture ─────────────────────────────────────

    def update_analysis(self, symbol: str, agent: str, data: Dict) -> None:
        with self._lock:
            self._data["market_analysis"].setdefault(symbol, {})
            self._data["market_analysis"][symbol][agent] = {
                **(data or {}),
                "ts": self._now(),
            }
            self._save()

    def updateanalysis(self, symbol: str, agent: str, data: Dict) -> None:
        self.update_analysis(symbol, agent, data)

    def update_debate(self, symbol: str, role: str, content: Dict) -> None:
        with self._lock:
            self._data["debate"].setdefault(symbol, {})
            self._data["debate"][symbol][role] = {
                **(content or {}),
                "ts": self._now(),
            }
            self._save()

    def updatedebate(self, symbol: str, role: str, content: Dict) -> None:
        self.update_debate(symbol, role, content)

    def update_position(self, symbol: str, data: Optional[Dict] = None, **kwargs) -> None:
        with self._lock:
            if data is None and kwargs:
                data = kwargs
            if data is None:
                self._data["positions"].pop(symbol, None)
            else:
                self._data["positions"][symbol] = {
                    **data,
                    "ts": self._now(),
                }
            self._save()

    def updateposition(self, symbol: str, data: Optional[Dict] = None, **kwargs) -> None:
        self.update_position(symbol, data, **kwargs)

    def update_regime(self, trend: str, volatility: str, **extra) -> None:
        with self._lock:
            self._data["regime"] = {
                "trend": trend,
                "volatility": volatility,
                "ts": self._now(),
                **extra,
            }
            self._save()

    def updateregime(self, trend: str, volatility: str, **extra) -> None:
        self.update_regime(trend, volatility, **extra)

    def update_risk(self, **kwargs) -> None:
        with self._lock:
            self._data["risk_status"].update(kwargs)
            self._data["risk_status"]["ts"] = self._now()
            self._save()

    def updaterisk(self, **kwargs) -> None:
        self.update_risk(**kwargs)

    def update_risk_daily_pnl(self, value: float) -> None:
        self.update_risk(daily_pnl=float(value))

    def updateriskdailypnl(self, value: float) -> None:
        self.update_risk(daily_pnl=float(value))

    def add_alert(self, message: str) -> None:
        with self._lock:
            self._data["risk_status"].setdefault("alerts", []).append(
                {"msg": message, "ts": self._now()}
            )
            self._data["risk_status"]["alerts"] = self._data["risk_status"]["alerts"][-500:]
            self._save()
        logger.warning("⚠️ ALERTE RISK: %s", message)

    def addalert(self, message: str) -> None:
        self.add_alert(message)

    def add_signal(
        self,
        agent: str,
        symbol: str,
        action: str,
        confidence: float,
        reason: str,
    ) -> None:
        with self._lock:
            self._data["agent_signals"].append(
                {
                    "agent": agent,
                    "symbol": symbol,
                    "action": action,
                    "confidence": float(confidence),
                    "reason": reason,
                    "ts": self._now(),
                }
            )
            self._data["agent_signals"] = self._data["agent_signals"][-2000:]
            self._save()

    def addsignal(
        self,
        agent: str,
        symbol: str,
        action: str,
        confidence: float,
        reason: str,
    ) -> None:
        self.add_signal(agent, symbol, action, confidence, reason)

    def add_trade(self, trade: Dict) -> None:
        with self._lock:
            payload = dict(trade or {})
            payload.setdefault("ts", self._now())
            self._data["trade_history"].append(payload)
            self._data["trade_history"] = self._data["trade_history"][-5000:]
            self._save()

    def addtrade(self, trade: Dict) -> None:
        self.add_trade(trade)

    def add_message(self, from_agent: str, to_agent: str, content: str) -> None:
        with self._lock:
            self._data["agent_messages"].append(
                {
                    "from": from_agent,
                    "to": to_agent,
                    "content": content,
                    "ts": self._now(),
                }
            )
            self._data["agent_messages"] = self._data["agent_messages"][-2000:]
            self._save()

    def addmessage(self, from_agent: str, to_agent: str, content: str) -> None:
        self.add_message(from_agent, to_agent, content)

    def send_message(self, from_agent: str, to_agent: str, content: str) -> None:
        self.add_message(from_agent, to_agent, content)

    def sendmessage(self, from_agent: str, to_agent: str, content: str) -> None:
        self.add_message(from_agent, to_agent, content)

    def update_scalper_profile(self, symbol: str, regime_key: str, profile: Dict) -> None:
        with self._lock:
            self._data["scalper_profiles"].setdefault(symbol, {})
            self._data["scalper_profiles"][symbol][regime_key] = {
                **(profile or {}),
                "ts": self._now(),
            }
            self._save()

    def updatescalperprofile(self, symbol: str, regimekey: str, profile: Dict) -> None:
        self.update_scalper_profile(symbol, regimekey, profile)

    # ── Extensions minimales pour FeatureEngine / RegimeEngine ──

    def update_advanced_features(self, symbol: str, features: Dict) -> None:
        with self._lock:
            self._data["advanced_features"][symbol] = {
                **(features or {}),
                "symbol": symbol,
                "ts": self._now(),
            }
            self._save()

    def updateadvancedfeatures(self, symbol: str, features: Dict) -> None:
        self.update_advanced_features(symbol, features)

    def set_features(self, symbol: str, features: Dict) -> None:
        self.update_advanced_features(symbol, features)

    def setfeatures(self, symbol: str, features: Dict) -> None:
        self.update_advanced_features(symbol, features)

    def update_regime_for_symbol(self, symbol: str, regime: Dict) -> None:
        with self._lock:
            self._data["symbol_regimes"][symbol] = {
                **(regime or {}),
                "symbol": symbol,
                "ts": self._now(),
            }
            self._save()

    def updateregimeforsymbol(self, symbol: str, regime: Dict) -> None:
        self.update_regime_for_symbol(symbol, regime)

    def update_orderbook_snapshot(self, symbol: str, snapshot: Dict) -> None:
        with self._lock:
            self._data["orderbook_snapshots"][symbol] = {
                **(snapshot or {}),
                "symbol": symbol,
                "ts": self._now(),
            }
            self._save()

    def updateorderbooksnapshot(self, symbol: str, snapshot: Dict) -> None:
        self.update_orderbook_snapshot(symbol, snapshot)

    def add_validation_log(
        self,
        stage: str,
        symbol: str,
        payload: Dict,
        level: str = "INFO",
    ) -> None:
        with self._lock:
            self._data["validation_logs"].append(
                {
                    "stage": stage,
                    "symbol": symbol,
                    "level": str(level).upper(),
                    "payload": payload or {},
                    "ts": self._now(),
                }
            )
            self._data["validation_logs"] = self._data["validation_logs"][-5000:]
            self._save()

    def addvalidationlog(
        self,
        stage: str,
        symbol: str,
        payload: Dict,
        level: str = "INFO",
    ) -> None:
        self.add_validation_log(stage, symbol, payload, level)

    # ── Statistiques / debug ─────────────────────────

    def get_trades(self, limit: int = 100) -> List[Dict]:
        return self.get_recent_trades(limit)

    def gettrades(self, limit: int = 100) -> List[Dict]:
        return self.get_recent_trades(limit)

    def get_stats(self) -> Dict:
        with self._lock:
            return {
                "positions": len(self._data["positions"]),
                "trades": len(self._data["trade_history"]),
                "alerts": len(self._data["risk_status"].get("alerts", [])),
                "signals": len(self._data["agent_signals"]),
                "messages": len(self._data["agent_messages"]),
                "features": len(self._data["advanced_features"]),
                "symbol_regimes": len(self._data["symbol_regimes"]),
                "validation_logs": len(self._data["validation_logs"]),
            }

    def getstats(self) -> Dict:
        return self.get_stats()

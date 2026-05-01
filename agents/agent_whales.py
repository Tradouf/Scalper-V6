"""
AgentWhales — surveille Whales Alert et interprète les gros mouvements.
Alimente la mémoire partagée avec le sentiment whale.
"""
from __future__ import annotations
import logging
import os
import requests
from typing import Dict, List
from .base_agent import BaseAgent
from memory.shared_memory import SharedMemory
from config.settings import WHALES_API_KEY, WHALES_MIN_USD

logger = logging.getLogger("sdm.whales")

class AgentWhales(BaseAgent):

    WHALES_URL = "https://api.whale-alert.io/v1/transactions"

    def __init__(self, memory: SharedMemory):
        super().__init__("whales", memory)
        self._api_key = os.getenv("WHALES_API_KEY", WHALES_API_KEY)

    def analyze(self, symbols: List[str]) -> Dict:
        """
        Récupère les dernières transactions whale et demande au LLM
        d'interpréter l'impact sur les symboles analysés.
        """
        txs = self._fetch_whales()
        if not txs:
            self.logger.warning("Aucune transaction whale récupérée")
            return {}

        # Filtre les transactions pertinentes
        relevant = self._filter_relevant(txs, symbols)
        summary  = self._summarize(relevant)

        if not relevant:
            self.memory.update_analysis("MARKET", "whales", {
                "sentiment": "neutral", "signals": [], "raw_count": len(txs)
            })
            return {"sentiment": "neutral", "signals": []}

        # LLM interprète les mouvements
        system = (
            "Tu es expert en analyse on-chain et mouvements de whales crypto. "
            "Tu analyses des grosses transactions blockchain pour détecter des signaux de trading. "
            "Réponds UNIQUEMENT en JSON."
        )
        user = (
            f"Voici les dernières transactions whale (>{WHALES_MIN_USD/1e6:.0f}M$):\n{summary}\n\n"
            f"Symboles analysés: {', '.join(symbols)}\n\n"
            "Analyse l'impact probable sur les prix. JSON:\n"
            '{"sentiment":"bullish"|"bearish"|"neutral",'
            '"signals":[{"symbol":"BTC","impact":"bullish","reason":"...","confidence":0.7}],'
            '"summary":"résumé court"}'
        )

        result = self._parse_json(self._llm(system, user, temperature=0.1, max_tokens=400))
        if not result:
            result = {"sentiment": "neutral", "signals": [], "summary": "Parsing error"}

        result["raw_count"]    = len(txs)
        result["relevant_count"] = len(relevant)

        # Stocke en mémoire partagée
        self.memory.update_analysis("MARKET", "whales", result)

        # Envoie les signaux forts au trader
        for sig in result.get("signals", []):
            if sig.get("confidence", 0) >= 0.7:
                self._send_message("trader",
                    f"WHALE SIGNAL {sig['symbol']}: {sig['impact'].upper()} — {sig['reason']}")
                self.memory.add_signal(
                    "whales", sig["symbol"], sig["impact"],
                    sig.get("confidence", 0.7), sig.get("reason", ""))

        self.logger.info("Whales: %d txs, %d pertinentes, sentiment=%s",
                        len(txs), len(relevant), result.get("sentiment"))
        return result

    def _fetch_whales(self) -> List[Dict]:
        import time
        for attempt in range(3):
            try:
                params = {
                    "api_key":   self._api_key,
                    "min_value": WHALES_MIN_USD,
                    "limit":     100,
                }
                resp = requests.get(self.WHALES_URL, params=params, timeout=15)
                if resp.status_code == 500:
                    self.logger.warning("Whale Alert serveur KO (500), tentative %d/3", attempt+1)
                    time.sleep(5 * (attempt + 1))
                    continue
                resp.raise_for_status()
                data = resp.json()
                txs = data.get("transactions", [])
                if not txs:
                    self.logger.debug("Whale Alert: 0 transactions (normal)")
                return txs
            except requests.exceptions.HTTPError as e:
                self.logger.warning("Whale Alert HTTP erreur: %s", e)
                return []
            except Exception as e:
                self.logger.warning("Whale Alert indisponible: %s — cycle continue sans whales", e)
                return []
        self.logger.warning("Whale Alert inaccessible après 3 tentatives — cycle continue sans whales")
        return []

    def _filter_relevant(self, txs: List[Dict], symbols: List[str]) -> List[Dict]:
        syms_lower = [s.lower() for s in symbols]
        return [
            t for t in txs
            if t.get("symbol", "").lower() in syms_lower
            or t.get("blockchain", "").lower() in syms_lower
        ]

    def _summarize(self, txs: List[Dict]) -> str:
        lines = []
        for t in txs[:20]:
            amount_usd = t.get("amount_usd", 0)
            symbol     = t.get("symbol", "?").upper()
            from_      = t.get("from", {}).get("owner_type", "unknown")
            to_        = t.get("to",   {}).get("owner_type", "unknown")
            tx_type    = t.get("transaction_type", "transfer")
            lines.append(f"  {symbol} ${amount_usd/1e6:.1f}M {from_}→{to_} ({tx_type})")
        return "\n".join(lines) or "Aucune transaction"

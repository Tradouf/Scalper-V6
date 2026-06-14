"""
OnChainFeed — flux whales (Whale Alert) pour enrichir le contexte stratège (2026-06-14).

Port de l'AgentWhales V6, sans LLM propre : COLLECTE + résume. Le stratège Opus
interprète. Clé : WHALES_API_KEY (.env).

Signal-clé pour le trading : les FLUX EXCHANGE.
  - transfert VERS un exchange  → potentiel d'OFFRE (pression vendeuse)
  - transfert DEPUIS un exchange → retrait/HODL (pression réduite)
On résume net flux + gros transferts sur les actifs tradés.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("v7.onchain")

WHALES_URL = "https://api.whale-alert.io/v1/transactions"
REFRESH_SEC = 900          # 15 min
MIN_USD = 5_000_000        # transferts >= $5M
WINDOW_SEC = 3600          # fenêtre 1h


def _load_key() -> str:
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("WHALES_API_KEY="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("WHALES_API_KEY", "")


class OnChainFeed:
    def __init__(self) -> None:
        self._key = _load_key()
        self._summary = ""
        self._ts = 0.0

    def summary(self) -> str:
        now = time.time()
        if self._summary and now - self._ts < REFRESH_SEC:
            return self._summary
        if not self._key:
            return "(WHALES_API_KEY absente)"
        try:
            self._summary = self._build()
            self._ts = now
        except Exception as e:
            logger.warning("OnChainFeed build: %r — garde cache", e)
        return self._summary

    def _build(self) -> str:
        params = {"api_key": self._key, "min_value": MIN_USD,
                  "start": int(time.time()) - WINDOW_SEC}
        r = requests.get(WHALES_URL, params=params, timeout=15)
        r.raise_for_status()
        txs = r.json().get("transactions", [])
        if not txs:
            return "(aucune transaction whale >$5M sur 1h)"

        to_exch = from_exch = 0.0       # flux exchange en $ (pression)
        notable: list[str] = []
        for t in txs:
            amt = float(t.get("amount_usd", 0) or 0)
            sym = str(t.get("symbol", "")).upper()
            frm = (t.get("from", {}) or {}).get("owner_type", "")
            to = (t.get("to", {}) or {}).get("owner_type", "")
            if to == "exchange" and frm != "exchange":
                to_exch += amt
            elif frm == "exchange" and to != "exchange":
                from_exch += amt
            if amt >= 20_000_000:        # transferts énormes : on les liste
                direction = ("→exch" if to == "exchange" else "exch→" if frm == "exchange" else "wallet")
                notable.append(f"{sym} ${amt/1e6:.0f}M {direction}")

        net = to_exch - from_exch
        pressure = ("VENDEUSE" if net > 0 else "ACHETEUSE" if net < 0 else "neutre")
        lines = [
            f"Flux exchange 1h (>{MIN_USD/1e6:.0f}M) : entrées ${to_exch/1e6:.0f}M, "
            f"sorties ${from_exch/1e6:.0f}M → net ${net/1e6:+.0f}M (pression {pressure})",
        ]
        if notable:
            lines.append("Gros mouvements : " + " | ".join(notable[:6]))
        return "\n".join(lines)

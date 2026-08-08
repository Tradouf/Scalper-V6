"""
Ingestion d'un signal : parse le texte, capture le mid au moment de la
réception, enregistre dans le store.

CLI (mail collé dans un fichier ou sur stdin) :
    python -m grokwatch.ingest email.txt
    cat email.txt | python -m grokwatch.ingest
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from grokwatch.parser import parse_signal
from grokwatch.store import record_signal

logger = logging.getLogger("sdm.grokwatch.ingest")

HL_INFO_URL = "https://api.hyperliquid.xyz/info"


def fetch_mid(symbol: str, timeout: float = 5.0) -> Optional[float]:
    try:
        from hl_rate_limit import throttle_before_hl_request

        throttle_before_hl_request()
        resp = requests.post(
            HL_INFO_URL,
            headers={"Content-Type": "application/json"},
            json={"type": "allMids"},
            timeout=timeout,
        )
        resp.raise_for_status()
        mid = resp.json().get(symbol)
        return float(mid) if mid is not None else None
    except Exception as e:
        logger.warning("fetch_mid: %r", e)
        return None


def ingest_text(text: str, received_ts: Optional[float] = None,
                source: str = "manual") -> Optional[dict]:
    """Parse + enregistre. None si pas de signal ou doublon."""
    sig = parse_signal(text)
    if sig is None:
        logger.info("Aucun signal reconnu dans le texte (%d car.)", len(text or ""))
        return None
    ts = received_ts if received_ts is not None else time.time()
    sig["ts"] = ts
    sig["iso"] = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    sig["source"] = source
    sig["mid_at_receipt"] = fetch_mid(sig["symbol"])
    if not record_signal(sig):
        logger.info("Signal déjà enregistré (hash %s) — ignoré",
                    sig["content_hash"][:10])
        return None
    logger.info("Signal enregistré: %s %s @ mid=%s (source=%s)",
                sig["direction"], sig["symbol"], sig["mid_at_receipt"], source)
    return sig


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    if len(sys.argv) > 1:
        with open(sys.argv[1], "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    else:
        text = sys.stdin.read()
    sig = ingest_text(text)
    if sig is None:
        print("Rien d'enregistré (pas de signal, ou doublon).")
        return 1
    print(f"OK: {sig['direction']} {sig['symbol']} @ mid={sig['mid_at_receipt']} "
          f"({sig['iso']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

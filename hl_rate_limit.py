"""
Limiteur de débit global Hyperliquid — partagé entre processus (fcntl).

SimpleBot, SuperBot, MinuteLab et les autres clients /info frappent le même
endpoint public. Sans coordination inter-processus, les optimiseurs parallèles
provoquent des rafales 429 (observé juillet 2026).

Usage :
    from hl_rate_limit import throttle_before_hl_request
    throttle_before_hl_request()
    resp = requests.post(HL_INFO_URL, ...)
"""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path

_DEFAULT_STATE = Path(__file__).resolve().parent / "state" / "hl_rate_limit.lock"


def _state_path() -> Path:
    raw = os.environ.get("HL_RATE_LIMIT_STATE", "").strip()
    return Path(raw) if raw else _DEFAULT_STATE


def _interval_sec() -> float:
    try:
        return float(os.environ.get("HL_RATE_LIMIT_SEC", "0.4"))
    except (TypeError, ValueError):
        return 0.4


def throttle_before_hl_request(interval: float | None = None) -> None:
    """Attend si nécessaire pour respecter l'intervalle minimum entre requêtes /info."""
    gap = _interval_sec() if interval is None else interval
    if gap <= 0:
        return

    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            raw = f.read().strip()
            try:
                last = float(raw)
            except ValueError:
                last = 0.0
            now = time.time()
            wait = gap - (now - last)
            if wait > 0:
                time.sleep(wait)
                now = time.time()
            f.seek(0)
            f.truncate()
            f.write(f"{now:.6f}")
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
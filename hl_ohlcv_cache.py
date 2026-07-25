"""
Cache OHLCV disque partagé entre processus (SimpleBot, SuperBot, outils).

Problème visé (juillet 2026) : les optimiseurs SimpleBot et SuperBot + le
momentum paper re-téléchargent les MÊMES 40 symboles × 15m/1h/4h sur la même
IP → rafales 429 en continu (>4000 par log), fetchs perdus après 3 retries,
décisions prises sur données incomplètes.

Principe : un fichier JSON par (symbol, interval) sous state/ohlcv_cache/.
Un fetch n'est réutilisé que s'il a eu lieu APRÈS la clôture de la dernière
bougie de l'intervalle — garantit que la dernière bougie clôturée est présente
(critère indispensable aux décisions live). La fenêtre stockée grandit vers le
superset des fenêtres demandées, les demandes plus courtes sont servies par
découpage.

⚠️ La dernière bougie retournée peut être un instantané EN COURS pris au moment
du fetch (donc figé) : filtrer avec closed_candles() côté appelant — c'est déjà
la convention de tout le repo.

Écriture atomique (tmp + os.replace) → lecture sans verrou sûre.
Désactivable : HL_OHLCV_CACHE=0.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import List, Optional

_CACHE_DIR = Path(__file__).resolve().parent / "state" / "ohlcv_cache"

_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}

# candleSnapshot tronque silencieusement à ~5000 bougies : au-delà de ce
# volume, la couverture du début de fenêtre ne peut pas être exigée.
_API_MAX_CANDLES = 4_900


def _enabled() -> bool:
    return os.environ.get("HL_OHLCV_CACHE", "1") not in ("0", "false", "False")


def _dir() -> Path:
    raw = os.environ.get("HL_OHLCV_CACHE_DIR", "").strip()
    return Path(raw) if raw else _CACHE_DIR


def _path(symbol: str, interval: str) -> Path:
    safe = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in symbol)
    return _dir() / f"{safe}__{interval}.json"


def cache_get(symbol: str, interval: str, start_ms: int, end_ms: int,
              now_ms: Optional[int] = None) -> Optional[List[dict]]:
    """Bougies [start_ms, end_ms] si le cache est frais, sinon None.

    Frais = fetché après la dernière frontière de bougie (la dernière bougie
    clôturée est donc dans les données) ET couvrant le début de fenêtre
    demandé (sauf si le fetch d'origine avait atteint le cap API).
    """
    if not _enabled():
        return None
    step = _INTERVAL_MS.get(interval)
    if step is None:
        return None
    now_ms = now_ms or int(time.time() * 1000)
    # seules les fenêtres se terminant « maintenant » sont cachées
    if end_ms < now_ms - step:
        return None
    try:
        with open(_path(symbol, interval), "r", encoding="utf-8") as f:
            entry = json.load(f)
    except Exception:
        return None

    last_boundary = (now_ms // step) * step
    if entry.get("fetched_at_ms", 0) < last_boundary:
        return None                       # une bougie a clôturé depuis → refetch
    candles = entry.get("candles") or []
    if not candles:
        return None
    covers_start = candles[0]["ts"] <= start_ms + step
    api_capped = len(candles) >= _API_MAX_CANDLES
    if not covers_start and not api_capped:
        return None
    return [c for c in candles if c["ts"] >= start_ms]


def cache_put(symbol: str, interval: str, candles: List[dict],
              fetched_at_ms: Optional[int] = None) -> None:
    """Stocke un fetch. Ne rétrécit jamais la fenêtre : si le cache existant
    (même frais ou périmé) commence plus tôt, on fusionne son préfixe."""
    if not _enabled() or not candles:
        return
    step = _INTERVAL_MS.get(interval)
    if step is None:
        return
    fetched_at_ms = fetched_at_ms or int(time.time() * 1000)
    path = _path(symbol, interval)
    merged = list(candles)
    try:
        with open(path, "r", encoding="utf-8") as f:
            old = (json.load(f).get("candles") or [])
        if old and old[0]["ts"] < merged[0]["ts"]:
            merged = [c for c in old if c["ts"] < merged[0]["ts"]] + merged
    except Exception:
        pass
    try:
        _dir().mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"fetched_at_ms": fetched_at_ms,
                       "interval": interval,
                       "candles": merged}, f)
        os.replace(tmp, path)
    except Exception:
        pass                              # le cache est best-effort, jamais bloquant

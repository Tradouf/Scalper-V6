#!/usr/bin/env python3
"""
OHLC 1m collector — historise toutes les bougies 1m de tous les perps HL.

Sorties :
  data/ohlc_1m/<SYMBOL>.parquet      bougies 1m par symbole
  data/ohlc_1m/_collector.log        log rotatif

Modes :
  --backfill DAYS    bootstrap : remplit DAYS jours en arrière (défaut 30)
  --incremental      cron : fetch depuis max(ts) jusqu'à maintenant (défaut)
  --symbols A,B,C    restreint à une liste (défaut : meta.universe complet)

Idempotent : déduplique sur ts_open avant write. Append safe (read+merge+atomic write).

API HL : POST /info type=candleSnapshot, max 5000 bougies/call.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
import time
from pathlib import Path
from typing import List, Optional

import pandas as pd
import requests

REPO = Path(__file__).resolve().parent.parent
DATA_DIR = REPO / "data" / "ohlc_1m"
DATA_DIR.mkdir(parents=True, exist_ok=True)

HL_API = "https://api.hyperliquid.xyz/info"
INTERVAL = "1m"
INTERVAL_MS = 60_000
MAX_CANDLES_PER_CALL = 5000
RATE_LIMIT_SLEEP = 0.05  # 50ms entre calls, < 20 req/s
RATE_LIMIT_BACKOFF = 2.0  # 2s en cas de 429/erreur

logger = logging.getLogger("ohlc_collector")


def _setup_logging() -> None:
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            DATA_DIR / "_collector.log",
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
        ),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


def _fetch_universe() -> List[str]:
    r = requests.post(HL_API, json={"type": "meta"}, timeout=10)
    r.raise_for_status()
    universe = r.json().get("universe", [])
    return [str(u["name"]).upper() for u in universe if u.get("name")]


def _fetch_candles(coin: str, start_ms: int, end_ms: int) -> List[dict]:
    """Un appel candleSnapshot. Retourne liste de dicts {t,o,h,l,c,v}."""
    try:
        r = requests.post(
            HL_API,
            json={
                "type": "candleSnapshot",
                "req": {
                    "coin": coin, "interval": INTERVAL,
                    "startTime": start_ms, "endTime": end_ms,
                },
            },
            timeout=15,
        )
        if r.status_code == 429:
            logger.warning("%s rate-limited, backoff %.1fs", coin, RATE_LIMIT_BACKOFF)
            time.sleep(RATE_LIMIT_BACKOFF)
            return []
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.warning("%s candles error %s..%s: %r", coin, start_ms, end_ms, e)
        return []


def _candles_to_df(rows: List[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["ts_open", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame([{
        "ts_open": int(r.get("t", 0)),
        "open": float(r.get("o", 0)),
        "high": float(r.get("h", 0)),
        "low": float(r.get("l", 0)),
        "close": float(r.get("c", 0)),
        "volume": float(r.get("v", 0) or 0),
    } for r in rows])
    return df


def _load_existing(coin: str) -> pd.DataFrame:
    path = DATA_DIR / f"{coin}.parquet"
    if not path.exists():
        return pd.DataFrame(columns=["ts_open", "open", "high", "low", "close", "volume"])
    try:
        return pd.read_parquet(path)
    except Exception as e:
        logger.warning("%s read existing parquet: %r — restart from scratch", coin, e)
        return pd.DataFrame(columns=["ts_open", "open", "high", "low", "close", "volume"])


def _save_parquet(coin: str, df: pd.DataFrame) -> None:
    path = DATA_DIR / f"{coin}.parquet"
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, compression="zstd", index=False)
    tmp.replace(path)


def _fetch_range(coin: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Itère par chunks de MAX_CANDLES_PER_CALL en avançant. Retourne tout concaténé."""
    out: List[pd.DataFrame] = []
    chunk_ms = MAX_CANDLES_PER_CALL * INTERVAL_MS
    cur = start_ms
    while cur < end_ms:
        chunk_end = min(cur + chunk_ms, end_ms)
        rows = _fetch_candles(coin, cur, chunk_end)
        if rows:
            out.append(_candles_to_df(rows))
            # Avance en se basant sur le dernier ts retourné (HL renvoie parfois moins
            # de 5000 même si la fenêtre couvre plus, ne pas dépendre de la longueur)
            last_ts = max(int(r.get("t", cur)) for r in rows)
            cur = max(last_ts + INTERVAL_MS, cur + chunk_ms)
        else:
            cur = chunk_end
        time.sleep(RATE_LIMIT_SLEEP)
    if not out:
        return _candles_to_df([])
    return pd.concat(out, ignore_index=True)


def _collect_symbol(coin: str, mode: str, backfill_days: int) -> tuple[int, int]:
    """Returns (new_bars, total_bars_after)."""
    existing = _load_existing(coin)
    now_ms = int(time.time() * 1000)
    if mode == "backfill" or existing.empty:
        # Fetch DAYS jours en arrière (ou complète si existe déjà partiellement)
        start_ms = now_ms - backfill_days * 24 * 3600 * 1000
        if not existing.empty:
            # Backfill comble seulement la zone manquante avant le min existant
            existing_min = int(existing["ts_open"].min())
            if start_ms >= existing_min:
                # Rien à backfill, l'existant couvre déjà cette fenêtre côté ancien
                start_ms = int(existing["ts_open"].max()) + INTERVAL_MS
    else:
        # Incremental : depuis dernière bougie + 1m
        start_ms = int(existing["ts_open"].max()) + INTERVAL_MS

    if start_ms >= now_ms:
        return 0, len(existing)

    new_df = _fetch_range(coin, start_ms, now_ms)
    if new_df.empty:
        return 0, len(existing)

    merged = pd.concat([existing, new_df], ignore_index=True)
    merged = merged.drop_duplicates(subset=["ts_open"], keep="last")
    merged = merged.sort_values("ts_open").reset_index(drop=True)
    new_count = len(merged) - len(existing)
    if new_count > 0:
        _save_parquet(coin, merged)
    return max(0, new_count), len(merged)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=0,
                    help="Bootstrap : DAYS jours en arrière (défaut 0 = incrémental)")
    ap.add_argument("--incremental", action="store_true",
                    help="Force le mode incrémental (depuis last_ts en parquet)")
    ap.add_argument("--symbols", type=str, default="",
                    help="Liste CSV de symboles (défaut : meta.universe complet)")
    args = ap.parse_args()

    _setup_logging()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        try:
            symbols = _fetch_universe()
        except Exception as e:
            logger.error("meta.universe fetch error: %r — abort", e)
            return 2

    mode = "backfill" if args.backfill > 0 else "incremental"
    days = args.backfill if args.backfill > 0 else 1  # incrémental : 1j de fallback si parquet vide
    logger.info("OHLC collector start mode=%s days=%d symbols=%d",
                mode, days, len(symbols))

    t0 = time.time()
    total_new = 0
    errors = 0
    for i, coin in enumerate(symbols, 1):
        try:
            new_bars, total = _collect_symbol(coin, mode, days)
            total_new += new_bars
            if new_bars > 0:
                logger.info("[%d/%d] %s +%d (total=%d)", i, len(symbols), coin, new_bars, total)
        except Exception as e:
            errors += 1
            logger.warning("[%d/%d] %s error: %r", i, len(symbols), coin, e)

    elapsed = time.time() - t0
    logger.info(
        "OHLC collector done elapsed=%.1fs new_bars=%d symbols=%d errors=%d",
        elapsed, total_new, len(symbols), errors,
    )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

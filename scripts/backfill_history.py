#!/usr/bin/env python3
"""
Backfill historique Hyperliquid pour le backtester V7.

Sorties :
  - data/historical/fills.parquet  : tous les fills du compte (userFillsByTime,
    itération par chunks de 2000 en remontant le temps).
  - data/historical/ohlcv_1h_{SYM}.parquet : candles 1h par symbole sur ~6 mois
    (info type=candleSnapshot, chunks par mois).

Aucune écriture côté HL. Lecture seule.

Usage :
  cd ~/SalleDesMarches_v7
  source ../SalleDesMarches_fixed/.venv/bin/activate   # ou .venv local
  python3 scripts/backfill_history.py
"""
from __future__ import annotations
import os
import sys
import time
from pathlib import Path
from typing import Dict, List

import requests

REPO = Path(__file__).parent.parent
HL_API = "https://api.hyperliquid.xyz/info"

# Symboles à backfill (watchlist V6 + cibles V7)
WATCHLIST = [
    "BTC", "ETH", "SOL", "BNB", "AAVE", "LINK", "SUI", "DOGE", "BCH",
    "XRP", "TON", "ADA", "NEAR", "WLD", "PENGU", "HYPE", "VIRTUAL",
]


def _load_env() -> str:
    env_path = REPO / ".env"
    if not env_path.exists():
        sys.exit("Pas de .env trouvé — symlink vers ../SalleDesMarches_fixed/.env ?")
    addr = ""
    for line in env_path.read_text().splitlines():
        if line.startswith("HL_ACCOUNT_ADDRESS="):
            addr = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
    if not addr:
        sys.exit("HL_ACCOUNT_ADDRESS introuvable dans .env")
    return addr


def fetch_fills_by_time(addr: str, start_time_ms: int, end_time_ms: int) -> List[Dict]:
    """Récupère un chunk de fills entre start_time_ms et end_time_ms (max 2000).

    HL API : startTime obligatoire, endTime optionnel. Retourne max 2000 fills.
    """
    try:
        payload = {
            "type": "userFillsByTime",
            "user": addr,
            "startTime": start_time_ms,
            "endTime": end_time_ms,
        }
        r = requests.post(HL_API, json=payload, timeout=15)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        return []
    except Exception as e:
        print(f"  fetch_fills error: {e!r}", flush=True)
        return []


def backfill_fills(addr: str, months: int = 12) -> int:
    """Backfill par fenêtres glissantes du passé vers le présent.
    HL renvoie max 2000 par appel ; on chunke par fenêtres de N jours pour
    rester sous cette limite, et on étend si le chunk est plein.
    """
    out_path = REPO / "data" / "historical" / "fills.parquet"
    all_fills: List[Dict] = []
    seen_keys: set = set()

    now_ms = int(time.time() * 1000)
    span_ms = months * 30 * 24 * 3600 * 1000
    earliest = now_ms - span_ms
    window_ms = 7 * 24 * 3600 * 1000  # 7 jours par fenêtre (typique < 2000 fills)

    print(f"[backfill] fills userFillsByTime sur {months} mois (fenêtres 7j)...", flush=True)
    t = earliest
    iteration = 0
    while t < now_ms:
        iteration += 1
        t_end = min(t + window_ms, now_ms)
        chunk = fetch_fills_by_time(addr, t, t_end)
        new = 0
        for f in chunk:
            key = (int(f.get("oid", 0)), str(f.get("tid", "")))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_fills.append(f)
            new += 1
        ts_start = time.strftime('%Y-%m-%d', time.localtime(t/1000))
        ts_end = time.strftime('%Y-%m-%d', time.localtime(t_end/1000))
        print(f"  iter {iteration:3d}: {ts_start} → {ts_end}  {len(chunk):4d} chunk / {new:4d} nouveaux", flush=True)
        # Si chunk plein (≥2000), on rétrécit la fenêtre pour ne pas tronquer
        if len(chunk) >= 2000:
            window_ms = max(window_ms // 2, 3600 * 1000)
            print(f"  fenêtre rétrécie à {window_ms / 86400000:.2f}j", flush=True)
            continue  # même point de départ, plus petit
        t = t_end + 1
        # Si chunk peu rempli, on peut élargir prudemment
        if len(chunk) < 200 and window_ms < 30 * 24 * 3600 * 1000:
            window_ms = min(window_ms * 2, 30 * 24 * 3600 * 1000)
        time.sleep(0.3)

    print(f"[backfill] total fills : {len(all_fills)}", flush=True)
    if not all_fills:
        return 0

    # Sauvegarde parquet
    try:
        import pandas as pd
        df = pd.DataFrame(all_fills)
        df = df.sort_values("time").reset_index(drop=True)
        df.to_parquet(out_path, index=False)
        first = pd.to_datetime(df["time"].iloc[0], unit="ms")
        last = pd.to_datetime(df["time"].iloc[-1], unit="ms")
        print(f"[backfill] fills écrits → {out_path}  ({first} → {last})", flush=True)
    except ImportError:
        # Fallback JSON si pandas/pyarrow indispo
        import json
        out_path = out_path.with_suffix(".json")
        out_path.write_text(json.dumps(all_fills))
        print(f"[backfill] pandas/pyarrow indispo → fallback JSON {out_path}", flush=True)
    return len(all_fills)


def fetch_candles(coin: str, interval: str, start_ms: int, end_ms: int) -> List[Dict]:
    """candleSnapshot pour un symbole + interval entre 2 timestamps."""
    try:
        r = requests.post(
            HL_API,
            json={
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": interval,
                    "startTime": start_ms,
                    "endTime": end_ms,
                },
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        return []
    except Exception as e:
        print(f"  fetch_candles {coin} {interval}: {e!r}", flush=True)
        return []


def backfill_candles(months: int = 6) -> int:
    """Pour chaque symbole de la watchlist, pull candles 1h sur N mois.
    Stocké en un parquet par symbole pour faciliter le chargement modulaire."""
    interval = "1h"
    end_ms = int(time.time() * 1000)
    span_ms = months * 30 * 24 * 3600 * 1000
    start_ms = end_ms - span_ms

    print(f"[backfill] candles 1h sur {months} mois pour {len(WATCHLIST)} symbols...", flush=True)
    total_rows = 0
    try:
        import pandas as pd
    except ImportError:
        print("  pandas/pyarrow indispo, skip candles", flush=True)
        return 0

    for coin in WATCHLIST:
        # HL retourne max ~5000 candles par appel. 6 mois × 24 × 30 = 4320 candles,
        # ça passe en un appel. On chunk par mois pour rester safe.
        all_rows = []
        chunk_ms = 30 * 24 * 3600 * 1000  # 1 mois
        t = start_ms
        while t < end_ms:
            t_end = min(t + chunk_ms, end_ms)
            rows = fetch_candles(coin, interval, t, t_end)
            all_rows.extend(rows)
            t = t_end + 1
            time.sleep(0.2)

        if not all_rows:
            print(f"  {coin}: 0 candles", flush=True)
            continue
        df = pd.DataFrame(all_rows)
        # Normaliser noms colonnes (HL : T=ts open close high low n volume)
        rename = {"t": "ts_open", "T": "ts_close", "o": "open", "c": "close",
                  "h": "high", "l": "low", "v": "volume", "n": "trades"}
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
        if "ts_open" in df.columns:
            df = df.drop_duplicates(subset=["ts_open"]).sort_values("ts_open").reset_index(drop=True)
        out = REPO / "data" / "historical" / f"ohlcv_1h_{coin}.parquet"
        df.to_parquet(out, index=False)
        total_rows += len(df)
        first = pd.to_datetime(df["ts_open"].iloc[0], unit="ms") if "ts_open" in df.columns else "?"
        last = pd.to_datetime(df["ts_open"].iloc[-1], unit="ms") if "ts_open" in df.columns else "?"
        print(f"  {coin}: {len(df):4d} candles  {first} → {last}", flush=True)
    print(f"[backfill] candles total rows : {total_rows}", flush=True)
    return total_rows


def main() -> int:
    addr = _load_env()
    print(f"[backfill] account: {addr[:6]}...{addr[-4:]}", flush=True)
    print(f"[backfill] output: {REPO / 'data' / 'historical'}", flush=True)

    n_fills = backfill_fills(addr)
    print(flush=True)
    n_candles = backfill_candles(months=6)

    print(f"\n[backfill] DONE : {n_fills} fills + {n_candles} candle rows", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

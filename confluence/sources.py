"""
Sources d'historique profond — contournement d'une limite de l'API Hyperliquid.

**Le problème, mesuré le 2026-08-10.** Le §9.2 exige au minimum 3 ans de
données, avec une cadence de décision au 15m. Or `candleSnapshot` ne rend que
les 5000 DERNIÈRES bougies de chaque intervalle et ignore un `endTime` situé
plus tôt : paginer en arrière ne remonte pas le temps, on retombe sur la même
fenêtre. Relevé effectif sur BTC :

    1d  : 2001 bougies → 2000 j  (2021-02 → 2026-08)   ✔
    4h  : 5000 bougies →  833 j                        ✔
    1h  : 5000 bougies →  208 j                        ✘
    15m : 5000 bougies →   52 j                        ✘
    1m  : 5000 bougies →    3 j                        ✘

Le 15m — la cadence de décision de tout le module — plafonne à 52 jours. Le
protocole du §9 n'est donc PAS exécutable sur les seules données Hyperliquid.

**Les deux réponses, et ce qu'elles valent.**

1. `BinanceSource` — BTCUSDT perpétuel USD-M, historique profond (retour à
   2019), pagination avant qui fonctionne. C'est un **proxy** : même
   sous-jacent, même type d'instrument, mais un autre carnet. Il sert à
   estimer si le SIGNAL a un edge ; il ne dit rien de l'exécution réelle sur
   Hyperliquid. Les frais, le funding et le modèle de fill restent ceux
   d'Hyperliquid dans le backtest.
2. `collect_native()` — accumule les fetchs Hyperliquid dans une archive qui
   grossit. Ne rattrape pas le passé, mais construit la série native à partir
   d'aujourd'hui, pour que le backtest cesse un jour de dépendre d'un proxy.

**La mesure qui compte.** `compare_overlap()` chiffre l'écart entre proxy et
série native sur leur fenêtre commune. Un proxy qu'on ne mesure pas est une
hypothèse ; mesuré, c'est une approximation dont on connaît le prix. Si la
corrélation des rendements 15m décroche, le résultat du §9 ne vaut pas pour
Hyperliquid, et il faut le dire avant d'engager de l'argent.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from confluence.indicators import INTERVAL_MS, sort_dedup

logger = logging.getLogger("sdm.confluence.sources")

ARCHIVE_DIR = Path(__file__).resolve().parent / "state" / "archive"

BINANCE_URL = "https://fapi.binance.com/fapi/v1/klines"
BINANCE_INTERVALS = {"1m": "1m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}
BINANCE_MAX_LIMIT = 1500

# Correspondance des noms de perp. Hyperliquid cote « BTC », Binance
# « BTCUSDT » ; hors de cette table, on refuse plutôt que de deviner.
SYMBOL_MAP = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}


class BinanceSource:
    """Historique profond en proxy. Signature `fetch` compatible avec
    `confluence.data.fetch_paginated`, mais la pagination y est AVANT (par
    `startTime` croissant) — c'est ce qui remonte réellement le temps."""

    name = "binance-fapi"

    def __init__(self, throttle_s: float = 0.15, timeout: float = 20.0) -> None:
        self.throttle_s = throttle_s
        self.timeout = timeout

    def fetch_range(self, symbol: str, interval: str, start_ms: int,
                    end_ms: int) -> List[dict]:
        import requests

        pair = SYMBOL_MAP.get(symbol.upper())
        if pair is None:
            raise ValueError(
                f"aucune correspondance Binance pour {symbol!r} — l'ajouter "
                f"explicitement à SYMBOL_MAP plutôt que de la déduire")
        if interval not in BINANCE_INTERVALS:
            raise ValueError(f"intervalle non géré: {interval}")

        step = INTERVAL_MS[interval]
        out: List[dict] = []
        cursor = start_ms
        guard = 0
        max_requests = int((end_ms - start_ms) / (step * BINANCE_MAX_LIMIT)) + 10

        while cursor < end_ms and guard < max_requests:
            guard += 1
            resp = requests.get(BINANCE_URL, params={
                "symbol": pair, "interval": BINANCE_INTERVALS[interval],
                "startTime": int(cursor), "limit": BINANCE_MAX_LIMIT,
            }, timeout=self.timeout)
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            out.extend({
                "ts": int(k[0]), "open": float(k[1]), "high": float(k[2]),
                "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
            } for k in batch)
            last = int(batch[-1][0])
            if last < cursor:
                break
            cursor = last + step
            if self.throttle_s:
                time.sleep(self.throttle_s)

        if guard >= max_requests:
            logger.warning("%s/%s: plafond de requêtes atteint — historique tronqué",
                           symbol, interval)
        series = [c for c in sort_dedup(out) if start_ms <= c["ts"] < end_ms]
        # La dernière bougie Binance peut être en cours (§3).
        now_ms = int(time.time() * 1000)
        return [c for c in series if c["ts"] + step <= now_ms]

    def __call__(self, symbol: str, interval: str, days: float,
                 end_ms: Optional[int] = None) -> List[dict]:
        end = int(end_ms) if end_ms is not None else int(time.time() * 1000)
        return self.fetch_range(symbol, interval, end - int(days * 86_400_000), end)


# ── Mesure de fidélité du proxy ──────────────────────────────────────────────

@dataclass
class OverlapReport:
    timeframe: str
    bars: int
    return_correlation: Optional[float]
    median_basis_bps: Optional[float]
    max_basis_bps: Optional[float]

    @property
    def usable(self) -> bool:
        """Seuil délibérément strict : en dessous de 0,99 de corrélation des
        rendements 15m, les deux marchés ne racontent plus la même histoire à
        l'échelle où la stratégie décide."""
        return (self.return_correlation is not None
                and self.return_correlation >= 0.99
                and self.bars >= 500)

    def summary(self) -> str:
        corr = "n/a" if self.return_correlation is None else f"{self.return_correlation:.4f}"
        med = "n/a" if self.median_basis_bps is None else f"{self.median_basis_bps:+.1f}"
        return (f"{self.timeframe}: {self.bars} bougies communes, "
                f"corrélation des rendements {corr}, base médiane {med} bps → "
                f"{'utilisable' if self.usable else 'NON UTILISABLE comme proxy'}")


def compare_overlap(native: Sequence[dict], proxy: Sequence[dict],
                    timeframe: str) -> OverlapReport:
    """Compare deux séries sur leurs timestamps communs.

    On corrèle les RENDEMENTS, pas les prix : deux séries de prix crypto sont
    corrélées à 0,999 par construction (même tendance), ce qui ne prouve rien.
    Ce qui compte pour une stratégie qui décide au 15m, c'est que les VARIATIONS
    coïncident.
    """
    by_ts = {int(c["ts"]): float(c["close"]) for c in proxy}
    pairs = [(float(c["close"]), by_ts[int(c["ts"])])
             for c in native if int(c["ts"]) in by_ts]
    if len(pairs) < 3:
        return OverlapReport(timeframe, len(pairs), None, None, None)

    basis = [10_000.0 * (p - n) / n for n, p in pairs if n > 0]
    rn = [pairs[i][0] / pairs[i - 1][0] - 1.0 for i in range(1, len(pairs))
          if pairs[i - 1][0] > 0]
    rp = [pairs[i][1] / pairs[i - 1][1] - 1.0 for i in range(1, len(pairs))
          if pairs[i - 1][1] > 0]

    corr = None
    if len(rn) == len(rp) and len(rn) > 2:
        mn, mp = sum(rn) / len(rn), sum(rp) / len(rp)
        cov = sum((a - mn) * (b - mp) for a, b in zip(rn, rp))
        vn = sum((a - mn) ** 2 for a in rn)
        vp = sum((b - mp) ** 2 for b in rp)
        if vn > 0 and vp > 0:
            corr = cov / (vn ** 0.5 * vp ** 0.5)

    ordered = sorted(basis)
    median = ordered[len(ordered) // 2] if ordered else None
    worst = max(basis, key=abs) if basis else None
    return OverlapReport(timeframe, len(pairs), corr, median, worst)


# ── Archive native accumulée ─────────────────────────────────────────────────

def archive_path(symbol: str, timeframe: str) -> Path:
    return ARCHIVE_DIR / f"{symbol}__{timeframe}.json"


def collect_native(symbol: str, timeframes: Sequence[str] = ("15m", "1h", "1d"),
                   fetch=None) -> Dict[str, int]:
    """Fusionne un fetch Hyperliquid frais dans l'archive locale.

    À lancer périodiquement (une fois par semaine suffit pour le 15m, dont la
    fenêtre API couvre 52 jours). L'archive ne rétrécit jamais : c'est ce qui
    permet, à terme, de rejouer le §9 sur des données NATIVES et de se passer
    du proxy.
    """
    if fetch is None:
        from simplebot.data import fetch_ohlcv as fetch

    out: Dict[str, int] = {}
    for tf in timeframes:
        step = INTERVAL_MS[tf]
        days = 4900 * step / 86_400_000.0        # la fenêtre que l'API veut bien rendre
        fresh = fetch(symbol, tf, days)
        path = archive_path(symbol, tf)
        existing: List[dict] = []
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.error("archive %s illisible (%s) — elle NE sera pas écrasée",
                             path.name, exc)
                out[tf] = 0
                continue
        merged = sort_dedup(list(existing) + list(fresh))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(merged), encoding="utf-8")
        out[tf] = len(merged) - len(existing)
        logger.info("archive %s/%s: %d bougies (+%d), couvre %.0f j",
                    symbol, tf, len(merged), out[tf],
                    (merged[-1]["ts"] - merged[0]["ts"]) / 86_400_000.0 if merged else 0)
    return out


def load_archive(symbol: str, timeframe: str) -> List[dict]:
    path = archive_path(symbol, timeframe)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def resolve_source(name: str):
    """`native` (Hyperliquid) ou `binance` (proxy profond)."""
    if name == "native":
        from simplebot.data import fetch_ohlcv

        return fetch_ohlcv
    if name == "binance":
        return BinanceSource()
    raise ValueError(f"source inconnue: {name!r} (native|binance)")


__all__ = ["ARCHIVE_DIR", "BinanceSource", "OverlapReport", "SYMBOL_MAP",
           "archive_path", "collect_native", "compare_overlap", "load_archive",
           "resolve_source"]

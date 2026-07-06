"""
Moteur permanent MinuteLab — PAPER TRADING uniquement.

Boucle :
- toutes les 5 s : échantillonne le mid BTC (allMids), met à jour le gain de
  la position paper et sort si le gain croise sous sa moyenne mobile
  (EXIT_MA_SAMPLES × 5 s), ou si stop dur / durée max ;
- à chaque nouvelle bougie 1 m clôturée : évalue le signal d'entrée de la
  stratégie championne ; si flat et signal → entrée paper au mid ± slippage ;
- à intervalle adaptatif (15 min par défaut, borné [5, 30]) : relance la
  sélection complète sur les 60 dernières minutes. L'intervalle se resserre
  quand le champion déçoit ou qu'on est flat, se détend quand il gagne —
  le rythme du marché change, le rythme de réévaluation aussi.

État persisté dans minutelab/state/ (state.json + trades.jsonl + scans.jsonl).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import List, Optional

import requests

from minutelab import config
from minutelab.data1m import fetch_recent_1m
from minutelab.selector import select
from minutelab.strategies import Strat, compute_signals

logger = logging.getLogger("sdm.minutelab.engine")

HL_INFO_URL = "https://api.hyperliquid.xyz/info"


def fetch_mid(symbol: str, timeout: float = 5.0) -> Optional[float]:
    try:
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


class PaperEngine:
    def __init__(self):
        self.symbol = config.SYMBOL
        self.champion: Optional[Strat] = None
        self.champion_since: float = 0.0
        self.champion_entry_equity: float = 0.0
        self.reselect_min: float = config.RESELECT_START_MIN
        self.next_reselect: float = 0.0
        self.equity_pct: float = 0.0        # cumul des pnl trades (% prix)
        self.position: Optional[dict] = None
        self.last_bar_ts: int = 0
        self.last_entry_bar_ts: int = 0   # une seule entrée par signal/bougie
        os.makedirs(config.STATE_DIR, exist_ok=True)

    # --- persistance ---------------------------------------------------

    def _append(self, fname: str, obj: dict) -> None:
        with open(os.path.join(config.STATE_DIR, fname), "a") as f:
            f.write(json.dumps(obj) + "\n")

    def _save_state(self) -> None:
        state = {
            "ts": time.time(),
            "symbol": self.symbol,
            "champion": self.champion.name if self.champion else None,
            "reselect_min": self.reselect_min,
            "equity_pct": round(self.equity_pct, 6),
            "position": (
                {k: v for k, v in self.position.items() if k != "gains"}
                if self.position else None
            ),
        }
        path = os.path.join(config.STATE_DIR, "state.json")
        with open(path, "w") as f:
            json.dump(state, f, indent=2)

    # --- sélection -----------------------------------------------------

    def reselect(self, candles: List[dict]) -> None:
        res = select(candles)
        champ = res["champion"]

        # Adaptation du rythme AVANT de remplacer le champion : basée sur ce
        # que l'ancien champion a réellement produit depuis sa sélection.
        pnl_since = self.equity_pct - self.champion_entry_equity
        if self.champion is None or pnl_since < 0:
            self.reselect_min = max(config.RESELECT_MIN_MIN, self.reselect_min / 2)
        elif pnl_since > 0:
            self.reselect_min = min(config.RESELECT_MAX_MIN, self.reselect_min * 1.5)

        new = champ.strat if champ else None
        if (new and self.champion and new != self.champion) or bool(new) != bool(self.champion):
            logger.info("champion : %s → %s",
                        self.champion.name if self.champion else "FLAT",
                        new.name if new else "FLAT")
        self.champion = new
        self.champion_since = time.time()
        self.champion_entry_equity = self.equity_pct
        self.next_reselect = time.time() + self.reselect_min * 60

        self._append("scans.jsonl", {
            "ts": res["ts"],
            "scanned": res["scanned"],
            "n_qualified": len(res["qualified"]),
            "champion": champ.strat.name if champ else None,
            "champion_metrics": (
                {"n_trades": champ.n_trades,
                 "pnl_pct": round(champ.pnl_pct, 6),
                 "pnl_recent_pct": round(champ.pnl_recent_pct, 6),
                 "winrate": round(champ.winrate, 3)}
                if champ else None
            ),
            "reselect_min": self.reselect_min,
            "pnl_since_prev_scan": round(pnl_since, 6),
        })
        self._save_state()

    # --- exécution paper -------------------------------------------------

    def maybe_enter(self, candles: List[dict], mid: float) -> None:
        if self.champion is None or self.position is not None:
            return
        if candles[-1]["ts"] == self.last_entry_bar_ts:
            return
        signals = compute_signals(candles, self.champion)
        if signals and signals[-1] != 0:
            self.last_entry_bar_ts = candles[-1]["ts"]
            d = signals[-1]
            entry = mid * (1 + d * config.SLIPPAGE_PCT)
            self.position = {
                "dir": d, "entry": entry, "ts": time.time(),
                "strat": self.champion.name, "gains": [],
            }
            logger.info("ENTRÉE paper %s %s @ %.2f (%s)",
                        "LONG" if d == 1 else "SHORT", self.symbol, entry,
                        self.champion.name)
            self._save_state()

    def check_exit(self, mid: float) -> None:
        """Appelé toutes les EXIT_SAMPLE_SEC secondes quand une position est ouverte."""
        pos = self.position
        if pos is None:
            return
        g = pos["dir"] * (mid - pos["entry"]) / pos["entry"]
        gains = pos["gains"]
        gains.append(g)
        held_min = (time.time() - pos["ts"]) / 60

        reason = None
        if g <= -config.HARD_SL_PCT:
            reason = "HARD_SL"
        elif held_min >= config.MAX_HOLD_MIN:
            reason = "MAX_HOLD"
        elif len(gains) > config.EXIT_WARMUP_SAMPLES:
            m = config.EXIT_MA_SAMPLES
            ma_now = sum(gains[-m:]) / min(m, len(gains))
            prev = gains[:-1]
            ma_prev = sum(prev[-m:]) / min(m, len(prev))
            if gains[-2] >= ma_prev and g < ma_now:
                gate = (2.0 * (config.FEE_PCT + config.SLIPPAGE_PCT)
                        if config.EXIT_REQUIRE_NET_GAIN else None)
                if gate is None or g > gate:
                    reason = "PNL_MA"

        if reason:
            exit_px = mid * (1 - pos["dir"] * config.SLIPPAGE_PCT)
            pnl = pos["dir"] * (exit_px - pos["entry"]) / pos["entry"] - 2 * config.FEE_PCT
            self.equity_pct += pnl
            trade = {
                "ts_entry": pos["ts"], "ts_exit": time.time(),
                "dir": pos["dir"], "entry": pos["entry"], "exit": exit_px,
                "pnl_pct": round(pnl, 6), "reason": reason,
                "strat": pos["strat"],
                "equity_pct": round(self.equity_pct, 6),
            }
            self._append("trades.jsonl", trade)
            logger.info("SORTIE paper %s @ %.2f — pnl %.4f%% (%s) equity %.4f%%",
                        self.symbol, exit_px, pnl * 100, reason,
                        self.equity_pct * 100)
            self.position = None
            self._save_state()

    # --- boucle principale ----------------------------------------------

    def run_forever(self) -> None:
        logger.info("MinuteLab démarre — %s, paper trading, frais %.3f%%/side",
                    self.symbol, (config.FEE_PCT + config.SLIPPAGE_PCT) * 100)
        candles: List[dict] = []
        while True:
            now = time.time()
            try:
                # Rafraîchit les bougies quand une nouvelle 1 m est clôturée
                if not candles or candles[-1]["ts"] // 60000 < (int(now * 1000) - 60000) // 60000:
                    fresh = fetch_recent_1m(self.symbol, config.WARMUP_HOURS)
                    if fresh:
                        candles = fresh
                        self.last_bar_ts = candles[-1]["ts"]

                if now >= self.next_reselect and candles:
                    self.reselect(candles)

                mid = fetch_mid(self.symbol)
                if mid is not None and candles:
                    self.check_exit(mid)
                    self.maybe_enter(candles, mid)
            except Exception:
                logger.exception("tick en échec — on continue")

            time.sleep(config.EXIT_SAMPLE_SEC)

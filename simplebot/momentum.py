"""
MomentumPaperTrader — stratégie momentum 4h en PAPER TRADING pur.

Combinaison validée out-of-sample (833 j × 31 symboles, t-stat cluster-robuste
2.9, 84 % des symboles positifs — voir mémoire projet « simplebot-edge-oos ») :

  - ROC(12 bougies 4h = 48 h) > +2 %  → LONG  (suivre le mouvement)
  - ROC(48 h) < -2 %                  → SHORT
  - PAS de take-profit : les TP amputent les gros gagnants et tuent l'edge ;
  - sorties : time-exit après 72 bougies 4h (12 jours) OU stop 2×ATR(14) ;
  - signal opposé pendant la tenue → flip.

Deux principes non négociables, tous deux issus des données :
  1. Paramètres FIGÉS, univers-wide. AUCUN optimiseur : la ré-optimisation sur
     fenêtre trailing s'est avérée systématiquement contre-productive (la
     performance des stratégies mean-reverte).
  2. PAPER ONLY : cette classe ne détient AUCUN client d'exchange — il est
     structurellement impossible qu'elle envoie un ordre réel. Le passage en
     réel, le cas échéant, sera une décision explicite après 2-4 semaines de
     paper concluant.

Comptabilité paper :
  - entrée au close de la bougie de signal (≈ open de la bougie suivante sur
    perps liquides — convention du backtest) ;
  - coût aller-retour taker : 2 × (FEE_PCT + SLIPPAGE_PCT) ;
  - FUNDING accru par heure de tenue au taux courant HL (les positions momentum
    suivent la foule et paient souvent le funding — coût matériel sur 12 j,
    mesuré ~+0.29 % côté long) ;
  - equity paper compoundée : pnl_usd = notional × pnl_pct,
    notional = equity × MOMENTUM_NOTIONAL_PCT à l'entrée.

État persisté (JSON atomique) : simplebot/state/momentum_state.json
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Callable, Dict, List, Optional

from simplebot import config
from simplebot.data import closed_candles, fetch_funding_rates, fetch_ohlcv
from simplebot.strategy import atr

logger = logging.getLogger("sdm.simplebot.momentum")

COST = 2.0 * (config.FEE_PCT + config.SLIPPAGE_PCT)


def momentum_signal(candles: List[dict], roc_bars: int = None, thr: float = None,
                    atr_len: int = None) -> dict:
    """Signal momentum sur la DERNIÈRE bougie clôturée.
    +1 si ROC(roc_bars) > +thr, -1 si < -thr, 0 sinon."""
    roc_bars = roc_bars if roc_bars is not None else config.MOMENTUM_ROC_BARS
    thr = thr if thr is not None else config.MOMENTUM_THR
    atr_len = atr_len if atr_len is not None else config.MOMENTUM_ATR_LEN
    need = max(roc_bars, atr_len) + 2
    if len(candles) < need:
        return {"signal": 0, "roc": 0.0, "atr": 0.0, "close": 0.0, "ts": None}
    closes = [c["close"] for c in candles]
    roc = closes[-1] / closes[-1 - roc_bars] - 1.0
    sig = 1 if roc > thr else (-1 if roc < -thr else 0)
    return {
        "signal": sig,
        "roc": roc,
        "atr": atr(candles, atr_len)[-1],
        "close": closes[-1],
        "ts": candles[-1].get("ts"),
    }


class MomentumPaperTrader:
    """Boucle paper : un sweep par nouvelle bougie 4h (throttlé anti-429)."""

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        fetch: Optional[Callable[..., List[dict]]] = None,
        funding_fetch: Optional[Callable[[], Dict[str, float]]] = None,
        state_file=None,
    ):
        self.symbols = symbols or config.SYMBOLS
        self._fetch = fetch or fetch_ohlcv
        self._funding_fetch = funding_fetch or fetch_funding_rates
        self.state_file = state_file or config.MOMENTUM_STATE_FILE
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.state = self._load_state()

    # ── État ─────────────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            st = {}
        st.setdefault("started_at", time.time())
        st.setdefault("equity", config.MOMENTUM_PAPER_EQUITY)
        st.setdefault("equity_history", [])
        st.setdefault("positions", {})
        st.setdefault("trades", [])
        st.setdefault("last_ts", {})
        return st

    def _save_state(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
            os.replace(tmp, self.state_file)
        except Exception as e:
            logger.warning("Sauvegarde momentum_state échouée: %r", e)

    # ── Comptabilité ─────────────────────────────────────────────────────────

    def _accrue_funding(self, pos: dict, rate: float, now_ms: int) -> None:
        """Accrue le funding horaire depuis le dernier pointage. Positif = payé
        par les longs ; une position momentum paie quand elle suit la foule."""
        hours = int((now_ms - pos["funding_ts"]) // 3_600_000)
        if hours <= 0:
            return
        pos["funding_pct"] = pos.get("funding_pct", 0.0) - pos["dir"] * rate * hours
        pos["funding_ts"] += hours * 3_600_000

    def _close(self, symbol: str, exit_px: float, reason: str, ts) -> None:
        pos = self.state["positions"].pop(symbol, None)
        if not pos:
            return
        funding = pos.get("funding_pct", 0.0)
        pnl_pct = pos["dir"] * (exit_px - pos["entry"]) / pos["entry"] - COST + funding
        pnl_usd = pos["notional"] * pnl_pct
        self.state["equity"] += pnl_usd
        trades = self.state["trades"]
        trades.append({
            "symbol": symbol,
            "dir": pos["dir"],
            "entry": pos["entry"],
            "exit": exit_px,
            "pnl_pct": pnl_pct,
            "funding_pct": funding,
            "pnl_usd": pnl_usd,
            "reason": reason,
            "entry_ts": pos["entry_ts"],
            "exit_ts": ts,
        })
        wins = len([t for t in trades if t["pnl_pct"] > 0])
        logger.info(
            "[MOMENTUM-PAPER] %s: EXIT %s @ %.6g (%s) pnl=%+.2f%% (funding %+.3f%%) "
            "| equity=%.2f | %d trades, WR %.0f%%",
            symbol, "LONG" if pos["dir"] == 1 else "SHORT", exit_px, reason,
            pnl_pct * 100, funding * 100, self.state["equity"],
            len(trades), 100.0 * wins / len(trades),
        )

    def _open(self, symbol: str, direction: int, px: float, atr_val: float, ts) -> None:
        if atr_val <= 0 or px <= 0:
            return
        notional = self.state["equity"] * config.MOMENTUM_NOTIONAL_PCT
        self.state["positions"][symbol] = {
            "dir": direction,
            "entry": px,
            "sl": px - direction * config.MOMENTUM_SL_ATR * atr_val,
            "entry_ts": ts,
            "funding_ts": ts,
            "funding_pct": 0.0,
            "notional": notional,
        }
        logger.info(
            "[MOMENTUM-PAPER] %s: ENTER %s @ %.6g (SL %.6g, notional %.2f$) — "
            "sortie: time 12j ou SL, pas de TP",
            symbol, "LONG" if direction == 1 else "SHORT", px,
            self.state["positions"][symbol]["sl"], notional,
        )

    # ── Cœur : traitement d'un symbole ───────────────────────────────────────

    def _process_symbol(self, symbol: str, funding_rate: float) -> None:
        candles = closed_candles(
            self._fetch(symbol, config.MOMENTUM_INTERVAL, config.MOMENTUM_FETCH_DAYS),
            config.MOMENTUM_INTERVAL_MS,
        )
        if not candles:
            return
        last_ts = candles[-1]["ts"]
        prev_ts = self.state["last_ts"].get(symbol)
        pos = self.state["positions"].get(symbol)

        # 1) rejouer les bougies nouvelles pour les SORTIES (SL prioritaire, puis time)
        if pos is not None:
            self._accrue_funding(pos, funding_rate, last_ts)
            for c in candles:
                if prev_ts is not None and c["ts"] <= prev_ts:
                    continue
                if c["ts"] <= pos["entry_ts"]:
                    continue
                d = pos["dir"]
                if (d == 1 and c["low"] <= pos["sl"]) or (d == -1 and c["high"] >= pos["sl"]):
                    self._close(symbol, pos["sl"], "SL", c["ts"])
                    pos = None
                    break
                bars_held = (c["ts"] - pos["entry_ts"]) / config.MOMENTUM_INTERVAL_MS
                if bars_held >= config.MOMENTUM_TIME_EXIT_BARS:
                    self._close(symbol, c["close"], "TIME", c["ts"])
                    pos = None
                    break

        # 2) une décision d'ENTRÉE par bougie (sur la dernière clôturée uniquement)
        if prev_ts == last_ts:
            return
        self.state["last_ts"][symbol] = last_ts

        sig = momentum_signal(candles)
        if sig["signal"] == 0:
            return
        direction = sig["signal"]
        pos = self.state["positions"].get(symbol)

        if pos is not None and pos["dir"] == direction:
            return
        if pos is not None and pos["dir"] != direction:
            logger.info("[MOMENTUM-PAPER] %s: signal opposé → flip", symbol)
            self._close(symbol, sig["close"], "FLIP", sig["ts"])
        if len(self.state["positions"]) >= config.MOMENTUM_MAX_OPEN:
            logger.info("[MOMENTUM-PAPER] %s: signal %+d ignoré — MAX_OPEN (%d) atteint",
                        symbol, direction, config.MOMENTUM_MAX_OPEN)
            return
        self._open(symbol, direction, sig["close"], sig["atr"], sig["ts"])

    # ── Boucle ───────────────────────────────────────────────────────────────

    def sweep(self) -> None:
        """Un passage complet sur l'univers (appelé après chaque clôture 4h)."""
        try:
            rates = self._funding_fetch()
        except Exception as e:
            logger.warning("[MOMENTUM-PAPER] funding illisible (%r) — accrual différé", e)
            rates = {}
        for idx, symbol in enumerate(self.symbols):
            if idx > 0 and config.FETCH_THROTTLE_SEC > 0:
                time.sleep(config.FETCH_THROTTLE_SEC)
            try:
                self._process_symbol(symbol, rates.get(symbol, 0.0))
            except Exception as e:
                logger.error("[MOMENTUM-PAPER] %s en erreur: %r", symbol, e)
        now = time.time()
        hist = self.state["equity_history"]
        if not hist or now - hist[-1][0] >= 3600:
            hist.append([now, self.state["equity"]])
        self._save_state()

    def _current_bar(self, now_ms: Optional[int] = None) -> int:
        now_ms = now_ms or int(time.time() * 1000)
        return now_ms // config.MOMENTUM_INTERVAL_MS

    def _loop(self) -> None:
        logger.info(
            "[MOMENTUM-PAPER] démarré — ROC(%d×4h) ±%.1f%%, time-exit %d bougies, "
            "SL %.1f×ATR, PAS de TP, %d symboles, equity paper %.2f$ — AUCUN ordre réel",
            config.MOMENTUM_ROC_BARS, config.MOMENTUM_THR * 100,
            config.MOMENTUM_TIME_EXIT_BARS, config.MOMENTUM_SL_ATR,
            len(self.symbols), self.state["equity"],
        )
        # Décalage du sweep initial : au démarrage, le trader live et l'optimiseur
        # tirent déjà sur l'API — démarrer en même temps provoque des bursts 429
        # (cause du faux kill-switch du 2026-07-04 08:16).
        self._stop.wait(90)
        if self._stop.is_set():
            return
        self.sweep()   # rattrapage au démarrage (sorties manquées pendant l'arrêt)
        last_bar = self._current_bar()
        while not self._stop.is_set():
            self._stop.wait(config.MOMENTUM_LOOP_SEC)
            if self._stop.is_set():
                break
            bar = self._current_bar()
            if bar != last_bar:      # nouvelle bougie 4h clôturée
                last_bar = bar
                try:
                    self.sweep()
                except Exception as e:
                    logger.error("[MOMENTUM-PAPER] sweep en erreur: %r", e, exc_info=True)

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="SimpleBotMomentum", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    t = MomentumPaperTrader()
    t.sweep()
    print(json.dumps({"equity": t.state["equity"],
                      "positions": t.state["positions"],
                      "n_trades": len(t.state["trades"])}, indent=2))

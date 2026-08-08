"""
RSIMRPaperTrader — rachat de survente RSI en PAPER TRADING pur.

Candidat confirmé le 2026-08-07 (session « rythme du marché ») par le protocole
complet : découverte sur 65 j 15m/1h, puis test confirmatoire FIGÉ sur 200 j
de 1h (script rsi_mr_200d.py, scratchpad session 2de5c80d) :

  - fenêtre pleine 200 j : +32.0 bps bruts/trade, net +17.0, t_cl/jour=+3.73 ;
  - OOS pur (115 j jamais vus en découverte) : +26.7 bps bruts, net +11.7,
    t_cl=+2.55 — critère figé (brut>15, t≥2) passé ;
  - placebo (40 permutations de barres, placebo_gate.shuffle_candles) :
    p=0.024, t réel au-dessus des 40 tirages ;
  - deux moitiés positives (+29.8/+33.8). Réserves : majors BTC/ETH/SOL
    mortes sur 200 j (net négatif), largeur 24/48 symboles — l'edge est
    dans les alts, mais l'univers reste FIGÉ à 48 (pas de re-sélection).

RÈGLE FIGÉE (aucun optimiseur, aucun re-tuning en cours de test) :
  - RSI(14) Wilder sur closes 1h ;
  - LONG quand le RSI passe de ≤30 à >30 sur une bougie CLÔTURÉE ;
  - entrée au close de cette bougie, sortie au close 4 bougies plus tard ;
  - frais round-trip 15 bps (7.5 bps/côté, hypothèse du backtest) ;
  - univers = les 48 symboles de la découverte, notional fixe par trade,
    signaux chevauchants tous pris (comme le pooled du backtest).

⚠️ Statut : CANDIDAT CONFIRMÉ EN BACKTEST, le paper est le juge final
(précédent : momentum 4h beau en backtest, −21 % en forward). Critère de
jugement à ~6 semaines (mi-septembre 2026, comme xsmom) : moyenne nette
par trade > 0 et du même ordre que le backtest (+17 bps net), sur ≥300
trades. PAPER ONLY — cette classe ne détient AUCUN client d'exchange.

État persisté (JSON atomique) : rsimr/state/rsimr_state.json
Trades clos (append) :         rsimr/state/trades.jsonl
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, List, Optional

from simplebot.data import closed_candles, fetch_ohlcv

logger = logging.getLogger("sdm.rsimr")

# ── Paramètres FIGÉS (test confirmatoire 07-08 — ne pas optimiser) ───────────
SYMBOLS = [
    "AAVE", "ACE", "ADA", "AERO", "ARB", "AVAX", "BNB", "BTC", "DOGE", "ENA",
    "ETHFI", "ETH", "FARTCOIN", "HBAR", "HYPE", "INJ", "JTO", "KAITO", "LDO",
    "LINK", "LIT", "LTC", "MON", "MORPHO", "NEAR", "NIL", "ONDO", "PAXG",
    "PENGU", "PUMP", "SOL", "SUI", "TAO", "TRUMP", "TRX", "UNI", "VIRTUAL",
    "VVV", "WLD", "WLFI", "XMR", "XPL", "XRP", "ZEC", "ZRO", "kBONK",
    "kPEPE", "kSHIB",
]
RSI_N = 14
H_BARS = 4                 # sortie au close de la 4e bougie suivante (≈4 h)
FEE_SIDE = 0.00075         # 7.5 bps/côté = 15 bps round-trip (hyp. backtest)
NOTIONAL = 50.0            # $ par trade, fixe (pas de compounding)
PAPER_EQUITY0 = 1000.0
FETCH_DAYS = 11.0          # ~264 barres 1h : convergence Wilder largement acquise
MIN_BARS = 200             # jamais de signal avant ce rang dans la fenêtre
HOUR_MS = 3_600_000

STATE_FILE = Path(os.environ.get(
    "RSIMR_STATE_FILE",
    str(Path(__file__).resolve().parent / "state" / "rsimr_state.json"),
))


def rsi_series(closes: List[float], n: int = RSI_N) -> List[float]:
    """RSI Wilder — implémentation identique au test confirmatoire."""
    out = [50.0] * len(closes)
    ag = al = 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        g, l = max(d, 0.0), max(-d, 0.0)
        if i <= n:
            ag += g / n
            al += l / n
        else:
            ag = ag * (n - 1) / n + g / n
            al = al * (n - 1) / n + l / n
        out[i] = 100.0 if al <= 0 and ag > 0 else (
            50.0 if al <= 0 else 100.0 - 100.0 / (1.0 + ag / al))
    return out


class RSIMRPaperTrader:
    """Un sweep par heure UTC après clôture de la bougie 1h. Aucun ordre réel."""

    def __init__(
        self,
        fetch: Optional[Callable[..., List[dict]]] = None,
        state_file: Optional[Path] = None,
    ):
        self._fetch = fetch or fetch_ohlcv
        self.state_file = Path(state_file) if state_file else STATE_FILE
        self.trades_file = self.state_file.parent / "trades.jsonl"
        self.state = self._load_state()

    # ── État ────────────────────────────────────────────────────────────────
    def _load_state(self) -> dict:
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            st = {}
        st.setdefault("started_at", time.time())
        st.setdefault("realized_usd", 0.0)
        st.setdefault("fees_paid", 0.0)
        st.setdefault("n_trades", 0)
        st.setdefault("n_wins", 0)
        st.setdefault("sum_net_bps", 0.0)
        # positions ouvertes : [{sym, entry_ts, entry_px, exit_ts}]
        #   entry_ts = ts d'OUVERTURE de la bougie signal (close = prix d'entrée)
        #   exit_ts  = entry_ts + H_BARS heures (close de cette bougie = sortie)
        st.setdefault("open", [])
        st.setdefault("last_seen", {})       # sym -> ts dernière bougie traitée
        st.setdefault("equity_history", [])
        st.setdefault("last_sweep_hour", None)
        return st

    def _save_state(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=1)
            os.replace(tmp, self.state_file)
        except Exception as e:
            logger.warning("Sauvegarde rsimr_state échouée: %r", e)

    def _log_trade(self, rec: dict) -> None:
        try:
            self.trades_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.trades_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception as e:
            logger.warning("Écriture trades.jsonl échouée: %r", e)

    # ── Cœur ────────────────────────────────────────────────────────────────
    def sweep_if_due(self, now: Optional[float] = None) -> bool:
        """Traite chaque symbole si une nouvelle bougie 1h a clôturé."""
        now = now or time.time()
        hour = int(now // 3600)
        if self.state["last_sweep_hour"] == hour:
            return False

        now_ms = int(now * 1000)
        opened = closed = 0
        for sym in SYMBOLS:
            try:
                candles = closed_candles(
                    self._fetch(sym, "1h", FETCH_DAYS), HOUR_MS, now_ms)
            except Exception as e:
                logger.warning("fetch 1h %s: %r", sym, e)
                continue
            if len(candles) < MIN_BARS + 1:
                continue
            opened += self._open_new_signals(sym, candles)
            closed += self._close_due_positions(sym, candles)

        self.state["last_sweep_hour"] = hour
        eq = PAPER_EQUITY0 + self.state["realized_usd"]
        hist = self.state["equity_history"]
        hist.append([now, round(eq, 4)])
        if len(hist) > 20000:
            del hist[:-20000]
        if opened or closed:
            n = self.state["n_trades"]
            avg = self.state["sum_net_bps"] / n if n else 0.0
            logger.info(
                "[RSIMR-PAPER] sweep h=%d | +%d ouvertures, %d clôtures | "
                "%d positions ouvertes | %d trades clos, moy nette %+.1f bps | "
                "equity %.2f$", hour, opened, closed,
                len(self.state["open"]), n, avg, eq)
        self._save_state()
        return True

    def _open_new_signals(self, sym: str, candles: List[dict]) -> int:
        """Ouvre une position par croisement RSI 30↑ sur bougie non encore vue."""
        if sym not in self.state["last_seen"]:
            # premier passage : amorçage pur forward, pas de replay historique
            self.state["last_seen"][sym] = candles[-1]["ts"]
            return 0
        closes = [c["close"] for c in candles]
        r = rsi_series(closes)
        last_seen = self.state["last_seen"][sym]
        n_open = 0
        for i in range(max(MIN_BARS, 1), len(candles)):
            ts = candles[i]["ts"]
            if ts <= last_seen:
                continue
            if r[i - 1] <= 30 < r[i]:
                self.state["open"].append({
                    "sym": sym,
                    "entry_ts": ts,
                    "entry_px": closes[i],
                    "exit_ts": ts + H_BARS * HOUR_MS,
                })
                n_open += 1
                logger.info("[RSIMR-PAPER] LONG %s @ %.6g (RSI %.1f→%.1f)",
                            sym, closes[i], r[i - 1], r[i])
        if candles:
            self.state["last_seen"][sym] = candles[-1]["ts"]
        return n_open

    def _close_due_positions(self, sym: str, candles: List[dict]) -> int:
        """Clôt les positions dont la bougie de sortie est clôturée."""
        px_by_ts = {c["ts"]: c["close"] for c in candles}
        last_ts = candles[-1]["ts"]
        still, n_closed = [], 0
        for pos in self.state["open"]:
            if pos["sym"] != sym:
                still.append(pos)
                continue
            exit_px = px_by_ts.get(pos["exit_ts"])
            if exit_px is None:
                # bougie de sortie pas encore clôturée — ou trou de données :
                # au-delà de la fenêtre fetchée, on clôt au dernier close connu
                if pos["exit_ts"] < last_ts - HOUR_MS:
                    exit_px = candles[-1]["close"]
                    logger.warning("[RSIMR-PAPER] %s: bougie de sortie absente, "
                                   "clôture au dernier close", sym)
                else:
                    still.append(pos)
                    continue
            gross = (exit_px - pos["entry_px"]) / pos["entry_px"]
            net = gross - 2 * FEE_SIDE
            pnl = NOTIONAL * net
            self.state["realized_usd"] += pnl
            self.state["fees_paid"] += NOTIONAL * 2 * FEE_SIDE
            self.state["n_trades"] += 1
            self.state["n_wins"] += 1 if net > 0 else 0
            self.state["sum_net_bps"] += 1e4 * net
            n_closed += 1
            self._log_trade({
                "closed_at": int(time.time()),
                "sym": sym,
                "entry_ts": pos["entry_ts"], "entry_px": pos["entry_px"],
                "exit_ts": pos["exit_ts"], "exit_px": exit_px,
                "gross_bps": round(1e4 * gross, 2),
                "net_bps": round(1e4 * net, 2),
                "pnl_usd": round(pnl, 4),
            })
        self.state["open"] = still
        return n_closed

"""
XSMomentumPaperTrader — momentum cross-sectionnel en PAPER TRADING pur.

Config validée en backtest le 2026-07-23 (833 j de 4h, univers anti-survivance
38 symboles cotés ≥815 j, frais maker inclus, funding vérifié négligeable) :

  - score(sym) = ret14j / vol20j   (rendement 14 j ÷ écart-type des
    log-rendements quotidiens sur 20 j) — t=+2.39, +9.6 bps/j de book,
    trois tiers de période positifs (+8.1/+11.8/+9.0), maxDD ~-23 %.
  - LONG les 8 meilleurs scores, SHORT les 8 pires, equal-weight ;
  - portefeuille en 7 TRANCHES chevauchantes : chaque jour UTC, une seule
    tranche (day % 7) est re-rankée — c'est la version « sans choix du jour
    de rebalance » qui a résisté au test d'offset (l'edge hebdo à date fixe
    n'y avait pas résisté : t de +0.21 à +2.67 selon le jour).

⚠️ Statut scientifique : MEILLEUR CANDIDAT, PAS UNE PREUVE (t=2.39 après
sélection ~15 configs). Ce moteur paper est le juge. Critère de jugement
suggéré (à ~8 semaines, mi-septembre 2026) : PnL net > 0 et comportement
cohérent avec le backtest (ordre de grandeur +5-10 bps/j, DD < 25 %).

Principes non négociables (hérités de momentum.py / incidents passés) :
  1. Paramètres FIGÉS — aucun optimiseur, aucun re-tuning en cours de test.
  2. PAPER ONLY — cette classe ne détient AUCUN client d'exchange.
  3. Tests → état isolé via XSMOM_STATE_FILE (jamais l'état réel).

Comptabilité :
  - prix = close des bougies 1d Hyperliquid (mark quotidien) ;
  - frais maker 1.5 bps par côté sur chaque CHANGEMENT de position ;
  - funding accru par heure de tenue au taux courant HL (mesuré ±2 bps/sem
    sur cette stratégie — compté quand même) ;
  - gross visé = equity (7 tranches × 16 positions ≈ equity/112 chacune).

État persisté (JSON atomique) : xsmom/state/xsmom_state.json
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from simplebot.data import fetch_funding_rates, fetch_ohlcv, fetch_perp_universe

logger = logging.getLogger("sdm.xsmom")

# ── Paramètres FIGÉS (config validée — ne pas optimiser) ─────────────────────
RET_DAYS = 14          # lookback momentum
VOL_DAYS = 20          # lookback volatilité (log-returns quotidiens)
N_LEG = 8              # longs et shorts par tranche
N_TRANCHES = 7         # tenue effective 7 j, une tranche re-rankée par jour
TOP_N_UNIVERSE = 40    # univers = top 40 par volume 24h
MIN_HISTORY_DAYS = 40  # il faut ret14 + vol20 complets
FEE_SIDE = 0.00015     # maker 1.5 bps par côté
PAPER_EQUITY0 = 1000.0
FETCH_THROTTLE_SEC = 0.35
DAY_MS = 86_400_000

STATE_FILE = Path(os.environ.get(
    "XSMOM_STATE_FILE",
    str(Path(__file__).resolve().parent / "state" / "xsmom_state.json"),
))


def daily_closes(candles: List[dict]) -> List[dict]:
    """Bougies 1d CLÔTURÉES (ts + 24h <= maintenant), triées."""
    now_ms = int(time.time() * 1000)
    return [c for c in sorted(candles, key=lambda c: c["ts"])
            if c["ts"] + DAY_MS <= now_ms]


def score_symbol(closes: List[float]) -> Optional[float]:
    """ret14/vol20 sur la série des closes quotidiens (dernier = hier)."""
    need = max(RET_DAYS, VOL_DAYS + 1) + 1
    if len(closes) < need:
        return None
    ret = closes[-1] / closes[-1 - RET_DAYS] - 1.0
    logs = [math.log(closes[i] / closes[i - 1])
            for i in range(len(closes) - VOL_DAYS, len(closes))]
    mean = sum(logs) / len(logs)
    var = sum((x - mean) ** 2 for x in logs) / (len(logs) - 1)
    vol = math.sqrt(var)
    if vol <= 0:
        return None
    return ret / vol


class XSMomentumPaperTrader:
    """Une décision par jour UTC ; mark-to-market et funding à chaque sweep."""

    def __init__(
        self,
        fetch: Optional[Callable[..., List[dict]]] = None,
        funding_fetch: Optional[Callable[[], Dict[str, float]]] = None,
        universe_fetch: Optional[Callable[..., List[str]]] = None,
        state_file: Optional[Path] = None,
    ):
        self._fetch = fetch or fetch_ohlcv
        self._funding_fetch = funding_fetch or fetch_funding_rates
        self._universe_fetch = universe_fetch or fetch_perp_universe
        self.state_file = Path(state_file) if state_file else STATE_FILE
        self.state = self._load_state()

    # ── État ────────────────────────────────────────────────────────────────
    def _load_state(self) -> dict:
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            st = {}
        st.setdefault("started_at", time.time())
        st.setdefault("equity", PAPER_EQUITY0)
        st.setdefault("equity_history", [])
        # tranches[i] = {sym: {"dir", "entry", "notional", "funding_pct", "funding_ts"}}
        st.setdefault("tranches", [{} for _ in range(N_TRANCHES)])
        st.setdefault("last_rebalance_day", None)
        st.setdefault("fees_paid", 0.0)
        st.setdefault("funding_net", 0.0)
        st.setdefault("rebalances", [])
        return st

    def _save_state(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=1)
            os.replace(tmp, self.state_file)
        except Exception as e:
            logger.warning("Sauvegarde xsmom_state échouée: %r", e)

    # ── Données ─────────────────────────────────────────────────────────────
    def _universe(self) -> List[str]:
        try:
            return list(self._universe_fetch(top_n=TOP_N_UNIVERSE))
        except Exception as e:
            logger.warning("Univers illisible (%r) — univers des positions", e)
            return sorted({s for tr in self.state["tranches"] for s in tr})

    def _closes(self, sym: str) -> List[dict]:
        try:
            cs = daily_closes(self._fetch(sym, "1d", MIN_HISTORY_DAYS + 3))
        except Exception as e:
            logger.warning("fetch 1d %s: %r", sym, e)
            return []
        return cs

    # ── Comptabilité ────────────────────────────────────────────────────────
    def _accrue_funding(self, pos: dict, rate: float, now_ms: int) -> None:
        hours = int((now_ms - pos["funding_ts"]) // 3_600_000)
        if hours <= 0:
            return
        pos["funding_pct"] = pos.get("funding_pct", 0.0) - pos["dir"] * rate * hours
        pos["funding_ts"] += hours * 3_600_000

    def _close_position(self, sym: str, pos: dict, px: float) -> float:
        """PnL $ d'une position fermée à px (frais du côté sortie inclus)."""
        pnl_pct = (pos["dir"] * (px - pos["entry"]) / pos["entry"]
                   + pos.get("funding_pct", 0.0) - FEE_SIDE)
        self.state["funding_net"] += pos["notional"] * pos.get("funding_pct", 0.0)
        self.state["fees_paid"] += pos["notional"] * FEE_SIDE
        return pos["notional"] * pnl_pct

    # ── Cœur : rebalance quotidien d'UNE tranche ────────────────────────────
    def rebalance_if_due(self, now: Optional[float] = None) -> bool:
        now = now or time.time()
        day = int(now // 86_400)
        if self.state["last_rebalance_day"] == day:
            return False

        universe = self._universe()
        closes_map: Dict[str, List[dict]] = {}
        scores: Dict[str, float] = {}
        for i, sym in enumerate(universe):
            if i and FETCH_THROTTLE_SEC:
                time.sleep(FETCH_THROTTLE_SEC)
            cs = self._closes(sym)
            if len(cs) < MIN_HISTORY_DAYS:
                continue
            closes_map[sym] = cs
            sc = score_symbol([c["close"] for c in cs])
            if sc is not None:
                scores[sym] = sc
        if len(scores) < N_LEG * 3:
            logger.warning("Rebalance sauté — univers scoreable trop petit (%d)", len(scores))
            self.state["last_rebalance_day"] = day   # on ne re-tente pas dans la journée
            self._save_state()
            return False

        # prix de référence du jour = dernier close 1d
        px_now = {s: closes_map[s][-1]["close"] for s in closes_map}

        # 1) funding + mark de TOUTES les positions (equity vivante)
        try:
            rates = self._funding_fetch()
        except Exception as e:
            logger.warning("funding illisible (%r) — accrual différé", e)
            rates = {}
        now_ms = int(now * 1000)
        for tr in self.state["tranches"]:
            for sym, pos in tr.items():
                self._accrue_funding(pos, rates.get(sym, 0.0), now_ms)

        # 2) remplacer la tranche du jour
        k = day % N_TRANCHES
        old = self.state["tranches"][k]
        realized = 0.0
        for sym, pos in old.items():
            px = px_now.get(sym)
            if px is None:
                cs = self._closes(sym)
                px = cs[-1]["close"] if cs else pos["entry"]
            realized += self._close_position(sym, pos, px)

        ranked = sorted(scores, key=scores.get)
        longs = ranked[-N_LEG:]
        shorts = ranked[:N_LEG]
        tranche_notional = self.state["equity"] / N_TRANCHES
        per_pos = tranche_notional / (2 * N_LEG)
        new_tr: Dict[str, dict] = {}
        for sym, d in [(s, 1) for s in longs] + [(s, -1) for s in shorts]:
            px = px_now[sym]
            self.state["fees_paid"] += per_pos * FEE_SIDE
            new_tr[sym] = {
                "dir": d, "entry": px, "notional": per_pos,
                "funding_pct": 0.0,
                "funding_ts": now_ms,
            }
        entry_fees = per_pos * FEE_SIDE * len(new_tr)
        self.state["equity"] += realized - entry_fees
        self.state["tranches"][k] = new_tr
        self.state["last_rebalance_day"] = day
        self.state["rebalances"].append({
            "day": day, "tranche": k, "longs": longs, "shorts": shorts,
            "realized_usd": round(realized, 4), "equity": round(self.state["equity"], 4),
        })
        logger.info(
            "[XSMOM-PAPER] rebalance j=%d tranche=%d | realized %+0.2f$ | equity %.2f$ "
            "| L=%s | S=%s", day, k, realized, self.state["equity"],
            ",".join(longs), ",".join(shorts),
        )
        self._mark_equity(px_now, now)
        self._save_state()
        return True

    def _mark_equity(self, px_now: Dict[str, float], now: float) -> None:
        """Equity mark-to-market (positions ouvertes valorisées au dernier close)."""
        unreal = 0.0
        for tr in self.state["tranches"]:
            for sym, pos in tr.items():
                px = px_now.get(sym)
                if px is None:
                    continue
                unreal += pos["notional"] * (
                    pos["dir"] * (px - pos["entry"]) / pos["entry"]
                    + pos.get("funding_pct", 0.0))
        hist = self.state["equity_history"]
        hist.append([now, round(self.state["equity"] + unreal, 4)])
        if len(hist) > 20000:
            del hist[:-20000]

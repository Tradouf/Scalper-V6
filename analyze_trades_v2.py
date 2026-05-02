#!/usr/bin/env python3
"""
analyze_trades_v2.py — Analyseur de trades basé sur les logs.

Reconstruit chaque trade à partir des logs sdm.log* :
  - Entry context (prix, conf, bull/tech/bear_risk, régime)
  - Série trail (ROE, best, protected, armed, toutes les 2s)
  - Exit (ts, reason)

Produit :
  - Stats globales (WR, PF, EV, max DD)
  - WR par bucket de confiance
  - WR par régime
  - WR par exit_reason
  - WR par heure UTC
  - Trail efficiency : avg(max_roe - exit_roe)
  - Per-symbole × régime

Usage :
  python analyze_trades_v2.py [logs/sdm.log* ...]   # par défaut: tous les logs/sdm.log*
  python analyze_trades_v2.py --csv report.csv      # exporte les trades détaillés
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import mean, median
from typing import Dict, List, Optional, Tuple


# ============================================================
# Patterns
# ============================================================

TS_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\s+\[")

CONSENSUS_RE = re.compile(
    r"CONSENSUS\s+(\S+)\s+\|\s+side=(\w+)\s+conf=([\d.]+)\s+\|\s+"
    r"bull=([\d.]+)\s+tech=([\d.]+)\s+bear_risk=(\w+)"
)

REGIME_RE = re.compile(
    r"REGIME PIPELINE\s+(\S+)\s+trend=(\w+)\s+vol=(\w+)\s+risk=(\w+)"
)

ENTER_RE = re.compile(
    r"\[LIVE\](?:\[FLIP\])?\s+ENTER\s+(BUY|SELL)\s+(\S+)\s+\|\s+"
    r"entry=([\d.]+)\s+sl=([\d.]+)\s+tp=([\d.]+)\s+\|\s+"
    r"bull=([\d.]+)\s+tech=([\d.]+)\s+bear_risk=(\w+)"
)

GUARD_RE = re.compile(
    r"TRAIL GUARD enregistré\s+(BUY|SELL)\s+(\S+)\s+\|\s+entry=([\d.]+)\s+lev=([\d.]+)x"
)

TRAIL_RE = re.compile(
    r"TRAIL\s+(BUY|SELL)\s+(\S+)\s+ROE=(-?[\d.]+)%\s+best=(-?[\d.]+)%\s+protected=(-?[\d.]+)%\s+armed=(\w+)"
)

COOLDOWN_RE = re.compile(
    r"\[COOLDOWN\]\s+(\S+)\s+exit marqué\s+\(([^)]+)\)"
)

FLIP_RE = re.compile(
    r"\[FLIP\]\s+(\S+)\s+(BUY|SELL)→(BUY|SELL)\s+\|\s+PnL estimé=(-?[\d.]+)%"
)

STATS_RE = re.compile(
    r"Stats cycle:\s+analyzed=(\d+)\s+skipped=(\d+)\s+entered=(\d+)\s+flipped=(\d+)\s+open=(\d+)\s+trail_guards=(\d+)"
)


# ============================================================
# Data model
# ============================================================

@dataclass
class TrailTick:
    ts: datetime
    roe: float          # current ROE %
    best: float         # peak ROE %
    protected: float    # current trailing stop level (ROE %)
    armed: bool


@dataclass
class Trade:
    entry_ts: datetime
    symbol: str
    side: str
    entry: float
    sl: float
    tp: float
    leverage: float = 6.0

    # Decision context
    conf: float = 0.0
    bull_conf: float = 0.0
    tech_conf: float = 0.0
    bear_risk: str = "?"

    # Regime at entry
    regime_trend: str = "?"
    regime_vol: str = "?"
    regime_risk: str = "?"

    # Concurrent positions at entry
    concurrent_open: int = 0

    # Trail
    trail: List[TrailTick] = field(default_factory=list)

    # Exit
    exit_ts: Optional[datetime] = None
    exit_reason: Optional[str] = None  # external_exit / agent_close / flip / open
    # Si exit via flip : PnL prix en % (avant levier), tel que loggué par _handle_flip
    flip_pnl_price_pct: Optional[float] = None

    @property
    def max_roe(self) -> float:
        return max((t.best for t in self.trail), default=0.0)

    @property
    def final_roe(self) -> float:
        return self.trail[-1].roe if self.trail else 0.0

    @property
    def armed_at_exit(self) -> bool:
        return self.trail[-1].armed if self.trail else False

    @property
    def protected_at_exit(self) -> float:
        return self.trail[-1].protected if self.trail else 0.0

    @property
    def duration_sec(self) -> float:
        if not self.exit_ts:
            return 0.0
        return (self.exit_ts - self.entry_ts).total_seconds()

    @property
    def hour_utc(self) -> int:
        return self.entry_ts.hour

    @property
    def is_win(self) -> Optional[bool]:
        # On utilise la valeur "protected" si armé (le SL natif a normalement déclenché là),
        # sinon final_roe (perte ou gain non armé).
        # Pour les trades fermés via flip, on lit flip_pnl_pct.
        if self.exit_reason == "open":
            return None
        if self.exit_reason == "flip" and self.flip_pnl_price_pct is not None:
            return self.flip_pnl_price_pct > 0
        if self.armed_at_exit:
            return self.protected_at_exit > 0
        return self.final_roe > 0

    @property
    def realized_roe(self) -> float:
        """Approx ROE réalisée à la sortie (en %)."""
        if self.exit_reason == "open":
            return 0.0
        if self.exit_reason == "flip" and self.flip_pnl_price_pct is not None:
            return self.flip_pnl_price_pct * self.leverage
        if self.armed_at_exit:
            return self.protected_at_exit
        return self.final_roe

    @property
    def trail_efficiency_loss(self) -> float:
        """Combien de ROE laissé sur la table : max_roe - realized_roe."""
        return max(0.0, self.max_roe - self.realized_roe)


# ============================================================
# Date-aware log iterator
# ============================================================

def iter_log_lines(paths: List[str]):
    """
    Itère les lignes des logs en ordre chronologique, en attribuant
    une date à chaque ligne (les logs n'ont que HH:MM:SS).

    Stratégie : pour chaque fichier, on utilise sa mtime comme ancre
    (≈ ts de la dernière ligne) et on remonte en détectant les wraps
    de minuit (quand HH décroît → on passe au jour précédent).
    """
    # On traite chaque fichier indépendamment puis on fusionne
    all_dated = []
    for path in paths:
        if not os.path.isfile(path):
            continue
        mtime = datetime.fromtimestamp(os.path.getmtime(path))
        # Charger toutes les lignes du fichier
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if not lines:
            continue
        # Extraire les ts (HH:MM:SS) de chaque ligne
        parsed = []
        for ln in lines:
            m = TS_RE.match(ln)
            if m:
                parsed.append((int(m.group(1)), int(m.group(2)), int(m.group(3)), ln))
            else:
                parsed.append(None)  # ligne sans ts (continuation)
        # Trouver la dernière ligne avec ts pour anchorer
        last_idx = max((i for i, p in enumerate(parsed) if p is not None), default=-1)
        if last_idx < 0:
            continue
        last_h, last_m, last_s, _ = parsed[last_idx]
        anchor_date = mtime.date()
        # Si l'anchor (mtime) heure est avant la dernière ligne, mtime est en réalité
        # le lendemain. Mais en pratique mtime ≈ dernière ligne, donc on prend mtime.date().
        # Walk backward, en détectant les wraps
        dated = [None] * len(parsed)
        cur_date = anchor_date
        prev_h = last_h
        for i in range(last_idx, -1, -1):
            p = parsed[i]
            if p is None:
                if i + 1 < len(dated) and dated[i + 1] is not None:
                    dated[i] = (dated[i + 1][0], parsed[i + 1] and parsed[i + 1][3] or "")
                continue
            h, mn, s, ln = p
            # Si on remonte et que h > prev_h, on a wrappé (passé minuit en arrière)
            if h > prev_h:
                cur_date -= timedelta(days=1)
            prev_h = h
            ts = datetime(cur_date.year, cur_date.month, cur_date.day, h, mn, s)
            dated[i] = (ts, ln)
        # Forward fill pour les lignes sans ts (continuation)
        last_ts = None
        for i, d in enumerate(dated):
            if d is None and last_ts is not None:
                dated[i] = (last_ts, parsed[i] if isinstance(parsed[i], str) else "")
            elif d is not None:
                last_ts = d[0]
        all_dated.extend(d for d in dated if d is not None)

    # Trier global
    all_dated.sort(key=lambda x: x[0])
    return all_dated


# ============================================================
# Trade builder (state machine)
# ============================================================

class TradeBuilder:
    def __init__(self):
        self.open_trades: Dict[str, Trade] = {}
        self.closed: List[Trade] = []
        # Décisions/régimes récents par symbole (utilisés au moment de l'ENTER)
        self.last_consensus: Dict[str, dict] = {}
        self.last_regime: Dict[str, dict] = {}
        # Levier par symbole (capturé via TRAIL GUARD)
        self.leverage_by_sym: Dict[str, float] = {}
        # Concurrent open count (capturé via Stats cycle)
        self.last_open_count: int = 0

    def feed(self, ts: datetime, line: str) -> None:
        # 1. CONSENSUS
        m = CONSENSUS_RE.search(line)
        if m:
            sym, side, conf, bull, tech, bear = m.groups()
            self.last_consensus[sym] = {
                "ts": ts, "side": side, "conf": float(conf),
                "bull": float(bull), "tech": float(tech), "bear_risk": bear,
            }
            return

        # 2. REGIME PIPELINE
        m = REGIME_RE.search(line)
        if m:
            sym, trend, vol, risk = m.groups()
            self.last_regime[sym] = {
                "ts": ts, "trend": trend, "vol": vol, "risk": risk,
            }
            return

        # 3. Stats cycle (track concurrent open)
        m = STATS_RE.search(line)
        if m:
            self.last_open_count = int(m.group(5))
            return

        # 4. TRAIL GUARD enregistré (capture leverage)
        m = GUARD_RE.search(line)
        if m:
            side, sym, entry, lev = m.groups()
            self.leverage_by_sym[sym] = float(lev)
            return

        # 5. ENTER (création du trade)
        m = ENTER_RE.search(line)
        if m:
            side_raw, sym, entry, sl, tp, bull, tech, bear = m.groups()
            cons = self.last_consensus.get(sym, {})
            regime = self.last_regime.get(sym, {})
            t = Trade(
                entry_ts=ts,
                symbol=sym,
                side=side_raw.lower(),
                entry=float(entry),
                sl=float(sl),
                tp=float(tp),
                leverage=self.leverage_by_sym.get(sym, 6.0),
                conf=cons.get("conf", 0.0),
                bull_conf=float(bull),
                tech_conf=float(tech),
                bear_risk=bear,
                regime_trend=regime.get("trend", "?"),
                regime_vol=regime.get("vol", "?"),
                regime_risk=regime.get("risk", "?"),
                concurrent_open=self.last_open_count,
            )
            # Si un trade était déjà ouvert sur ce symbole (cas FLIP non capturé),
            # on le ferme proprement comme "flip"
            if sym in self.open_trades:
                old = self.open_trades.pop(sym)
                old.exit_ts = ts
                old.exit_reason = "flip"
                self.closed.append(old)
            self.open_trades[sym] = t
            return

        # 6. TRAIL tick
        m = TRAIL_RE.search(line)
        if m:
            side, sym, roe, best, prot, armed = m.groups()
            t = self.open_trades.get(sym)
            if t is not None:
                t.trail.append(TrailTick(
                    ts=ts,
                    roe=float(roe),
                    best=float(best),
                    protected=float(prot),
                    armed=(armed == "True"),
                ))
            return

        # 7. FLIP (capture pnl du trade qu'on ferme)
        m = FLIP_RE.search(line)
        if m:
            sym, old_side, new_side, pnl = m.groups()
            t = self.open_trades.get(sym)
            if t is not None and t.side == old_side.lower():
                # Le flip va fermer ce trade. Le pnl loggué est en % prix (avant levier).
                t.flip_pnl_price_pct = float(pnl)
            return

        # 8. COOLDOWN exit (clôture le trade)
        m = COOLDOWN_RE.search(line)
        if m:
            sym, reason = m.groups()
            t = self.open_trades.pop(sym, None)
            if t is not None:
                t.exit_ts = ts
                t.exit_reason = reason
                self.closed.append(t)
            return

    def finalize(self) -> List[Trade]:
        # Marquer les trades restés ouverts
        for sym, t in self.open_trades.items():
            t.exit_reason = "open"
        result = list(self.closed) + list(self.open_trades.values())
        result.sort(key=lambda x: x.entry_ts)
        return result


# ============================================================
# Analyses
# ============================================================

def fmt_pct(x: float, digits: int = 2) -> str:
    return f"{x:+.{digits}f}%"


def aggregate_basic(trades: List[Trade]) -> dict:
    closed = [t for t in trades if t.exit_reason != "open"]
    if not closed:
        return {}
    rois = [t.realized_roe for t in closed]
    wins = [r for r in rois if r > 0]
    losses = [r for r in rois if r < 0]
    avg_win = mean(wins) if wins else 0.0
    avg_loss = mean(losses) if losses else 0.0
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf") if wins else 0.0
    wr = len(wins) / len(closed) if closed else 0.0
    ev = mean(rois) if rois else 0.0
    # Max drawdown sur courbe cumulée
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in rois:
        cum += r
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    return {
        "trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": wr,
        "ev_roe": ev,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "pf": pf,
        "total_roe": sum(rois),
        "max_dd_roe": max_dd,
    }


def by_bucket(trades: List[Trade], key_fn, bucket_label_fn) -> List[dict]:
    """Groupe par clé, retourne une liste de stats."""
    by = defaultdict(list)
    for t in trades:
        if t.exit_reason == "open":
            continue
        by[key_fn(t)].append(t)
    out = []
    for k, group in by.items():
        s = aggregate_basic(group)
        if not s:
            continue
        s["bucket"] = bucket_label_fn(k)
        s["sort_key"] = k if isinstance(k, (int, float, str)) else str(k)
        out.append(s)
    return out


def conf_bucket(t: Trade) -> str:
    c = t.conf
    if c < 0.60:
        return "<0.60"
    if c < 0.65:
        return "0.60–0.65"
    if c < 0.70:
        return "0.65–0.70"
    if c < 0.75:
        return "0.70–0.75"
    if c < 0.80:
        return "0.75–0.80"
    if c < 0.85:
        return "0.80–0.85"
    if c < 0.90:
        return "0.85–0.90"
    return "0.90+"


def hour_bucket(t: Trade) -> str:
    h = t.hour_utc
    return f"{h:02d}h"


def regime_bucket(t: Trade) -> str:
    return f"{t.regime_trend}/{t.regime_vol}/{t.regime_risk}"


def concur_bucket(t: Trade) -> str:
    n = t.concurrent_open
    if n <= 1:
        return "1"
    if n <= 2:
        return "2"
    if n <= 3:
        return "3"
    if n <= 4:
        return "4"
    return "5+"


# ============================================================
# Reporting
# ============================================================

def print_section(title: str):
    print()
    print("═" * 78)
    print(f"  {title}")
    print("═" * 78)


def print_basic(stats: dict, label: str = "GLOBAL"):
    if not stats:
        print(f"  {label}: aucun trade fermé.")
        return
    print(
        f"  {label:<24} trades={stats['trades']:>3}  WR={stats['winrate']*100:>5.1f}%  "
        f"EV={fmt_pct(stats['ev_roe']):>7}  PF={stats['pf']:>5.2f}  "
        f"avg_win={fmt_pct(stats['avg_win']):>7}  avg_loss={fmt_pct(stats['avg_loss']):>7}  "
        f"total={fmt_pct(stats['total_roe']):>8}  maxDD={stats['max_dd_roe']:.2f}%"
    )


def print_buckets(rows: List[dict], header: str, sort_by: str = "sort_key"):
    if not rows:
        return
    print(f"  {'Bucket':<22} {'N':>4} {'WR%':>6} {'EV%':>8} {'PF':>5} {'avgWin':>7} {'avgLoss':>8} {'totROE':>8}")
    print(f"  {'-'*22} {'-'*4} {'-'*6} {'-'*8} {'-'*5} {'-'*7} {'-'*8} {'-'*8}")
    rows_sorted = sorted(rows, key=lambda r: r.get(sort_by, ""))
    for r in rows_sorted:
        print(
            f"  {r['bucket']:<22} {r['trades']:>4} "
            f"{r['winrate']*100:>6.1f} {r['ev_roe']:>+8.3f} "
            f"{r['pf']:>5.2f} {r['avg_win']:>+7.3f} {r['avg_loss']:>+8.3f} "
            f"{r['total_roe']:>+8.2f}"
        )


def print_trail_efficiency(trades: List[Trade]):
    closed = [t for t in trades if t.exit_reason != "open" and t.trail]
    if not closed:
        return
    losses = [t.trail_efficiency_loss for t in closed]
    armed = [t for t in closed if any(tk.armed for tk in t.trail)]
    print(f"  Trades avec données trail : {len(closed)}")
    print(f"  Trades dont trail s'est armé : {len(armed)} ({len(armed)/len(closed)*100:.1f}%)")
    print(f"  Avg ROE laissé sur la table : {mean(losses):+.3f}%")
    print(f"  Median                      : {median(losses):+.3f}%")
    print(f"  Max                         : {max(losses):+.3f}%")
    armed_eff = [t for t in armed if t.max_roe > 0]
    if armed_eff:
        captured = mean([t.realized_roe / t.max_roe for t in armed_eff if t.max_roe > 0])
        print(f"  Capture ratio (realized/max) sur trades armés : {captured*100:.1f}%")


def print_per_symbol_regime(trades: List[Trade]):
    by = defaultdict(list)
    for t in trades:
        if t.exit_reason == "open":
            continue
        key = (t.symbol, t.regime_trend, t.regime_vol)
        by[key].append(t)
    rows = []
    for (sym, trend, vol), group in by.items():
        s = aggregate_basic(group)
        if not s or s["trades"] < 2:
            continue
        s["sym"] = sym
        s["regime"] = f"{trend}/{vol}"
        rows.append(s)
    rows.sort(key=lambda r: r["ev_roe"])
    if not rows:
        return
    print(f"  {'Sym':<6} {'Régime':<14} {'N':>4} {'WR%':>6} {'EV%':>8} {'totROE':>8}")
    print(f"  {'-'*6} {'-'*14} {'-'*4} {'-'*6} {'-'*8} {'-'*8}")
    for r in rows:
        print(
            f"  {r['sym']:<6} {r['regime']:<14} {r['trades']:>4} "
            f"{r['winrate']*100:>6.1f} {r['ev_roe']:>+8.3f} {r['total_roe']:>+8.2f}"
        )


def print_cross_exit_conf(trades: List[Trade]):
    """Croise exit_reason × bucket de confiance pour identifier les couples perdants."""
    by = defaultdict(list)
    for t in trades:
        if t.exit_reason == "open":
            continue
        key = (t.exit_reason, conf_bucket(t))
        by[key].append(t)
    rows = []
    for (reason, bucket), group in by.items():
        s = aggregate_basic(group)
        if not s:
            continue
        s["bucket"] = f"{reason} × {bucket}"
        s["sort_key"] = (reason, bucket)
        rows.append(s)
    rows.sort(key=lambda r: r["sort_key"])
    print(f"  {'exit × conf':<26} {'N':>4} {'WR%':>6} {'EV%':>8} {'totROE':>8}")
    print(f"  {'-'*26} {'-'*4} {'-'*6} {'-'*8} {'-'*8}")
    for r in rows:
        flag = " ⚠" if r["ev_roe"] < -0.3 and r["trades"] >= 5 else ""
        print(
            f"  {r['bucket']:<26} {r['trades']:>4} "
            f"{r['winrate']*100:>6.1f} {r['ev_roe']:>+8.3f} {r['total_roe']:>+8.2f}{flag}"
        )


def simulate_filter(trades: List[Trade], filter_fn, label: str) -> dict:
    """Recalcule l'EV/PnL si on avait skippé les trades matching filter_fn."""
    closed = [t for t in trades if t.exit_reason != "open"]
    kept = [t for t in closed if not filter_fn(t)]
    skipped = [t for t in closed if filter_fn(t)]
    s_keep = aggregate_basic(kept)
    s_skip = aggregate_basic(skipped)
    return {
        "label": label,
        "n_skipped": len(skipped),
        "n_kept": len(kept),
        "skipped_total_roe": s_skip.get("total_roe", 0.0) if skipped else 0.0,
        "skipped_ev": s_skip.get("ev_roe", 0.0) if skipped else 0.0,
        "kept_total_roe": s_keep.get("total_roe", 0.0) if kept else 0.0,
        "kept_ev": s_keep.get("ev_roe", 0.0) if kept else 0.0,
        "kept_pf": s_keep.get("pf", 0.0) if kept else 0.0,
        "kept_wr": s_keep.get("winrate", 0.0) if kept else 0.0,
    }


def print_filter_simulations(trades: List[Trade]):
    """Évalue plusieurs filtres candidats."""
    baseline = aggregate_basic([t for t in trades if t.exit_reason != "open"])
    print(f"  Baseline   : {baseline['trades']} trades, EV={fmt_pct(baseline['ev_roe'])}, "
          f"PF={baseline['pf']:.2f}, total={fmt_pct(baseline['total_roe'])}")
    print()

    candidates = [
        ("Skip flips", lambda t: t.exit_reason == "flip"),
        ("Skip conf < 0.75", lambda t: t.conf < 0.75),
        ("Skip conf < 0.80", lambda t: t.conf < 0.80),
        ("Skip conf < 0.85", lambda t: t.conf < 0.85),
        ("Skip ATOM", lambda t: t.symbol == "ATOM"),
        ("Skip 13h-14h UTC", lambda t: t.hour_utc in (13, 14)),
        ("Skip 18h-22h UTC", lambda t: 18 <= t.hour_utc <= 22),
        ("Skip range/low/low", lambda t: (t.regime_trend, t.regime_vol, t.regime_risk) == ("range", "low", "low")),
        ("Skip flips OR conf<0.80", lambda t: t.exit_reason == "flip" or t.conf < 0.80),
    ]
    print(f"  {'Filtre candidat':<28} {'N kept':>7} {'kept EV':>9} {'kept PF':>8} {'kept tot':>9} {'gain vs base':>13}")
    print(f"  {'-'*28} {'-'*7} {'-'*9} {'-'*8} {'-'*9} {'-'*13}")
    for label, fn in candidates:
        sim = simulate_filter(trades, fn, label)
        gain = sim["kept_total_roe"] - baseline["total_roe"]
        flag = " ✓" if gain > 0 else ""
        print(
            f"  {sim['label']:<28} {sim['n_kept']:>7} "
            f"{sim['kept_ev']:>+9.3f} {sim['kept_pf']:>8.2f} "
            f"{sim['kept_total_roe']:>+9.2f} {gain:>+13.2f}{flag}"
        )


def export_csv(trades: List[Trade], path: str):
    closed = [t for t in trades if t.exit_reason != "open"]
    if not closed:
        return
    fields = [
        "entry_ts", "symbol", "side", "entry", "sl", "tp", "leverage",
        "conf", "bull_conf", "tech_conf", "bear_risk",
        "regime_trend", "regime_vol", "regime_risk",
        "concurrent_open",
        "exit_ts", "exit_reason",
        "max_roe", "final_roe", "armed_at_exit", "protected_at_exit",
        "realized_roe", "trail_efficiency_loss",
        "duration_sec", "hour_utc", "is_win",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in closed:
            w.writerow({
                "entry_ts": t.entry_ts.isoformat(),
                "symbol": t.symbol,
                "side": t.side,
                "entry": t.entry,
                "sl": t.sl,
                "tp": t.tp,
                "leverage": t.leverage,
                "conf": t.conf,
                "bull_conf": t.bull_conf,
                "tech_conf": t.tech_conf,
                "bear_risk": t.bear_risk,
                "regime_trend": t.regime_trend,
                "regime_vol": t.regime_vol,
                "regime_risk": t.regime_risk,
                "concurrent_open": t.concurrent_open,
                "exit_ts": t.exit_ts.isoformat() if t.exit_ts else "",
                "exit_reason": t.exit_reason,
                "max_roe": round(t.max_roe, 4),
                "final_roe": round(t.final_roe, 4),
                "armed_at_exit": t.armed_at_exit,
                "protected_at_exit": round(t.protected_at_exit, 4),
                "realized_roe": round(t.realized_roe, 4),
                "trail_efficiency_loss": round(t.trail_efficiency_loss, 4),
                "duration_sec": round(t.duration_sec, 0),
                "hour_utc": t.hour_utc,
                "is_win": t.is_win,
            })


# ============================================================
# Main
# ============================================================

def main():
    p = argparse.ArgumentParser(description="Analyseur de trades log-based")
    p.add_argument("logs", nargs="*", help="Fichiers logs (défaut: logs/sdm.log*)")
    p.add_argument("--csv", help="Exporte les trades détaillés en CSV")
    args = p.parse_args()

    paths = args.logs or sorted(glob.glob("logs/sdm.log*"))
    if not paths:
        print("Aucun fichier log trouvé.")
        sys.exit(1)

    print(f"Logs analysés ({len(paths)}) : {[os.path.basename(p) for p in paths]}")

    # Parse
    builder = TradeBuilder()
    n_lines = 0
    for ts, line in iter_log_lines(paths):
        builder.feed(ts, line)
        n_lines += 1
    trades = builder.finalize()

    print(f"Lignes parsées       : {n_lines:,}")
    print(f"Trades extraits      : {len(trades)}")
    closed = [t for t in trades if t.exit_reason != "open"]
    print(f"Trades fermés        : {len(closed)}")
    print(f"Trades encore ouverts : {len(trades) - len(closed)}")

    if not closed:
        print("Pas assez de données pour analyse.")
        return

    # Période
    first_ts = min(t.entry_ts for t in closed)
    last_ts = max(t.exit_ts for t in closed if t.exit_ts)
    print(f"Période              : {first_ts.isoformat(timespec='minutes')} → {last_ts.isoformat(timespec='minutes')}")

    # === STATS GLOBALES ===
    print_section("STATS GLOBALES")
    print_basic(aggregate_basic(closed))

    # === PAR EXIT REASON ===
    print_section("PAR EXIT_REASON")
    rows = by_bucket(closed, lambda t: t.exit_reason or "?", lambda k: k)
    print_buckets(rows, "exit_reason")

    # === PAR CONFIANCE ===
    print_section("PAR BUCKET DE CONFIANCE")
    rows = by_bucket(closed, conf_bucket, lambda k: k)
    # Order by bucket name (string sort works because of the prefix)
    print_buckets(rows, "conf")

    # === PAR REGIME ===
    print_section("PAR RÉGIME (trend/vol/risk)")
    rows = by_bucket(closed, regime_bucket, lambda k: k)
    rows.sort(key=lambda r: r["ev_roe"])
    print_buckets(rows, "régime", sort_by="ev_roe")

    # === PAR HEURE UTC ===
    print_section("PAR HEURE UTC (heure d'ENTRÉE)")
    rows = by_bucket(closed, hour_bucket, lambda k: k)
    print_buckets(rows, "heure")

    # === PAR NB POSITIONS CONCURRENTES ===
    print_section("PAR NB POSITIONS CONCURRENTES (au moment de l'entry)")
    rows = by_bucket(closed, concur_bucket, lambda k: k)
    print_buckets(rows, "concur")

    # === PAR SYMBOLE ===
    print_section("PAR SYMBOLE")
    rows = by_bucket(closed, lambda t: t.symbol, lambda k: k)
    rows.sort(key=lambda r: r["ev_roe"])
    print_buckets(rows, "symbole", sort_by="ev_roe")

    # === SYMBOLE × RÉGIME ===
    print_section("SYMBOLE × RÉGIME (trend/vol)  — min 2 trades")
    print_per_symbol_regime(closed)

    # === EXIT_REASON × CONFIANCE ===
    print_section("CROSS : EXIT_REASON × CONFIANCE")
    print_cross_exit_conf(closed)

    # === TRAIL EFFICIENCY ===
    print_section("TRAIL EFFICIENCY")
    print_trail_efficiency(closed)

    # === SIMULATEUR DE FILTRES ===
    print_section("SIMULATEUR DE FILTRES — gain vs baseline si on skippe ces trades")
    print_filter_simulations(trades)

    # === RECOMMANDATIONS ===
    print_section("LECTURE RAPIDE / PISTES")
    g = aggregate_basic(closed)
    if g["ev_roe"] < 0:
        print(f"  ⚠ EV global négatif ({fmt_pct(g['ev_roe'])}). Sélectivité à augmenter.")
    if g["pf"] < 1.0:
        print(f"  ⚠ Profit Factor < 1 ({g['pf']:.2f}). Logique perdante sur l'échantillon.")
    # Best/worst conf bucket
    conf_rows = by_bucket(closed, conf_bucket, lambda k: k)
    if len(conf_rows) >= 2:
        best = max(conf_rows, key=lambda r: r["ev_roe"])
        worst = min(conf_rows, key=lambda r: r["ev_roe"])
        if best["ev_roe"] > worst["ev_roe"] + 0.5:
            print(f"  → Bucket conf {best['bucket']} domine ({fmt_pct(best['ev_roe'])}) vs {worst['bucket']} ({fmt_pct(worst['ev_roe'])}). "
                  f"Envisager MIN_CONFIDENCE >= {best['bucket'].split('–')[0] if '–' in best['bucket'] else best['bucket']}.")
    # Worst regime
    reg_rows = by_bucket(closed, regime_bucket, lambda k: k)
    reg_neg = [r for r in reg_rows if r["ev_roe"] < -0.5 and r["trades"] >= 5]
    if reg_neg:
        for r in reg_neg[:3]:
            print(f"  → Régime {r['bucket']} : {r['trades']} trades, EV {fmt_pct(r['ev_roe'])}. Candidat à exclure.")
    # Trail efficiency
    armed_trades = [t for t in closed if any(tk.armed for tk in t.trail)]
    if armed_trades:
        avg_loss = mean([t.trail_efficiency_loss for t in armed_trades])
        if avg_loss > 0.5:
            print(f"  → Trail laisse en moyenne {avg_loss:.2f}% ROE sur la table. "
                  f"Envisager TRAIL_DROP_PCT plus serré ou TRAIL_STEP_ROE plus fin.")

    # CSV export
    if args.csv:
        export_csv(trades, args.csv)
        print(f"\n  CSV exporté : {os.path.abspath(args.csv)}")


if __name__ == "__main__":
    main()

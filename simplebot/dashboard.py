#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dashboard SimpleBot — à l'image du dashboard V7, mais 100 % stdlib
(aucune dépendance : SimpleBot est volontairement minimal).

Sert sur http://localhost:8083 :
  - `/`            page HTML dark, auto-refresh 5 s ;
  - `/api/state`   JSON agrégé lu depuis les 3 fichiers d'état SimpleBot :
        simplebot/state/best_params.json        (résultats d'optimisation)
        simplebot/state/live_state.json          (equity, kill-switch, papier)
        simplebot/state/optimizer_history.jsonl  (historique des runs)

Rien n'est écrit, aucun wallet n'est requis : le dashboard est en lecture
seule et tourne à côté du bot (ou même bot arrêté, pour relire l'état).

Accès distant :
    Le serveur bind SIMPLEBOT_DASHBOARD_HOST (défaut 0.0.0.0 → accessible sur le
    LAN via http://<ip-machine>:8083). Pour une exposition hors LAN, protéger
    l'accès avec SIMPLEBOT_DASHBOARD_PASSWORD (Basic Auth) — sans mot de passe,
    le serveur REFUSE de binder autre chose que localhost/LAN privé et journalise
    un avertissement. Méthodes recommandées : tunnel SSH, Tailscale, cloudflared.

Usage :
    python -m simplebot.dashboard              # port 8083
    SIMPLEBOT_DASHBOARD_PORT=9000 python -m simplebot.dashboard
    SIMPLEBOT_DASHBOARD_PASSWORD=secret python -m simplebot.dashboard
"""
from __future__ import annotations

import hmac
import json
import os
import socket
import time
import urllib.request
from base64 import b64decode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from simplebot import config

PORT = int(os.environ.get("SIMPLEBOT_DASHBOARD_PORT", "8083"))
HOST = os.environ.get("SIMPLEBOT_DASHBOARD_HOST", "0.0.0.0")
AUTH_USER = os.environ.get("SIMPLEBOT_DASHBOARD_USER", "simplebot")
AUTH_PASSWORD = os.environ.get("SIMPLEBOT_DASHBOARD_PASSWORD", "")


def _is_private_host(host: str) -> bool:
    """True si l'adresse de bind reste sur boucle locale / réseau privé."""
    if host in ("127.0.0.1", "::1", "localhost"):
        return True
    return host.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18.",
                            "172.19.", "172.2", "172.30.", "172.31."))


def _lan_ip() -> str:
    """Meilleure IP LAN pour l'affichage (aucun paquet réellement émis)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ── Lecture des fichiers d'état ──────────────────────────────────────────────

def _read_json(path) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _read_history(path, limit: int = 200) -> List[Dict[str, Any]]:
    """Dernières lignes du JSONL d'optimisation (une par run)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []
    out = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _iso_to_epoch(s: str) -> float:
    if not s:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


# ── Courbes d'equity multi-période (source canonique HL `portfolio`) ─────────
# L'historique local (live_state.json) est élagué à 24 h glissantes par le
# kill-switch : pour 7d/30d/all on lit l'endpoint PUBLIC portfolio de HL
# (adresse seule, lecture seule, pas de clé) qui fournit day/week/month/allTime
# — et qui est insensible aux résidus perp fantômes. Cache 90 s, fail-soft
# (on sert la dernière version connue si le réseau tousse).

_PF_CACHE: Dict[str, Any] = {"ts": 0.0, "curves": None}
_PERIOD_MAP = [("day", "24h"), ("week", "7d"), ("month", "30d"), ("allTime", "all")]


def _fetch_equity_curves() -> Optional[Dict[str, list]]:
    addr = os.environ.get(config.ENV_ACCOUNT_ADDRESS, "").strip()
    if not addr:
        return None
    body = json.dumps({"type": "portfolio", "user": addr}).encode()
    req = urllib.request.Request(
        "https://api.hyperliquid.xyz/info", data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        pf = json.load(r)
    per = dict(pf)   # [[period, {...}], ...] -> {period: {...}}
    curves: Dict[str, list] = {}
    for hl_key, label in _PERIOD_MAP:
        avh = (per.get(hl_key) or {}).get("accountValueHistory") or []
        pts = [[t / 1000.0, float(v)] for t, v in avh]
        # allTime démarre à 0 avant le funding du compte : on coupe les zéros de tête
        while pts and pts[0][1] <= 0:
            pts.pop(0)
        curves[label] = pts
    return curves


def _equity_curves(now: float) -> Optional[Dict[str, list]]:
    if _PF_CACHE["curves"] is not None and now - _PF_CACHE["ts"] < 90:
        return _PF_CACHE["curves"]
    try:
        curves = _fetch_equity_curves()
        if curves:
            _PF_CACHE["ts"] = now
            _PF_CACHE["curves"] = curves
    except Exception:
        pass   # on garde (et sert) la dernière version connue
    return _PF_CACHE["curves"]


def _momentum_block(now: float) -> Dict[str, Any]:
    """Résumé de l'état du momentum 4h paper (absent si jamais lancé)."""
    st = _read_json(getattr(config, "MOMENTUM_STATE_FILE", None))
    if not st:
        return {}
    trades = st.get("trades", []) or []
    wins = len([t for t in trades if t.get("pnl_pct", 0) > 0])
    eq = st.get("equity")
    start = None
    hist = st.get("equity_history", []) or []
    if hist:
        start = hist[0][1]
    return {
        "equity": eq,
        "equity_start": start or config.MOMENTUM_PAPER_EQUITY,
        "pnl_usd": (eq - (start or config.MOMENTUM_PAPER_EQUITY)) if eq is not None else None,
        "n_trades": len(trades),
        "winrate": (wins / len(trades)) if trades else None,
        "open": len(st.get("positions", {}) or {}),
        "positions": st.get("positions", {}) or {},
        "recent_trades": list(reversed(trades[-10:])),
        "last_update": hist[-1][0] if hist else None,
        "age_sec": int(now - hist[-1][0]) if hist else None,
        "params": {
            "roc_bars": config.MOMENTUM_ROC_BARS,
            "thr": config.MOMENTUM_THR,
            "time_exit_bars": config.MOMENTUM_TIME_EXIT_BARS,
            "sl_atr": config.MOMENTUM_SL_ATR,
        },
    }


def build_state() -> Dict[str, Any]:
    best = _read_json(config.BEST_PARAMS_FILE)
    live = _read_json(config.LIVE_STATE_FILE)
    history = _read_history(config.OPTIMIZER_HISTORY_FILE)

    now = time.time()

    # ── Symboles : résultats d'optimisation ──────────────────────────────────
    symbols_raw = best.get("symbols", {}) or {}
    symbols: List[Dict[str, Any]] = []
    active_count = 0
    for name, entry in symbols_raw.items():
        is_active = bool(entry.get("active"))
        active_count += int(is_active)
        row: Dict[str, Any] = {
            "symbol": name,
            "active": is_active,
            "reason": entry.get("reason"),
            "params": entry.get("params"),
            "train": entry.get("train"),
            "valid": entry.get("valid"),
        }
        symbols.append(row)
    # actifs d'abord, puis par PF de validation décroissant
    symbols.sort(
        key=lambda r: (
            not r["active"],
            -((r.get("valid") or {}).get("profit_factor") or 0),
        )
    )

    # ── Equity / PnL ─────────────────────────────────────────────────────────
    # equity « fraîche » : historique local (points 5 min, valeurs clampées) ;
    # départ/pic : courbe canonique allTime (l'historique local est élagué à 24 h,
    # son 1er point n'est PAS le départ du compte).
    eq_hist = live.get("equity_history", []) or []
    curves = _equity_curves(now) or {}
    all_curve = curves.get("all") or []
    equity = eq_hist[-1][1] if eq_hist else (all_curve[-1][1] if all_curve else None)
    equity_start = all_curve[0][1] if all_curve else (eq_hist[0][1] if eq_hist else None)
    peak_candidates = [v for _, v in all_curve] + [v for _, v in eq_hist]
    peak = max(peak_candidates, default=None)
    pnl_abs = (equity - equity_start) if (equity is not None and equity_start is not None) else None
    pnl_pct = (pnl_abs / equity_start) if (pnl_abs is not None and equity_start) else None
    dd_pct = ((equity - peak) / peak) if (equity is not None and peak) else None
    last_eq_ts = eq_hist[-1][0] if eq_hist else (all_curve[-1][0] if all_curve else None)

    # ── Positions papier (dry-run) ───────────────────────────────────────────
    paper = live.get("paper", {}) or {}
    paper_positions = paper.get("positions", {}) or {}
    paper_trades = paper.get("trades", []) or []
    p_total = sum(t.get("pnl_pct", 0) for t in paper_trades)
    p_wins = len([t for t in paper_trades if t.get("pnl_pct", 0) > 0])
    paper_stats = {
        "n_trades": len(paper_trades),
        "total_pnl_pct": p_total,
        "winrate": (p_wins / len(paper_trades)) if paper_trades else None,
        "open": len(paper_positions),
    }
    recent_trades = list(reversed(paper_trades[-25:]))

    # ── Kill-switch ──────────────────────────────────────────────────────────
    paused_until = float(live.get("paused_until", 0) or 0)
    paused = now < paused_until

    # ── Historique d'optimisation (évolution du nombre d'actifs) ─────────────
    opt_runs = []
    for run in history:
        syms = run.get("symbols", {}) or {}
        opt_runs.append({
            "ts": _iso_to_epoch(run.get("updated_at", "")),
            "updated_at": run.get("updated_at"),
            "n_symbols": len(syms),
            "n_active": len([1 for s in syms.values() if s.get("active")]),
        })

    return {
        "now": int(now),
        "mode": "DRY-RUN" if config.DRY_RUN else "LIVE",
        "interval": best.get("interval") or config.INTERVAL,
        "backtest_days": best.get("backtest_days") or config.BACKTEST_DAYS,
        "updated_at": best.get("updated_at"),
        "updated_age_sec": int(now - _iso_to_epoch(best.get("updated_at", ""))) if best.get("updated_at") else None,
        "config": {
            "leverage": config.LEVERAGE,
            "margin_pct": config.MARGIN_PCT,
            "max_open": config.MAX_OPEN_POSITIONS,
            "kill_loss_pct": config.KILL_LOSS_PCT,
            "universe_size": len(symbols),
        },
        "equity": equity,
        "equity_start": equity_start,
        "peak": peak,
        "pnl_abs": pnl_abs,
        "pnl_pct": pnl_pct,
        "dd_pct": dd_pct,
        "last_eq_ts": last_eq_ts,
        "equity_age_sec": int(now - last_eq_ts) if last_eq_ts else None,
        "equity_history": eq_hist,
        "equity_curves": curves,
        "active_count": active_count,
        "symbols": symbols,
        "paper_stats": paper_stats,
        "paper_positions": paper_positions,
        "recent_trades": recent_trades,
        "paused": paused,
        "paused_until": paused_until,
        "paused_remaining_min": int((paused_until - now) / 60) if paused else 0,
        "opt_runs": opt_runs,
        "momentum": _momentum_block(now),
    }


# ── HTTP ─────────────────────────────────────────────────────────────────────

INDEX_HTML = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>SimpleBot — Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { --bg:#0f1419; --panel:#1a1f29; --fg:#d6dde6; --mut:#7a8595; --grn:#3fb950; --red:#f85149; --acc:#58a6ff; --bord:#2a313c; --orange:#ff7f0e; --gold:#f5a623; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:13px/1.4 -apple-system,Segoe UI,Roboto,sans-serif; }
  header { padding:10px 18px; background:var(--panel); border-bottom:1px solid var(--bord); display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; }
  header h1 { margin:0; font-size:15px; font-weight:600; }
  header .meta { font-size:12px; color:var(--mut); }
  .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap:12px; padding:12px; }
  .card { background:var(--panel); border:1px solid var(--bord); border-radius:6px; padding:12px; }
  .card.wide { grid-column:1/-1; }
  .card h2 { margin:0 0 10px 0; font-size:12px; color:var(--mut); font-weight:600; text-transform:uppercase; letter-spacing:0.5px; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th, td { padding:4px 6px; text-align:right; border-bottom:1px solid var(--bord); white-space:nowrap; }
  th:first-child, td:first-child { text-align:left; }
  th { color:var(--mut); font-weight:500; }
  .grn { color:var(--grn); }
  .red { color:var(--red); }
  .acc { color:var(--acc); }
  .mut { color:var(--mut); }
  .big { font-size:26px; font-weight:600; }
  .row { display:flex; gap:14px; margin-bottom:8px; flex-wrap:wrap; }
  .row > div { flex:1; min-width:90px; }
  .lbl { font-size:11px; color:var(--mut); text-transform:uppercase; letter-spacing:.3px; }
  .badge { display:inline-block; padding:2px 8px; border-radius:3px; background:var(--bord); font-size:10px; text-transform:uppercase; font-weight:600; letter-spacing:.5px; }
  .badge.live { background:#3a1c1c; color:var(--red); }
  .badge.dry  { background:#3a2b1c; color:var(--gold); }
  .badge.on   { background:#12331d; color:var(--grn); }
  .badge.off  { background:#2a313c; color:var(--mut); }
  .badge.warn { background:#3a1c1c; color:var(--red); }
  .mono { font-family:Menlo,Consolas,monospace; }
  .ranges { display:inline-flex; gap:4px; margin-left:12px; vertical-align:middle; }
  .rbtn { background:var(--bord); border:none; color:var(--mut); padding:2px 9px; border-radius:3px; cursor:pointer; font-size:10px; font-weight:600; letter-spacing:.3px; }
  .rbtn:hover { color:var(--fg); }
  .rbtn.on { background:#12331d; color:var(--grn); }
  svg { display:block; width:100%; height:120px; }
  .scroll { max-height:340px; overflow:auto; }
  .kpi { text-align:center; }
  .kpi .big { font-size:22px; }
</style>
</head>
<body>
<header>
  <h1>SimpleBot — Dashboard <span class="badge" id="mode">…</span></h1>
  <div class="meta">Refresh <span id="lastrf">…</span> · <span id="freshness"></span></div>
</header>

<div class="grid">

  <div class="card wide">
    <h2>Equity — wallet dédié
      <span class="ranges" id="eqranges">
        <button class="rbtn" data-r="24h">24h</button>
        <button class="rbtn" data-r="7d">7d</button>
        <button class="rbtn" data-r="30d">30d</button>
        <button class="rbtn" data-r="all">all</button>
      </span>
    </h2>
    <div class="row">
      <div><div class="lbl">Equity</div><div class="big" id="equity">…</div></div>
      <div><div class="lbl">PnL depuis départ</div><div class="big" id="pnl">…</div></div>
      <div class="kpi"><div class="lbl">Pic</div><div class="big" id="peak">…</div></div>
      <div class="kpi"><div class="lbl">Drawdown vs pic</div><div class="big" id="dd">…</div></div>
      <div class="kpi"><div class="lbl">Kill-switch</div><div class="big"><span class="badge" id="kill">…</span></div></div>
    </div>
    <svg id="equityChart" viewBox="0 0 1000 120" preserveAspectRatio="none"></svg>
    <div class="mut" id="eqmeta" style="font-size:11px;margin-top:4px"></div>
  </div>

  <div class="card">
    <h2>État d'optimisation</h2>
    <div class="row">
      <div class="kpi"><div class="lbl">Symboles actifs</div><div class="big grn" id="nactive">…</div></div>
      <div class="kpi"><div class="lbl">Univers</div><div class="big" id="nuniv">…</div></div>
      <div class="kpi"><div class="lbl">Positions max</div><div class="big" id="maxopen">…</div></div>
    </div>
    <div class="row">
      <div><div class="lbl">Intervalle</div><div class="mono" id="interval">…</div></div>
      <div><div class="lbl">Backtest</div><div class="mono" id="btdays">…</div></div>
      <div><div class="lbl">Levier</div><div class="mono" id="lev">…</div></div>
    </div>
    <div class="mut" id="optmeta" style="font-size:11px;margin-top:4px"></div>
  </div>

  <div class="card" id="papercard">
    <h2>Positions papier (dry-run)</h2>
    <div class="row">
      <div class="kpi"><div class="lbl">Trades</div><div class="big" id="pn">…</div></div>
      <div class="kpi"><div class="lbl">PnL cumulé</div><div class="big" id="ppnl">…</div></div>
      <div class="kpi"><div class="lbl">Winrate</div><div class="big" id="pwr">…</div></div>
      <div class="kpi"><div class="lbl">Ouvertes</div><div class="big" id="popen">…</div></div>
    </div>
    <table id="postbl"><thead><tr><th>Symbole</th><th>Sens</th><th>Entrée</th><th>TP</th><th>SL</th></tr></thead><tbody></tbody></table>
  </div>

  <div class="card wide">
    <h2>Symboles — résultats d'optimisation walk-forward</h2>
    <div class="scroll">
    <table id="symtbl">
      <thead><tr>
        <th>Symbole</th><th>État</th><th>EMA</th><th>TP/SL ATR</th>
        <th>PF train</th><th>PF valid</th><th>PnL valid</th><th>WR valid</th><th>#trades</th><th>Motif</th>
      </tr></thead>
      <tbody></tbody>
    </table>
    </div>
  </div>

  <div class="card wide" id="momcard" style="display:none">
    <h2>Momentum 4h — paper (params figés, aucun ordre réel)</h2>
    <div class="row">
      <div class="kpi"><div class="lbl">Equity paper</div><div class="big" id="m_eq">…</div></div>
      <div class="kpi"><div class="lbl">PnL</div><div class="big" id="m_pnl">…</div></div>
      <div class="kpi"><div class="lbl">Trades</div><div class="big" id="m_n">…</div></div>
      <div class="kpi"><div class="lbl">Winrate</div><div class="big" id="m_wr">…</div></div>
      <div class="kpi"><div class="lbl">Ouvertes</div><div class="big" id="m_open">…</div></div>
    </div>
    <div class="mut" id="m_meta" style="font-size:11px;margin-bottom:6px"></div>
    <table id="m_postbl"><thead><tr><th>Symbole</th><th>Sens</th><th>Entrée</th><th>SL</th><th>Funding acc.</th><th>Depuis</th></tr></thead><tbody></tbody></table>
    <table id="m_trtbl" style="margin-top:8px"><thead><tr><th>Symbole</th><th>Sens</th><th>Entrée</th><th>Sortie</th><th>PnL</th><th>Funding</th><th>Motif</th></tr></thead><tbody></tbody></table>
  </div>

  <div class="card wide" id="tradescard">
    <h2>Derniers trades papier</h2>
    <div class="scroll">
    <table id="trtbl">
      <thead><tr><th>Symbole</th><th>Sens</th><th>Entrée</th><th>Sortie</th><th>PnL</th><th>Motif</th><th>Clôture</th></tr></thead>
      <tbody></tbody>
    </table>
    </div>
  </div>

</div>

<script>
const fmt  = (n,d=2) => (n==null||isNaN(n)) ? '–' : Number(n).toFixed(d);
const usd  = (n,d=2) => (n==null||isNaN(n)) ? '–' : '$'+Number(n).toFixed(d);
const pct  = (n,d=2) => (n==null||isNaN(n)) ? '–' : (n*100>=0?'+':'')+(n*100).toFixed(d)+'%';
const cls  = (n) => (n==null||isNaN(n)) ? 'mut' : (n>=0?'grn':'red');
const ago  = (s) => s==null ? '–' : (s<90? s+'s' : s<3600? Math.round(s/60)+'min' : Math.round(s/3600)+'h')+' ago';
const dt   = (ts) => ts ? new Date(ts*1000).toLocaleString() : '–';

function drawEquity(hist) {
  const svg = document.getElementById('equityChart');
  if (!hist || hist.length < 2) { svg.innerHTML = '<text x="10" y="60" fill="#7a8595" font-size="13">pas encore de courbe d\'equity</text>'; return; }
  const W=1000, H=120, pad=6;
  const ys = hist.map(p=>p[1]);
  let lo=Math.min(...ys), hi=Math.max(...ys);
  if (hi===lo){ hi+=1; lo-=1; }
  const n=hist.length;
  const X = i => pad + i*(W-2*pad)/(n-1);
  const Y = v => H-pad - (v-lo)*(H-2*pad)/(hi-lo);
  let d='';
  hist.forEach((p,i)=>{ d += (i? 'L':'M') + X(i).toFixed(1)+' '+Y(p[1]).toFixed(1)+' '; });
  const start=ys[0], end=ys[ys.length-1];
  const color = end>=start ? '#3fb950' : '#f85149';
  const area = d + `L ${X(n-1).toFixed(1)} ${H-pad} L ${X(0).toFixed(1)} ${H-pad} Z`;
  // ligne de départ (référence)
  const yStart = Y(start).toFixed(1);
  svg.innerHTML =
    `<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
       <stop offset="0" stop-color="${color}" stop-opacity="0.25"/>
       <stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>`+
    `<line x1="0" y1="${yStart}" x2="${W}" y2="${yStart}" stroke="#2a313c" stroke-dasharray="4 4"/>`+
    `<path d="${area}" fill="url(#g)"/>`+
    `<path d="${d}" fill="none" stroke="${color}" stroke-width="1.6"/>`;
}

// ── Sélecteur de période de la courbe d'equity ───────────────────────────────
// 24h = historique local dense (points 5 min, clampé anti-fantôme) ;
// 7d/30d/all = courbes canoniques HL (endpoint portfolio).
let eqRange = localStorage.getItem('eqRange') || '24h';
let lastS = null;

function curveFor(s) {
  if (eqRange === '24h') {
    const local = s.equity_history || [];
    if (local.length >= 2) return local;
    return (s.equity_curves||{})['24h'] || local;   // repli si local vide
  }
  const c = ((s.equity_curves||{})[eqRange] || []).slice();
  // point « maintenant » : l'historique HL retarde de ~20 min, on colle la
  // dernière valeur locale fraîche au bout de la courbe
  if (s.last_eq_ts && s.equity != null && (!c.length || s.last_eq_ts > c[c.length-1][0]))
    c.push([s.last_eq_ts, s.equity]);
  return c;
}

function renderEquityCurve() {
  if (!lastS) return;
  const c = curveFor(lastS);
  drawEquity(c);
  const src = eqRange === '24h' ? 'local 5min' : 'HL canonique';
  document.getElementById('eqmeta').textContent =
    eqRange+' ('+src+') · départ compte '+usd(lastS.equity_start)+' · '+c.length+' points · dernier '+dt(lastS.last_eq_ts);
  document.querySelectorAll('#eqranges .rbtn').forEach(b =>
    b.classList.toggle('on', b.dataset.r === eqRange));
}

document.getElementById('eqranges').addEventListener('click', (e) => {
  const r = e.target && e.target.dataset && e.target.dataset.r;
  if (!r) return;
  eqRange = r;
  localStorage.setItem('eqRange', r);
  renderEquityCurve();
});

async function refresh() {
  let s;
  try {
    const r = await fetch('/api/state', {cache:'no-store'});
    s = await r.json();
  } catch(e) {
    document.getElementById('lastrf').textContent = 'erreur: '+e.message;
    return;
  }
  lastS = s;
  document.getElementById('lastrf').textContent = new Date().toLocaleTimeString();

  // Mode + fraîcheur
  const modeEl = document.getElementById('mode');
  modeEl.textContent = s.mode;
  modeEl.className = 'badge ' + (s.mode==='LIVE' ? 'live':'dry');
  const eqAge = s.equity_age_sec;
  const stale = eqAge!=null && eqAge>600;
  document.getElementById('freshness').innerHTML =
    'equity <span class="'+(stale?'red':'mut')+'">'+ago(eqAge)+'</span>';

  // Equity
  document.getElementById('equity').textContent = usd(s.equity);
  const pnlEl = document.getElementById('pnl');
  pnlEl.textContent = (s.pnl_abs==null?'–':usd(s.pnl_abs)) + ' · ' + pct(s.pnl_pct);
  pnlEl.className = 'big ' + cls(s.pnl_abs);
  document.getElementById('peak').textContent = usd(s.peak);
  const ddEl = document.getElementById('dd');
  ddEl.textContent = pct(s.dd_pct);
  ddEl.className = 'big ' + cls(s.dd_pct);
  const killEl = document.getElementById('kill');
  if (s.paused) { killEl.textContent = 'PAUSE '+s.paused_remaining_min+'min'; killEl.className='badge warn'; }
  else { killEl.textContent = 'armé'; killEl.className='badge on'; }
  renderEquityCurve();

  // Optimisation
  document.getElementById('nactive').textContent = s.active_count;
  document.getElementById('nuniv').textContent = s.symbols.length;
  document.getElementById('maxopen').textContent = s.config.max_open;
  document.getElementById('interval').textContent = s.interval;
  document.getElementById('btdays').textContent = s.backtest_days + 'j';
  document.getElementById('lev').textContent = s.config.leverage + 'x';
  const optWhen = s.updated_at ? new Date(s.updated_at).toLocaleString() : '–';
  const optStale = s.updated_age_sec!=null && s.updated_age_sec>25200; // > 7h (optim toutes les 6h)
  document.getElementById('optmeta').innerHTML =
    'dernière optim '+optWhen+' <span class="'+(optStale?'red':'mut')+'">('+ago(s.updated_age_sec)+')</span>';

  // Paper
  const ps = s.paper_stats || {};
  const paperActive = ps.n_trades>0 || ps.open>0;
  document.getElementById('papercard').style.display = paperActive ? '' : 'none';
  document.getElementById('tradescard').style.display = (s.recent_trades && s.recent_trades.length) ? '' : 'none';
  document.getElementById('pn').textContent = ps.n_trades ?? 0;
  const ppnlEl = document.getElementById('ppnl');
  ppnlEl.textContent = pct(ps.total_pnl_pct); ppnlEl.className = 'big '+cls(ps.total_pnl_pct);
  document.getElementById('pwr').textContent = ps.winrate==null?'–':(ps.winrate*100).toFixed(0)+'%';
  document.getElementById('popen').textContent = ps.open ?? 0;
  const tbP = document.querySelector('#postbl tbody'); tbP.innerHTML='';
  const pos = s.paper_positions || {};
  Object.entries(pos).forEach(([sym,p])=>{
    const tr=document.createElement('tr');
    const dcls = p.dir>0?'grn':'red', dtxt = p.dir>0?'LONG':'SHORT';
    tr.innerHTML = `<td>${sym}</td><td class="${dcls}">${dtxt}</td><td class="mono">${fmt(p.entry,5)}</td><td class="mono">${fmt(p.tp,5)}</td><td class="mono">${fmt(p.sl,5)}</td>`;
    tbP.appendChild(tr);
  });
  if (!Object.keys(pos).length) tbP.innerHTML='<tr><td colspan="5" class="mut" style="text-align:center">aucune position papier</td></tr>';

  // Momentum 4h paper
  const m = s.momentum || {};
  const mActive = m.equity != null;
  document.getElementById('momcard').style.display = mActive ? '' : 'none';
  if (mActive) {
    document.getElementById('m_eq').textContent = usd(m.equity);
    const mp = document.getElementById('m_pnl');
    mp.textContent = usd(m.pnl_usd); mp.className = 'big '+cls(m.pnl_usd);
    document.getElementById('m_n').textContent = m.n_trades ?? 0;
    document.getElementById('m_wr').textContent = m.winrate==null?'–':(m.winrate*100).toFixed(0)+'%';
    document.getElementById('m_open').textContent = m.open ?? 0;
    const pr = m.params||{};
    document.getElementById('m_meta').textContent =
      `ROC ${pr.roc_bars}×4h ±${(pr.thr*100).toFixed(0)}% · time-exit ${pr.time_exit_bars} bougies (12j) · SL ${pr.sl_atr}×ATR · pas de TP · maj ${ago(m.age_sec)}`;
    const tbM = document.querySelector('#m_postbl tbody'); tbM.innerHTML='';
    Object.entries(m.positions||{}).forEach(([sym,p])=>{
      const tr=document.createElement('tr');
      const dcls=p.dir>0?'grn':'red', dtxt=p.dir>0?'LONG':'SHORT';
      const since=p.entry_ts?Math.round((Date.now()-p.entry_ts)/3600000)+'h':'–';
      tr.innerHTML=`<td>${sym}</td><td class="${dcls}">${dtxt}</td><td class="mono">${fmt(p.entry,5)}</td><td class="mono">${fmt(p.sl,5)}</td><td class="mono ${cls(p.funding_pct)}">${pct(p.funding_pct,3)}</td><td class="mono mut">${since}</td>`;
      tbM.appendChild(tr);
    });
    if (!Object.keys(m.positions||{}).length) tbM.innerHTML='<tr><td colspan="6" class="mut" style="text-align:center">aucune position — les croisements ROC±2% sont rares, patience</td></tr>';
    const tbMt = document.querySelector('#m_trtbl tbody'); tbMt.innerHTML='';
    (m.recent_trades||[]).forEach(t=>{
      const tr=document.createElement('tr');
      const dcls=t.dir>0?'grn':'red', dtxt=t.dir>0?'LONG':'SHORT';
      tr.innerHTML=`<td>${t.symbol}</td><td class="${dcls}">${dtxt}</td><td class="mono">${fmt(t.entry,5)}</td><td class="mono">${fmt(t.exit,5)}</td><td class="mono ${cls(t.pnl_pct)}">${pct(t.pnl_pct)}</td><td class="mono ${cls(t.funding_pct)}">${pct(t.funding_pct,3)}</td><td><span class="badge off">${t.reason||''}</span></td>`;
      tbMt.appendChild(tr);
    });
  }

  // Symboles
  const tbS = document.querySelector('#symtbl tbody'); tbS.innerHTML='';
  s.symbols.forEach(r=>{
    const tr=document.createElement('tr');
    const st = r.active ? '<span class="badge on">actif</span>' : '<span class="badge off">inactif</span>';
    const pr = r.params||{}, tv=r.train||{}, vv=r.valid||{};
    const ema = pr.ema_fast!=null ? `${pr.ema_fast}/${pr.ema_slow}` : '–';
    const tpsl = pr.tp_atr!=null ? `${pr.tp_atr}/${pr.sl_atr}` : '–';
    const pfv = vv.profit_factor, wr = vv.winrate;
    tr.innerHTML =
      `<td><b>${r.symbol}</b></td><td>${st}</td>`+
      `<td class="mono">${ema}</td><td class="mono">${tpsl}</td>`+
      `<td class="mono">${tv.profit_factor!=null?fmt(tv.profit_factor,2):'–'}</td>`+
      `<td class="mono ${pfv!=null?(pfv>=1.2?'grn':'red'):''}">${pfv!=null?fmt(pfv,2):'–'}</td>`+
      `<td class="mono ${cls(vv.total_pnl_pct)}">${vv.total_pnl_pct!=null?pct(vv.total_pnl_pct):'–'}</td>`+
      `<td class="mono">${wr!=null?(wr*100).toFixed(0)+'%':'–'}</td>`+
      `<td class="mono">${vv.n_trades!=null?vv.n_trades:'–'}</td>`+
      `<td class="mut" style="text-align:left;white-space:normal">${r.reason||''}</td>`;
    tbS.appendChild(tr);
  });
  if (!s.symbols.length) tbS.innerHTML='<tr><td colspan="10" class="mut" style="text-align:center">pas encore d\'optimisation</td></tr>';

  // Trades
  const tbT = document.querySelector('#trtbl tbody'); tbT.innerHTML='';
  (s.recent_trades||[]).forEach(t=>{
    const tr=document.createElement('tr');
    const dcls=t.dir>0?'grn':'red', dtxt=t.dir>0?'LONG':'SHORT';
    tr.innerHTML =
      `<td>${t.symbol}</td><td class="${dcls}">${dtxt}</td>`+
      `<td class="mono">${fmt(t.entry,5)}</td><td class="mono">${fmt(t.exit,5)}</td>`+
      `<td class="mono ${cls(t.pnl_pct)}">${pct(t.pnl_pct)}</td>`+
      `<td><span class="badge off">${t.reason||''}</span></td>`+
      `<td class="mono mut">${t.exit_ts?dt(t.exit_ts/1000):'–'}</td>`;
    tbT.appendChild(tr);
  });
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence stdout
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        """Basic Auth si un mot de passe est configuré ; libre sinon."""
        if not AUTH_PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            user, _, pwd = b64decode(header[6:]).decode("utf-8").partition(":")
        except Exception:
            return False
        # comparaison à temps constant sur les deux champs
        return (hmac.compare_digest(user, AUTH_USER)
                and hmac.compare_digest(pwd, AUTH_PASSWORD))

    def _deny(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="SimpleBot Dashboard"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        if not self._authorized():
            self._deny()
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            try:
                payload = json.dumps(build_state()).encode("utf-8")
                self._send(200, payload, "application/json")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")


def main() -> int:
    host = HOST
    # Garde-fou : sans mot de passe, on interdit un bind au-delà du réseau privé.
    # (0.0.0.0 est toléré : il n'écoute que sur les interfaces locales de la
    #  machine ; le vrai risque — l'exposition Internet — passe par un port
    #  forwardé/tunnel, à protéger par SIMPLEBOT_DASHBOARD_PASSWORD.)
    if not AUTH_PASSWORD and not (host == "0.0.0.0" or _is_private_host(host)):
        print(f"⚠️  Bind {host} refusé sans SIMPLEBOT_DASHBOARD_PASSWORD "
              f"— repli sur 127.0.0.1 (accès local uniquement).")
        host = "127.0.0.1"

    server = ThreadingHTTPServer((host, PORT), Handler)

    auth = "🔒 Basic Auth (user=%s)" % AUTH_USER if AUTH_PASSWORD else "🔓 sans auth"
    print(f"Dashboard SimpleBot — {auth} — Ctrl+C pour arrêter")
    print(f"  local   : http://localhost:{PORT}/")
    if host in ("0.0.0.0", "::"):
        print(f"  LAN     : http://{_lan_ip()}:{PORT}/")
    if not AUTH_PASSWORD:
        print("  ⚠️  Hors LAN : définir SIMPLEBOT_DASHBOARD_PASSWORD et passer par "
              "un tunnel (SSH / Tailscale / cloudflared).")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

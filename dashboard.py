#!/usr/bin/env python3
"""
Dashboard live SalleDesMarches V6.

Usage : .venv/bin/python dashboard.py  (puis http://localhost:8081)

Agrège en lecture seule :
  - scalp_memory.json  → trades, WR, PnL cumulé
  - memory/shared_memory.json → régime, watchlist
  - logs/sdm.log (tail) → derniers SKIP + Stats cycle
  - API HL → positions ouvertes, account value

Aucune écriture, aucune dépendance sur le bot tournant.
"""
from __future__ import annotations
import json
import os
import re
import time
from collections import Counter, deque
from pathlib import Path
from typing import Any, Dict, List

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

REPO = Path(__file__).parent
SCALP_MEM = REPO / "memory" / "scalp_memory.json"
SHARED_MEM = REPO / "memory" / "shared_memory.json"
MR_STATE = REPO / "memory" / "mr_state.json"
LOG_FILE = REPO / "logs" / "sdm.log"

# Charger .env pour HL_ACCOUNT_ADDRESS
for line in (REPO / ".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

HL_API = "https://api.hyperliquid.xyz/info"
HL_ADDR = os.environ.get("HL_ACCOUNT_ADDRESS", "")

# Cache user_fills 60s : pas la peine de re-tirer 2000 fills à chaque refresh (5s)
_FILLS_CACHE: Dict[str, Any] = {"ts": 0.0, "fills": []}
_FILLS_TTL_SEC = 60.0

app = FastAPI(title="SalleDesMarches Dashboard")


def _read_scalp() -> Dict:
    try:
        return json.loads(SCALP_MEM.read_text())
    except Exception:
        return {"trades": []}


def _read_shared() -> Dict:
    try:
        return json.loads(SHARED_MEM.read_text())
    except Exception:
        return {}


def _hl_state() -> Dict:
    out = {
        "positions": [],
        "open_orders": [],
        "spot_usdc": 0.0,
        "upnl_total": 0.0,
        "margin_used": 0.0,
        "account_value": 0.0,   # spot + uPnL (unified account, perp accountValue déjà inclus dans spot)
        "ok": False,
    }
    if not HL_ADDR:
        return out
    try:
        # 1) Compte perp (positions + uPnL)
        r = requests.post(
            HL_API, json={"type": "clearinghouseState", "user": HL_ADDR}, timeout=4
        ).json()
        ms = r.get("marginSummary", {})
        out["margin_used"] = float(ms.get("totalMarginUsed", 0) or 0)
        total_upnl = 0.0
        for p in r.get("assetPositions", []):
            pp = p.get("position", {})
            szi = float(pp.get("szi", 0) or 0)
            if szi == 0:
                continue
            entry = float(pp.get("entryPx", 0) or 0)
            upnl = float(pp.get("unrealizedPnl", 0) or 0)
            total_upnl += upnl
            lev = pp.get("leverage", {}) or {}
            out["positions"].append({
                "coin": pp.get("coin"),
                "side": "long" if szi > 0 else "short",
                "qty": abs(szi),
                "entry": entry,
                "upnl": upnl,
                "roe": float(pp.get("returnOnEquity", 0) or 0) * 100,
                "leverage": lev.get("value", 0),
            })
        out["upnl_total"] = total_upnl

        # 2) Compte spot. Sur HL unified, spot_usdc.total EST l'equity totale du
        # compte : il intègre déjà le PnL réalisé ET l'uPnL en temps réel via le
        # mécanisme de margin partagée. Vérif numérique constante :
        #   spot_total ≈ spot_free + perp_accountValue
        #              = (spot_total − spot_hold) + (spot_hold + uPnL)
        # Donc ajouter total_upnl serait un double-comptage (fix 25/05).
        rs = requests.post(
            HL_API, json={"type": "spotClearinghouseState", "user": HL_ADDR}, timeout=4
        ).json()
        for b in rs.get("balances", []) or []:
            if str(b.get("coin", "")).upper() == "USDC":
                out["spot_usdc"] = float(b.get("total", 0) or 0)

        out["account_value"] = out["spot_usdc"]

        # 3) Open orders avec lookup registre pour la source
        try:
            from memory.order_registry import get_order_registry
            reg = get_order_registry()
        except Exception:
            reg = None
        try:
            ro = requests.post(
                HL_API, json={"type": "frontendOpenOrders", "user": HL_ADDR}, timeout=4
            ).json()
            for o in (ro or []):
                try:
                    oid = int(o.get("oid"))
                except Exception:
                    continue
                rec = reg.lookup(oid) if reg else None
                # HL renvoie limitPx ET triggerPx comme strings ("0.0" pour les
                # champs non utilisés). Le `or` court-circuite sur la string
                # non-vide "0.0" → toujours 0 si on lit triggerPx en premier.
                # Fix : sélectionner le bon champ via isTrigger.
                is_trigger = bool(o.get("isTrigger", False))
                try:
                    if is_trigger:
                        price = float(o.get("triggerPx") or 0)
                    else:
                        price = float(o.get("limitPx") or 0)
                except (ValueError, TypeError):
                    price = 0.0
                out["open_orders"].append({
                    "oid": oid,
                    "coin": str(o.get("coin", "")),
                    "side": "buy" if str(o.get("side", "")).upper() in ("B", "BUY") else "sell",
                    "price": price,
                    "sz": float(o.get("sz") or 0),
                    "trigger": is_trigger,
                    "tpsl": str(o.get("tpsl") or ""),
                    "reduce_only": bool(o.get("reduceOnly", False)),
                    "source": rec.source if rec else "unknown",
                    "intent": rec.intent if rec else "?",
                })
        except Exception:
            pass

        out["ok"] = True
    except Exception as e:
        out["error"] = str(e)[:120]
    return out


def _wr(trades: List[Dict], since: float) -> Dict:
    sub = [t for t in trades if (t.get("exit_ts") or 0) >= since and t.get("pnl_pct") is not None]
    if not sub:
        return {"n": 0, "wr": 0.0, "pnl_pct_sum": 0.0, "pnl_usdt_sum": 0.0}
    wins = sum(1 for t in sub if t["pnl_pct"] > 0)
    return {
        "n": len(sub),
        "wr": wins / len(sub) * 100,
        "pnl_pct_sum": sum(t["pnl_pct"] for t in sub) * 100,
        "pnl_usdt_sum": sum(float(t.get("pnl_usdt") or 0) for t in sub),
    }


SKIP_RE = re.compile(r"SKIP (\S+) — (.+?)(?:\s+\(|$)")
STATS_RE = re.compile(r"Stats cycle: analyzed=(\d+) skipped=(\d+) entered=(\d+) flipped=(\d+) open=(\d+) trail_guards=(\d+)")


def _tail_logs(max_lines: int = 4000) -> Dict:
    out = {"skips": [], "skip_counts": {}, "last_stats": None, "last_regime_line": None}
    if not LOG_FILE.exists():
        return out
    try:
        # Tail efficace : seek depuis la fin
        size = LOG_FILE.stat().st_size
        with LOG_FILE.open("rb") as f:
            f.seek(max(0, size - 600_000))   # ~600 KB
            data = f.read().decode("utf-8", errors="ignore")
        lines = data.splitlines()[-max_lines:]
        recent_skips = deque(maxlen=30)
        for ln in lines:
            m = SKIP_RE.search(ln)
            if m:
                sym, reason = m.group(1), m.group(2).strip()
                # Normalise raison
                if "ATR trop élevé" in reason:
                    cat = "ATR"
                elif "RSI overbought" in reason:
                    cat = "RSI_long"
                elif "RSI oversold" in reason:
                    cat = "RSI_short"
                elif "strate gate" in reason:
                    cat = "strate_gate"
                elif "XGB gate" in reason:
                    cat = "XGB_gate"
                elif "scalp_filter" in reason or "scalp filter" in reason:
                    cat = "scalp_filter"
                else:
                    cat = "other"
                # extraire timestamp HH:MM:SS
                ts = ln[:8] if len(ln) >= 8 and ln[2] == ":" else ""
                recent_skips.append({"ts": ts, "symbol": sym, "reason": reason, "cat": cat})
            m2 = STATS_RE.search(ln)
            if m2:
                out["last_stats"] = {
                    "analyzed": int(m2.group(1)),
                    "skipped": int(m2.group(2)),
                    "entered": int(m2.group(3)),
                    "flipped": int(m2.group(4)),
                    "open": int(m2.group(5)),
                    "trail_guards": int(m2.group(6)),
                    "ts": ln[:8] if len(ln) >= 8 else "",
                }
        out["skips"] = list(recent_skips)
        out["skip_counts"] = dict(Counter(s["cat"] for s in recent_skips))
    except Exception as e:
        out["error"] = str(e)[:120]
    return out


def _pnl_curve(trades: List[Dict]) -> List[Dict]:
    closed = [t for t in trades if t.get("exit_ts") and t.get("pnl_usdt") is not None]
    closed.sort(key=lambda t: t["exit_ts"])
    cum = 0.0
    return [
        {"ts": int(t["exit_ts"]), "pnl_cum": (cum := cum + float(t["pnl_usdt"]))}
        for t in closed
    ]


def _hl_fills() -> List[Dict]:
    """Cache 60s des derniers fills HL (max 2000)."""
    now = time.time()
    if now - _FILLS_CACHE["ts"] < _FILLS_TTL_SEC and _FILLS_CACHE["fills"]:
        return _FILLS_CACHE["fills"]
    if not HL_ADDR:
        return []
    try:
        r = requests.post(
            HL_API, json={"type": "userFills", "user": HL_ADDR}, timeout=6
        ).json()
        if isinstance(r, list):
            _FILLS_CACHE["fills"] = r
            _FILLS_CACHE["ts"] = now
            return r
    except Exception:
        pass
    return _FILLS_CACHE.get("fills") or []


def _pnl_curve_hl(fills: List[Dict]) -> List[Dict]:
    """Courbe cumulative (closedPnl - fee) à partir des fills HL.

    Vérité absolue : prend en compte les fees, les trades grid, et le levier
    réel HL. Source corrige le biais de scalp_memory (qui surestimait +35
    USDC alors que le NET HL réel est ~-30 USDC sur la même période).
    """
    if not fills:
        return []
    by_time = sorted(fills, key=lambda f: int(f.get("time", 0)))
    pts: List[Dict] = []
    cum = 0.0
    for f in by_time:
        delta = float(f.get("closedPnl", 0) or 0) - float(f.get("fee", 0) or 0)
        if delta == 0.0 and not pts:
            continue  # skip open-only fills jusqu'au premier mouvement
        cum += delta
        pts.append({"ts": int(int(f.get("time", 0)) / 1000), "pnl_cum": round(cum, 4)})
    return pts


def _wr_hl(fills: List[Dict], since_ms: int) -> Dict:
    """Win Rate calculé sur les vrais fills HL (closedPnl != 0 = trade close)."""
    closes = [f for f in fills if float(f.get("closedPnl", 0) or 0) != 0 and int(f.get("time", 0)) >= since_ms]
    if not closes:
        return {"n": 0, "wr": 0.0, "pnl_usdt_sum": 0.0}
    nets = [float(f.get("closedPnl", 0) or 0) - float(f.get("fee", 0) or 0) for f in closes]
    wins = sum(1 for n in nets if n > 0)
    return {
        "n": len(nets),
        "wr": round(wins / len(nets) * 100, 1),
        "pnl_usdt_sum": round(sum(nets), 2),
    }


def _mr_state() -> Dict:
    """Lit l'état mean-reversion persisté par le bot. None-safe."""
    out = {"ts": 0, "symbols": [], "metrics": {}, "open_positions": {}, "age_sec": None}
    try:
        if not MR_STATE.exists():
            return out
        d = json.loads(MR_STATE.read_text())
        out.update(d)
        out["age_sec"] = max(0, int(time.time() - float(d.get("ts", 0) or 0)))
    except Exception:
        pass
    return out


def _grid_view(hl_state: Dict) -> List[Dict]:
    """Construit la vue grid par symbole depuis open_orders + sources registry.

    Pour chaque symbole grid actif, regroupe les niveaux pending (limit non-reduce)
    et TPs (limit reduce_only), trie par prix et marque les fills (présents dans
    le registry mais plus côté HL ne sont pas listés ; absents = fills passés).
    """
    by_sym: Dict[str, Dict] = {}
    for o in hl_state.get("open_orders", []) or []:
        if o.get("source") not in ("grid_pending", "grid_tp"):
            continue
        sym = o.get("coin")
        if sym not in by_sym:
            by_sym[sym] = {"symbol": sym, "levels": []}
        by_sym[sym]["levels"].append({
            "oid": o["oid"], "side": o["side"], "price": o["price"],
            "qty": o["sz"], "kind": "pending" if o["source"] == "grid_pending" else "tp",
        })
    # Tri par prix (ladder propre)
    for sym in by_sym:
        by_sym[sym]["levels"].sort(key=lambda x: -x["price"])
    return list(by_sym.values())


def _regime_status(shared: Dict) -> Dict:
    """Indique si le régime est en mode fallback (orchestrator sans features) ou live."""
    rg = shared.get("regime", {}) or {}
    # Le orchestrator log persistence=0.0 nsymbols=0 → fallback. On vérifie via shared_memory
    # advanced_features dont les champs requis (slope_alignment, etc.) sont None.
    af = shared.get("advanced_features", {}) or {}
    sample = next(iter(af.values()), {}) if af else {}
    has_signals = any(sample.get(k) for k in ("slope_alignment", "vol_state", "markov_state"))
    return {
        **rg,
        "source": "live" if has_signals else "fallback_default",
    }


@app.get("/api/state")
def api_state() -> JSONResponse:
    scalp = _read_scalp()
    trades = scalp.get("trades", [])
    shared = _read_shared()
    now = time.time()
    cut7 = now - 7 * 86400
    cut30 = now - 30 * 86400

    last = sorted(
        [t for t in trades if t.get("exit_ts")],
        key=lambda t: t.get("exit_ts", 0), reverse=True,
    )[:20]

    hl = _hl_state()
    fills = _hl_fills()
    now_ms = int(now * 1000)

    return JSONResponse({
        "now": int(now),
        "hl": hl,
        "regime": _regime_status(shared),
        "grid": _grid_view(hl),
        "mr": _mr_state(),
        # WR + PnL : source HL (closedPnl − fees), pas scalp_memory (biaisé)
        "wr_7d": _wr_hl(fills, now_ms - 7 * 86400_000),
        "wr_30d": _wr_hl(fills, now_ms - 30 * 86400_000),
        "wr_all": _wr_hl(fills, 0),
        "last_trades": [{
            "ts": int(t.get("exit_ts") or 0),
            "symbol": t.get("symbol"),
            "side": t.get("side"),
            "entry": t.get("entry_px"),
            "exit": t.get("exit_px"),
            "pnl_pct": float(t.get("pnl_pct") or 0) * 100,
            "pnl_usdt": float(t.get("pnl_usdt") or 0),
            "cause": t.get("cause"),
            "dur_min": int(float(t.get("duration_sec") or 0) / 60),
        } for t in last],
        "pnl_curve": _pnl_curve_hl(fills),
        **_tail_logs(),
    })


INDEX_HTML = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>SDM Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { --bg:#0f1419; --panel:#1a1f29; --fg:#d6dde6; --mut:#7a8595; --grn:#3fb950; --red:#f85149; --acc:#58a6ff; --bord:#2a313c; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:13px/1.4 -apple-system,Segoe UI,Roboto,sans-serif; }
  header { padding:10px 18px; background:var(--panel); border-bottom:1px solid var(--bord); display:flex; justify-content:space-between; align-items:center; }
  header h1 { margin:0; font-size:15px; font-weight:600; }
  header .meta { font-size:12px; color:var(--mut); }
  .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap:12px; padding:12px; }
  .card { background:var(--panel); border:1px solid var(--bord); border-radius:6px; padding:12px; }
  .card h2 { margin:0 0 8px 0; font-size:13px; color:var(--mut); font-weight:600; text-transform:uppercase; letter-spacing:0.5px; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th, td { padding:4px 6px; text-align:right; border-bottom:1px solid var(--bord); }
  th:first-child, td:first-child { text-align:left; }
  th { color:var(--mut); font-weight:500; }
  .grn { color:var(--grn); }
  .red { color:var(--red); }
  .acc { color:var(--acc); }
  .big { font-size:22px; font-weight:600; }
  .row { display:flex; gap:12px; margin-bottom:6px; }
  .row > div { flex:1; }
  .lbl { font-size:11px; color:var(--mut); text-transform:uppercase; }
  .badge { display:inline-block; padding:1px 6px; border-radius:3px; background:var(--bord); font-size:10px; text-transform:uppercase; }
  canvas { width:100% !important; max-height:200px; }
  .err { color:var(--red); }
  .mono { font-family:Menlo,Consolas,monospace; }
</style>
</head>
<body>
<header>
  <h1>SalleDesMarches V6 — Dashboard</h1>
  <div class="meta">Refresh: <span id="lastrf">…</span> · <span id="server"></span></div>
</header>

<div class="grid">
  <div class="card">
    <h2>Account &amp; Régime</h2>
    <div class="row">
      <div><div class="lbl">Total</div><div class="big" id="acct">…</div><div class="mono" id="acctbk"></div></div>
      <div><div class="lbl">Régime</div><div class="big" id="regime">…</div></div>
    </div>
    <div class="row">
      <div><div class="lbl">Cycle</div><div id="cycle" class="mono">…</div></div>
    </div>
  </div>

  <div class="card">
    <h2>Positions ouvertes</h2>
    <table id="postbl">
      <thead><tr><th>Coin</th><th>Side</th><th>Qty</th><th>Entry</th><th>uPnL</th><th>ROE%</th></tr></thead>
      <tbody></tbody>
    </table>
    <div id="poserr" class="err mono"></div>
  </div>

  <div class="card">
    <h2>Win Rate &amp; PnL</h2>
    <div class="row">
      <div><div class="lbl">7 jours</div><div class="big" id="wr7">…</div><div class="mono" id="wr7sub"></div></div>
      <div><div class="lbl">30 jours</div><div class="big" id="wr30">…</div><div class="mono" id="wr30sub"></div></div>
      <div><div class="lbl">Total</div><div class="big" id="wrall">…</div><div class="mono" id="wrallsub"></div></div>
    </div>
  </div>

  <div class="card" style="grid-column: 1/-1;">
    <h2>PnL cumulé (USDT)</h2>
    <canvas id="pnlchart"></canvas>
  </div>

  <div class="card" style="grid-column: 1/-1;">
    <h2>20 derniers trades</h2>
    <table id="trtbl">
      <thead><tr><th>Date</th><th>Coin</th><th>Side</th><th>Entry</th><th>Exit</th><th>PnL%</th><th>USDT</th><th>Dur</th><th>Cause</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <div class="card">
    <h2>Skips (raisons, dernier ~30)</h2>
    <table id="skiptbl">
      <thead><tr><th>Catégorie</th><th>Count</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <div class="card" style="grid-column: span 2;">
    <h2>Derniers SKIP</h2>
    <table id="skiplast">
      <thead><tr><th>Heure</th><th>Sym</th><th>Cat</th><th>Raison</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>

  <div class="card" style="grid-column: 1/-1;">
    <h2>Mean Reversion (Z-score H1)</h2>
    <div id="mrmeta" class="mono" style="color:#7a8595;margin-bottom:6px"></div>
    <table id="mrtbl">
      <thead><tr><th>Sym</th><th>Signal</th><th>Z</th><th>Half-life</th><th>Mean</th><th>Std</th><th>Size×</th><th>Reason</th></tr></thead>
      <tbody></tbody>
    </table>
    <div id="mrpos" style="margin-top:8px"></div>
  </div>

  <div class="card" style="grid-column: 1/-1;">
    <h2>Grid actifs (ladder par symbole)</h2>
    <div id="gridview"></div>
  </div>

  <div class="card" style="grid-column: 1/-1;">
    <h2>Open Orders (HL + source registry)</h2>
    <table id="ordtbl">
      <thead><tr><th>Coin</th><th>Side</th><th>Px</th><th>Qty</th><th>Source</th><th>Intent</th><th>Trigger</th><th>RO</th><th class="mono">OID</th></tr></thead>
      <tbody></tbody>
    </table>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script>
let chart = null;
const fmt = (n, d=2) => (n==null||isNaN(n))? '–' : Number(n).toFixed(d);
const cls = n => n>=0 ? 'grn' : 'red';
const dt = ts => new Date(ts*1000).toLocaleString('fr-FR', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'});

async function refresh() {
  try {
    const r = await fetch('/api/state', {cache:'no-store'});
    const s = await r.json();
    document.getElementById('lastrf').textContent = new Date().toLocaleTimeString();
    document.getElementById('server').textContent = 'data ts=' + dt(s.now);

    // Account (unified : total = spot + uPnL, perp value n'est pas un solde indépendant)
    document.getElementById('acct').innerHTML = '$' + fmt(s.hl.account_value, 2);
    const upnl = s.hl.upnl_total || 0;
    document.getElementById('acctbk').innerHTML =
      `spot $${fmt(s.hl.spot_usdc,2)} · uPnL <span class="${cls(upnl)}">${fmt(upnl,2)}$</span> · margin used $${fmt(s.hl.margin_used,2)}`;
    const rg = s.regime || {};
    const src = rg.source === 'live' ? '' : ' <span class="badge" style="background:#3a2b1c;color:#f5a623">fallback</span>';
    document.getElementById('regime').innerHTML = '<span class="badge">'+(rg.trend||'?')+'</span> <span class="badge">'+(rg.volatility||'?')+'</span>'+src;

    // Cycle
    const st = s.last_stats;
    document.getElementById('cycle').textContent = st
      ? `${st.ts}  analyzed=${st.analyzed}  enter=${st.entered}  skip=${st.skipped}  open=${st.open}  guards=${st.trail_guards}`
      : '–';

    // Positions
    const tb = document.querySelector('#postbl tbody');
    tb.innerHTML = '';
    (s.hl.positions || []).forEach(p => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${p.coin}</td><td>${p.side}</td><td>${fmt(p.qty,4)}</td><td>${fmt(p.entry,4)}</td>
        <td class="${cls(p.upnl)}">${fmt(p.upnl,2)}</td><td class="${cls(p.roe)}">${fmt(p.roe,2)}%</td>`;
      tb.appendChild(tr);
    });
    if ((s.hl.positions||[]).length === 0) tb.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#7a8595">aucune position</td></tr>';
    document.getElementById('poserr').textContent = s.hl.error || '';

    // WR
    for (const [k, w] of [['7', s.wr_7d], ['30', s.wr_30d], ['all', s.wr_all]]) {
      const el = document.getElementById('wr' + k);
      const sub = document.getElementById('wr' + k + 'sub');
      el.textContent = w.n ? fmt(w.wr,1) + '%' : '–';
      el.className = 'big ' + (w.pnl_usdt_sum >= 0 ? 'grn' : 'red');
      sub.innerHTML = w.n ? `n=${w.n} · <span class="${cls(w.pnl_usdt_sum)}">${fmt(w.pnl_usdt_sum,2)}$</span>` : '';
    }

    // Trades
    const tb2 = document.querySelector('#trtbl tbody');
    tb2.innerHTML = '';
    s.last_trades.forEach(t => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${dt(t.ts)}</td><td>${t.symbol}</td><td>${t.side}</td>
        <td>${fmt(t.entry,4)}</td><td>${fmt(t.exit,4)}</td>
        <td class="${cls(t.pnl_pct)}">${fmt(t.pnl_pct,2)}%</td>
        <td class="${cls(t.pnl_usdt)}">${fmt(t.pnl_usdt,2)}</td>
        <td>${t.dur_min}min</td><td>${t.cause||''}</td>`;
      tb2.appendChild(tr);
    });

    // Skip counts
    const tb3 = document.querySelector('#skiptbl tbody');
    tb3.innerHTML = '';
    const sc = s.skip_counts || {};
    Object.entries(sc).sort((a,b)=>b[1]-a[1]).forEach(([k,v]) => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${k}</td><td>${v}</td>`;
      tb3.appendChild(tr);
    });

    // Last skips
    const tb4 = document.querySelector('#skiplast tbody');
    tb4.innerHTML = '';
    (s.skips || []).slice(-15).reverse().forEach(sk => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${sk.ts}</td><td>${sk.symbol}</td><td><span class="badge">${sk.cat}</span></td><td>${sk.reason}</td>`;
      tb4.appendChild(tr);
    });

    // Mean Reversion
    const mr = s.mr || {};
    const mrTb = document.querySelector('#mrtbl tbody');
    mrTb.innerHTML = '';
    const ageTxt = mr.age_sec != null ? (mr.age_sec < 60 ? `${mr.age_sec}s` : `${Math.floor(mr.age_sec/60)}min`) : '–';
    const ageColor = (mr.age_sec != null && mr.age_sec > 600) ? 'red' : '';
    document.getElementById('mrmeta').innerHTML =
      `symbols=${(mr.symbols||[]).join(',')} · last_tick <span class="${ageColor}">${ageTxt} ago</span>`;
    const sigColor = sig => {
      if (sig === 'LONG') return 'grn';
      if (sig === 'SHORT') return 'red';
      if (sig === 'CLOSE') return 'acc';
      return 'mut';
    };
    Object.entries(mr.metrics || {}).forEach(([sym, m]) => {
      const tr = document.createElement('tr');
      const zCls = (m.z != null && Math.abs(m.z) >= 2.0) ? cls(m.z >= 0 ? -1 : 1) : '';
      tr.innerHTML = `<td>${sym}</td>
        <td class="${sigColor(m.signal)}"><span class="badge">${m.signal||'?'}</span></td>
        <td class="${zCls}">${fmt(m.z, 2)}</td>
        <td>${fmt(m.hl, 1)}</td>
        <td>${fmt(m.mean, 4)}</td>
        <td>${fmt(m.std, 4)}</td>
        <td>${fmt(m.size_factor, 2)}</td>
        <td style="color:#7a8595;font-size:11px">${m.reason||''}</td>`;
      mrTb.appendChild(tr);
    });
    if (Object.keys(mr.metrics||{}).length === 0) {
      mrTb.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#7a8595">aucun tick MR encore (intervalle 5min)</td></tr>';
    }
    // Positions MR ouvertes
    const mrPosDiv = document.getElementById('mrpos');
    const mrPos = mr.open_positions || {};
    if (Object.keys(mrPos).length === 0) {
      mrPosDiv.innerHTML = '<span style="color:#7a8595">aucune position MR active</span>';
    } else {
      mrPosDiv.innerHTML = '<div class="lbl" style="margin-bottom:4px">Positions MR ouvertes</div>' +
        Object.entries(mrPos).map(([sym, p]) =>
          `<div class="mono" style="font-size:11px"><span class="badge">${sym}</span> ${p.side} qty=${fmt(p.qty,4)} entry=${fmt(p.entry,4)} SL=${fmt(p.sl_price,4)} z@entry=${fmt(p.z_at_entry,2)} hl@entry=${fmt(p.hl_at_entry,1)}</div>`
        ).join('');
    }

    // Grid view (ladder par symbole)
    const gv = document.getElementById('gridview');
    gv.innerHTML = '';
    if (!s.grid || s.grid.length === 0) {
      gv.innerHTML = '<div style="color:#7a8595;text-align:center;padding:8px">aucun grid actif</div>';
    } else {
      s.grid.forEach(g => {
        const div = document.createElement('div');
        div.style.cssText = 'margin-bottom:10px';
        const rows = g.levels.map(l => {
          const sideClass = l.side === 'buy' ? 'grn' : 'red';
          const kindLbl = l.kind === 'tp' ? '<span class="badge" style="background:#1c3a2b;color:#3fb950">TP</span>' : '';
          return `<tr><td class="${sideClass}">${l.side}</td><td>${fmt(l.price,4)}</td><td>${fmt(l.qty,4)}</td><td>${kindLbl}</td><td class="mono" style="color:#7a8595">${l.oid}</td></tr>`;
        }).join('');
        div.innerHTML = `<div style="font-weight:600;margin-bottom:4px">${g.symbol} <span class="badge">${g.levels.length} niveaux</span></div>
          <table><thead><tr><th>Side</th><th>Prix</th><th>Qty</th><th>Type</th><th>OID</th></tr></thead><tbody>${rows}</tbody></table>`;
        gv.appendChild(div);
      });
    }

    // Open orders table
    const tbo = document.querySelector('#ordtbl tbody');
    tbo.innerHTML = '';
    (s.hl.open_orders || []).forEach(o => {
      const tr = document.createElement('tr');
      const sourceClass = o.source === 'unknown' ? 'red' : (o.source.startsWith('grid') ? 'acc' : '');
      tr.innerHTML = `<td>${o.coin}</td><td class="${o.side==='buy'?'grn':'red'}">${o.side}</td>
        <td>${fmt(o.price,4)}</td><td>${fmt(o.sz,4)}</td>
        <td class="${sourceClass}">${o.source}</td><td>${o.intent}</td>
        <td>${o.trigger?'✓':''}</td><td>${o.reduce_only?'RO':''}</td>
        <td class="mono" style="color:#7a8595">${o.oid}</td>`;
      tbo.appendChild(tr);
    });
    if ((s.hl.open_orders||[]).length === 0) tbo.innerHTML = '<tr><td colspan="9" style="text-align:center;color:#7a8595">aucun ordre ouvert</td></tr>';

    // Chart
    const pts = s.pnl_curve || [];
    const labels = pts.map(p => dt(p.ts));
    const data = pts.map(p => p.pnl_cum);
    if (!chart) {
      const ctx = document.getElementById('pnlchart').getContext('2d');
      chart = new Chart(ctx, {
        type: 'line',
        data: { labels, datasets: [{ data, borderColor:'#58a6ff', backgroundColor:'rgba(88,166,255,0.1)', fill:true, pointRadius:0, borderWidth:1.5, tension:0.1 }] },
        options: { responsive:true, plugins:{ legend:{display:false} },
          scales: { x:{ ticks:{ color:'#7a8595', maxTicksLimit:10 }, grid:{ color:'#2a313c' } },
                    y:{ ticks:{ color:'#7a8595' }, grid:{ color:'#2a313c' } } } }
      });
    } else {
      chart.data.labels = labels;
      chart.data.datasets[0].data = data;
      chart.update('none');
    }
  } catch (e) {
    document.getElementById('lastrf').textContent = 'erreur: ' + e.message;
  }
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("DASHBOARD_PORT", 8081))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")

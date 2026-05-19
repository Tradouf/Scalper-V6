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
from typing import Dict, List

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

REPO = Path(__file__).parent
SCALP_MEM = REPO / "memory" / "scalp_memory.json"
SHARED_MEM = REPO / "memory" / "shared_memory.json"
LOG_FILE = REPO / "logs" / "sdm.log"

# Charger .env pour HL_ACCOUNT_ADDRESS
for line in (REPO / ".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

HL_API = "https://api.hyperliquid.xyz/info"
HL_ADDR = os.environ.get("HL_ACCOUNT_ADDRESS", "")

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
        "perp_value": 0.0,
        "spot_usdc": 0.0,
        "account_value": 0.0,   # perp + spot (vue totale)
        "ok": False,
    }
    if not HL_ADDR:
        return out
    try:
        # 1) Compte perp
        r = requests.post(
            HL_API, json={"type": "clearinghouseState", "user": HL_ADDR}, timeout=4
        ).json()
        out["perp_value"] = float(r.get("marginSummary", {}).get("accountValue", 0) or 0)
        for p in r.get("assetPositions", []):
            pp = p.get("position", {})
            szi = float(pp.get("szi", 0) or 0)
            if szi == 0:
                continue
            entry = float(pp.get("entryPx", 0) or 0)
            upnl = float(pp.get("unrealizedPnl", 0) or 0)
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

        # 2) Compte spot (les fonds non transférés en perp restent ici)
        rs = requests.post(
            HL_API, json={"type": "spotClearinghouseState", "user": HL_ADDR}, timeout=4
        ).json()
        for b in rs.get("balances", []) or []:
            if str(b.get("coin", "")).upper() == "USDC":
                out["spot_usdc"] = float(b.get("total", 0) or 0)

        out["account_value"] = out["perp_value"] + out["spot_usdc"]
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

    return JSONResponse({
        "now": int(now),
        "hl": _hl_state(),
        "regime": shared.get("regime", {}),
        "wr_7d": _wr(trades, cut7),
        "wr_30d": _wr(trades, cut30),
        "wr_all": _wr(trades, 0),
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
        "pnl_curve": _pnl_curve(trades),
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

    // Account
    document.getElementById('acct').innerHTML = '$' + fmt(s.hl.account_value, 2);
    document.getElementById('acctbk').innerHTML =
      `perp $${fmt(s.hl.perp_value,2)} · spot $${fmt(s.hl.spot_usdc,2)}`;
    const rg = s.regime || {};
    document.getElementById('regime').innerHTML = '<span class="badge">'+(rg.trend||'?')+'</span> <span class="badge">'+(rg.volatility||'?')+'</span>';

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

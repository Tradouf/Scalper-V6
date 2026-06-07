#!/usr/bin/env python3
"""
Dashboard V7 minimaliste — FastAPI sur port 8082.

Lit `memory/v7_state.json` écrit par main.py à chaque tick. Affiche :
  - régime courant + probabilités + confidence
  - poids stratégies (allocator)
  - perf scores
  - positions paper + equity
  - last fills (depuis logs/v7.log parsing rapide)

Usage :
  cd ~/SalleDesMarches_v7
  source ../SalleDesMarches_fixed/.venv/bin/activate
  python3 monitoring/dashboard.py     # port 8082 par défaut

Pour le moment c'est un dashboard simple sans graph. Étendu plus tard.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse


STATE_FILE = REPO / "memory" / "v7_state.json"
LOG_FILE = REPO / "logs" / "v7.log"

app = FastAPI(title="SalleDesMarches V7 Dashboard")


def _read_state() -> Dict[str, Any]:
    out = {"ok": False, "age_sec": None}
    if not STATE_FILE.exists():
        return out
    try:
        data = json.loads(STATE_FILE.read_text())
        ts = float(data.get("ts", 0))
        data["age_sec"] = max(0, int(time.time() - ts))
        data["ok"] = True
        return data
    except Exception as e:
        out["error"] = str(e)[:120]
        return out


def _tail_log_ticks(n: int = 30) -> list:
    """Extrait les N derniers logs 'tick #' de logs/v7.log."""
    if not LOG_FILE.exists():
        return []
    try:
        size = LOG_FILE.stat().st_size
        with LOG_FILE.open("rb") as f:
            f.seek(max(0, size - 200_000))
            content = f.read().decode("utf-8", errors="ignore")
        lines = content.splitlines()
        ticks = [ln for ln in lines if " tick #" in ln]
        return ticks[-n:]
    except Exception:
        return []


@app.get("/api/state")
def api_state() -> JSONResponse:
    return JSONResponse({
        "now": int(time.time()),
        "state": _read_state(),
        "recent_ticks": _tail_log_ticks(30),
    })


INDEX_HTML = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>SDM V7 Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { --bg:#0f1419; --panel:#1a1f29; --fg:#d6dde6; --mut:#7a8595; --grn:#3fb950; --red:#f85149; --acc:#58a6ff; --bord:#2a313c; --orange:#ff7f0e; --blue:#1f77b4; }
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
  .mono { font-family:Menlo,Consolas,monospace; }
  .bar { display:inline-block; height:8px; background:var(--acc); border-radius:2px; }
  .bar.trend_up { background:#2ca02c; }
  .bar.trend_down { background:#d62728; }
  .bar.range { background:#1f77b4; }
  .bar.high_vol { background:#ff7f0e; }
</style>
</head>
<body>
<header>
  <h1>SalleDesMarches V7 — Dashboard <span class="badge" id="modebadge" style="background:#3a2b1c;color:#f5a623">…</span></h1>
  <div class="meta">Refresh: <span id="lastrf">…</span> · <span id="age"></span> · grille <span id="gridloop"></span></div>
</header>

<div class="grid">
  <div class="card">
    <h2>Régime de marché</h2>
    <div class="row">
      <div><div class="lbl">Label dominant</div><div class="big" id="regime">…</div></div>
      <div><div class="lbl">Confidence</div><div class="big" id="conf">…</div></div>
    </div>
    <div id="probas"></div>
  </div>

  <div class="card">
    <h2>Poids stratégies (allocator)</h2>
    <table id="wtbl"><thead><tr><th>Strat</th><th>Poids</th><th>Mult perf</th></tr></thead><tbody></tbody></table>
  </div>

  <div class="card">
    <h2>Portfolio</h2>
    <div class="row">
      <div><div class="lbl">Equity</div><div class="big" id="equity">…</div></div>
      <div><div class="lbl">Positions</div><div class="big" id="npos">…</div></div>
      <div><div class="lbl">uPnL total</div><div class="big" id="upnl">…</div></div>
    </div>
    <table id="postbl"><thead><tr><th>Asset</th><th>Notional</th><th>szi</th><th>Entry</th><th>ROE</th></tr></thead><tbody></tbody></table>
  </div>

  <div class="card">
    <h2>Allocation</h2>
    <div class="row">
      <div><div class="lbl">Target gross</div><div id="tgross" class="mono">…</div></div>
      <div><div class="lbl">Projected gross</div><div id="pgross" class="mono">…</div></div>
    </div>
    <div class="row">
      <div><div class="lbl">Target net</div><div id="tnet" class="mono">…</div></div>
      <div><div class="lbl">Projected net</div><div id="pnet" class="mono">…</div></div>
    </div>
    <div class="row">
      <div><div class="lbl">Signaux actifs / total (tick)</div><div id="signals" class="mono">…</div></div>
      <div><div class="lbl">Fills cumul / depuis boot</div><div id="fills" class="mono">…</div></div>
    </div>
  </div>

  <div class="card" style="grid-column:1/-1">
    <h2>Positions &amp; logique</h2>
    <table id="logictbl"><thead><tr><th>Asset</th><th>Stratégie</th><th>Sens</th><th>Entry</th><th>Métrique</th><th>Intent ($ · conf)</th><th>szi / ROE (live)</th></tr></thead><tbody></tbody></table>
    <div id="logic_note" style="font-size:11px;color:#7a8595;margin-top:6px"></div>
  </div>

  <div class="card" style="grid-column:1/-1">
    <h2>Grilles actives <span style="font-size:11px;color:var(--mut)" id="gridcount"></span></h2>
    <div id="gridcards" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px"></div>
    <div id="grid_note" style="font-size:11px;color:#7a8595;margin-top:6px"></div>
  </div>

  <div class="card" style="grid-column:1/-1">
    <h2>Signaux courants</h2>
    <table id="sigtbl"><thead><tr><th>Strat</th><th>Asset</th><th>Direction</th><th>Notional</th><th>Conf</th><th>Edge bps</th></tr></thead><tbody></tbody></table>
    <div id="signals_note" style="font-size:11px;color:#7a8595;margin-top:6px"></div>
  </div>

  <div class="card" style="grid-column:1/-1">
    <h2>Derniers ticks</h2>
    <pre id="ticks" class="mono" style="font-size:11px;color:var(--fg);white-space:pre-wrap;background:#0f1419;padding:8px;border-radius:4px;max-height:240px;overflow:auto"></pre>
  </div>
</div>

<script>
const fmt = (n, d=2) => (n==null||isNaN(n))? '–' : Number(n).toFixed(d);

async function refresh() {
  try {
    const r = await fetch('/api/state', {cache:'no-store'});
    const s = await r.json();
    document.getElementById('lastrf').textContent = new Date().toLocaleTimeString();
    const st = s.state || {};
    if (!st.ok) {
      document.getElementById('age').textContent = 'pas de state V7 (bot pas démarré ?)';
      return;
    }
    const age = st.age_sec;
    const ageColor = age > 90 ? 'red' : 'mut';
    document.getElementById('age').innerHTML = `cycle #${st.cycle||'?'} · last tick <span class="${ageColor}">${age}s ago</span>`;

    // Mode LIVE/PAPER + état boucle grille
    const live = st.paper_mode === false;
    const mb = document.getElementById('modebadge');
    mb.textContent = live ? 'LIVE' : 'PAPER';
    mb.style.background = live ? '#2b1c1c' : '#3a2b1c';
    mb.style.color = live ? '#e8506a' : '#f5a623';
    document.getElementById('gridloop').innerHTML = st.grid_fast_loop
      ? '<span class="grn">⚙ thread ON</span>' : '<span class="red">thread OFF</span>';

    // Régime
    const r_ = st.regime || {};
    document.getElementById('regime').innerHTML = '<span class="badge">'+(r_.label||'?')+'</span>';
    document.getElementById('conf').textContent = fmt(r_.confidence*100, 1) + '%';
    const probas = r_.probabilities || {};
    const probaHtml = Object.entries(probas).map(([k,v]) => {
      const pct = (v*100).toFixed(1);
      return `<div style="display:flex;align-items:center;gap:6px;margin:3px 0"><span style="width:90px;font-size:11px;color:var(--mut)">${k}</span><span class="bar ${k}" style="width:${Math.max(2,pct*1.5)}px"></span><span class="mono" style="font-size:11px">${pct}%</span></div>`;
    }).join('');
    document.getElementById('probas').innerHTML = probaHtml;

    // Poids stratégies
    const w = st.weights || {};
    const p = st.perf_scores || {};
    const tbW = document.querySelector('#wtbl tbody');
    tbW.innerHTML = '';
    Object.keys(w).sort().forEach(k => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${k}</td><td>${(w[k]*100).toFixed(1)}%</td><td>${fmt(p[k]||1.0, 2)}</td>`;
      tbW.appendChild(tr);
    });

    // Portfolio — en live, on privilégie les positions HL réelles (szi/entry/ROE)
    document.getElementById('equity').innerHTML = '$'+fmt(st.portfolio_equity, 2);
    const hl = st.hl_positions || {};
    const pos = st.portfolio_positions || {};
    const useHl = Object.keys(hl).length > 0;
    const assets = useHl ? Object.keys(hl) : Object.keys(pos);
    document.getElementById('npos').textContent = assets.length;
    let totalPnl = 0;
    const tbP = document.querySelector('#postbl tbody');
    tbP.innerHTML = '';
    assets.forEach(a => {
      const h = hl[a] || {};
      const szi = Number(h.szi||0), mark = Number(h.mark_px||h.entry_px||0), entry = Number(h.entry_px||0);
      const notional = useHl ? (szi * mark) : Number(pos[a]||0);
      const roe = h.roe != null ? Number(h.roe) : null;
      if (useHl && entry) totalPnl += szi * (mark - entry);   // uPnL exact
      const cls = notional > 0 ? 'grn' : 'red';
      const roeCls = roe == null ? 'mut' : (roe >= 0 ? 'grn' : 'red');
      const trHtml = `<td>${a}</td><td class="${cls}">$${fmt(notional,2)}</td>`
        + `<td class="mono">${useHl? fmt(h.szi,4):'–'}</td>`
        + `<td class="mono">${useHl? fmt(h.entry_px,4):'–'}</td>`
        + `<td class="${roeCls} mono">${roe==null?'–':fmt(roe*100,2)+'%'}</td>`;
      const tr = document.createElement('tr'); tr.innerHTML = trHtml; tbP.appendChild(tr);
    });
    if (assets.length === 0) tbP.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--mut)">aucune position</td></tr>';
    const up = document.getElementById('upnl');
    if (useHl) { up.innerHTML = '$'+fmt(totalPnl,2); up.className = 'big '+(totalPnl>=0?'grn':'red'); }
    else { up.textContent = '–'; }

    // Positions & logique
    const logic = st.positions_logic || {};
    const tbL = document.querySelector('#logictbl tbody');
    tbL.innerHTML = '';
    let nlog = 0;
    Object.keys(logic).sort().forEach(sym => {
      (logic[sym]||[]).forEach(L => {
        nlog++;
        const h = hl[sym] || {};
        const dir = L.side === 'buy' ? '🟢 LONG' : (L.side === 'sell' ? '🔴 SHORT' : '⚪');
        const mv = L.metric_value;
        const metricStr = (L.metric_name && mv != null) ? `${L.metric_name}=${fmt(mv,2)}` : '–';
        const intentStr = `$${fmt(L.intent_notional,0)} · ${fmt(L.intent_confidence,2)}`;
        const roe = h.roe != null ? Number(h.roe) : null;
        const roeCls = roe == null ? 'mut' : (roe >= 0 ? 'grn' : 'red');
        const liveStr = (h.szi != null) ? `${fmt(h.szi,3)} · <span class="${roeCls}">${roe==null?'–':fmt(roe*100,1)+'%'}</span>` : '–';
        const tr = document.createElement('tr');
        tr.innerHTML = `<td>${sym}</td><td>${L.strategy}</td><td>${dir}</td>`
          + `<td class="mono">${fmt(L.entry_px,4)}</td><td class="mono">${metricStr}</td>`
          + `<td class="mono">${intentStr}</td><td class="mono">${liveStr}</td>`;
        tbL.appendChild(tr);
      });
    });
    if (nlog === 0) tbL.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--mut)">aucune position directionnelle tracée</td></tr>';
    document.getElementById('logic_note').textContent =
      'Intent = exposition ré-émise chaque tick (maintien anti-whipsaw). Métrique = ce qui justifie la position (z=écart MR, slope=momentum, direction=supertrend).';

    // Grilles actives — échelle de niveaux par symbole (style V6)
    const grids = st.grids || {};
    const gk = Object.keys(grids).sort();
    document.getElementById('gridcount').textContent = `(${gk.length})`;
    const cards = document.getElementById('gridcards');
    cards.innerHTML = '';
    const stColor = {pending:'#7a8595', filled:'#38c172', tp_placed:'#4aa3ff', frozen:'#e3342f', done:'#3a4150'};
    gk.forEach(sym => {
      const g = grids[sym];
      const lvls = g.levels || [];
      const dec = g.center < 1 ? 5 : (g.center < 100 ? 3 : (g.center < 5000 ? 2 : 0));
      const biasTag = g.bias === 'long' ? '<span class="grn">▲ LONG</span>'
                   : g.bias === 'short' ? '<span class="red">▼ SHORT</span>'
                   : '<span class="mut">◆ neutre</span>';
      const pnl = g.pnl_cumul_pct || 0;
      const pnlCls = pnl > 0 ? 'grn' : (pnl < 0 ? 'red' : 'mut');
      const drift = g.drift ? ' · <span class="red">⚠ drift</span>' : '';
      // Échelle : niveaux + marqueur prix, triés prix décroissant
      let rows = [];
      const mark = g.mark || 0;
      let markInserted = false;
      lvls.forEach(l => {
        if (mark > 0 && !markInserted && mark > l.px) {
          rows.push(`<div style="border-top:1px dashed #e3b341;color:#e3b341;font-size:10px;text-align:right;line-height:1">${fmt(mark,dec)} ◄ prix</div>`);
          markInserted = true;
        }
        const c = stColor[l.state] || '#7a8595';
        const arrow = l.side === 'buy' ? '▸ buy' : '◂ sell';
        const tp = (l.state === 'tp_placed' && l.tp_px) ? ` → TP ${fmt(l.tp_px,dec)}` : '';
        rows.push(`<div class="mono" style="font-size:11px;color:${c};line-height:1.45">`
          + `${fmt(l.px,dec)} ${arrow} <span style="font-size:10px">[${l.state}]${tp}</span></div>`);
      });
      if (mark > 0 && !markInserted)
        rows.push(`<div style="border-top:1px dashed #e3b341;color:#e3b341;font-size:10px;text-align:right;line-height:1">${fmt(mark,dec)} ◄ prix</div>`);
      const card = document.createElement('div');
      card.style.cssText = 'background:#10151d;border:1px solid #232a36;border-radius:8px;padding:8px 10px';
      card.innerHTML = `<div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px">`
        + `<b>${sym}</b>${biasTag}</div>`
        + `<div style="font-size:10px;color:var(--mut);margin-bottom:6px">center ${fmt(g.center,dec)} · pas ${fmt(g.spacing,dec)}`
        + ` · <span class="${pnlCls}">${pnl>=0?'+':''}${fmt(pnl,2)}%</span> (${g.trades||0} tr)`
        + ` · ${fmt(g.age_min,0)}min${drift}</div>`
        + rows.join('');
      cards.appendChild(card);
    });
    if (gk.length === 0) cards.innerHTML = '<div style="color:var(--mut);text-align:center;padding:12px">aucune grille active (régime hors range/high_vol ?)</div>';
    document.getElementById('grid_note').textContent =
      'gris = limit en attente · vert = fill (TP en pose) · bleu = TP posé · rouge = frozen (szi mauvais côté, dégel auto ~3s) · ligne jaune = prix actuel. Bias ▲ = long-only (momentum 24h > +1%).';

    // Allocation
    document.getElementById('tgross').textContent = '$' + fmt(st.target_gross, 2);
    document.getElementById('pgross').textContent = '$' + fmt(st.projected_gross, 2);
    document.getElementById('tnet').textContent = '$' + fmt(st.target_net, 2);
    document.getElementById('pnet').textContent = '$' + fmt(st.projected_net, 2);
    document.getElementById('signals').textContent =
      (st.signals_active_count != null ? st.signals_active_count : '?') + ' / ' + st.signals_count;
    document.getElementById('fills').textContent =
      st.fills_count_this_cycle + ' (tick) · ' + (st.cumulative_fills != null ? st.cumulative_fills : '?') + ' (cumul)';

    // Signaux courants (détail)
    const sigBody = document.querySelector('#sigtbl tbody');
    sigBody.innerHTML = '';
    const sigs = st.signals_detail || [];
    const active = sigs.filter(s => s.target_notional > 0);
    (active.length ? active : sigs.slice(0, 5)).forEach(s => {
      const tr = document.createElement('tr');
      const dirStr = s.direction > 0 ? '🟢 LONG' : (s.direction < 0 ? '🔴 SHORT' : '⚪ flat');
      tr.innerHTML = `<td>${s.strategy}</td><td>${s.asset}</td><td>${dirStr}</td>
        <td>$${fmt(s.target_notional,2)}</td><td>${fmt(s.confidence,2)}</td><td>${fmt(s.edge_bps,1)}</td>`;
      sigBody.appendChild(tr);
    });
    document.getElementById('signals_note').textContent =
      active.length ? `${active.length} signaux actifs (target>0)` : `Tous les signaux flat — bot en wait (régime ${st.regime?.label||'?'})`;

    // Recent ticks
    const ticks = s.recent_ticks || [];
    document.getElementById('ticks').textContent = ticks.slice(-15).reverse().join('\n');
  } catch (e) {
    document.getElementById('lastrf').textContent = 'erreur: '+e.message;
  }
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("DASHBOARD_PORT", 8082))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")

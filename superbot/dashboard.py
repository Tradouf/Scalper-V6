#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dashboard SuperBot — stdlib pur, port 8084 (8083 = SimpleBot), lecture seule.

Cartes (SPEC §9) : régime marché HMM (4 états + probabilités), table régime
par symbole, equity/PnL (courbe HL3 canonique si adresse dispo), table
symboles (sleeve, TF, params, PF), positions par sleeve, stats maker/taker,
kill-switch. Auto-refresh 5 s. Basic Auth optionnel.

    python -m superbot.dashboard
    SUPERBOT_DASHBOARD_PORT=9084 SUPERBOT_DASHBOARD_PASSWORD=... python -m superbot.dashboard
"""
from __future__ import annotations

import hmac
import json
import os
import time
import urllib.request
from base64 import b64decode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from superbot import config

PORT = int(os.environ.get("SUPERBOT_DASHBOARD_PORT", "8084"))
HOST = os.environ.get("SUPERBOT_DASHBOARD_HOST", "127.0.0.1")
AUTH_USER = os.environ.get("SUPERBOT_DASHBOARD_USER", "superbot")
AUTH_PASSWORD = os.environ.get("SUPERBOT_DASHBOARD_PASSWORD", "")


def _read_json(path) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_PF_CACHE: Dict[str, Any] = {"ts": 0.0, "curves": None}


def _equity_curves(now: float) -> Optional[Dict[str, list]]:
    """Courbes canoniques HL3 (24h/7d/30d/all) — cache 90 s, fail-soft."""
    if _PF_CACHE["curves"] is not None and now - _PF_CACHE["ts"] < 90:
        return _PF_CACHE["curves"]
    addr = os.environ.get(config.ENV_ACCOUNT_ADDRESS, "").strip()
    if not addr:
        return None
    try:
        body = json.dumps({"type": "portfolio", "user": addr}).encode()
        req = urllib.request.Request("https://api.hyperliquid.xyz/info", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            pf = dict(json.load(r))
        curves = {}
        for hl_key, label in (("day", "24h"), ("week", "7d"),
                              ("month", "30d"), ("allTime", "all")):
            avh = (pf.get(hl_key) or {}).get("accountValueHistory") or []
            pts = [[t / 1000.0, float(v)] for t, v in avh]
            while pts and pts[0][1] <= 0:
                pts.pop(0)
            curves[label] = pts
        _PF_CACHE.update(ts=now, curves=curves)
    except Exception:
        pass
    return _PF_CACHE["curves"]


def build_state() -> Dict[str, Any]:
    now = time.time()
    best = _read_json(config.BEST_PARAMS_FILE)
    live = _read_json(config.LIVE_STATE_FILE)
    market = _read_json(config.REGIME_MARKET_FILE)
    sym_regimes = _read_json(config.REGIME_SYMBOLS_FILE)

    symbols = []
    for name, e in (best.get("symbols") or {}).items():
        symbols.append({
            "symbol": name, "active": bool(e.get("active")),
            "sleeve": e.get("sleeve"), "timeframe": e.get("timeframe"),
            "params": e.get("params"), "train": e.get("train"),
            "valid": e.get("valid"),
            "reason": e.get("filter_reason") or e.get("reason"),
            "hmm": sym_regimes.get(name, {}).get("state"),
            "hmm_conf": sym_regimes.get(name, {}).get("confidence"),
            "hmm_source": sym_regimes.get(name, {}).get("source"),
        })
    symbols.sort(key=lambda r: (not r["active"],
                                -((r.get("valid") or {}).get("profit_factor") or 0)))

    positions = live.get("positions") or {}
    by_sleeve: Dict[str, list] = {}
    for sym, p in positions.items():
        by_sleeve.setdefault(p.get("sleeve", "?"), []).append(dict(p, symbol=sym))

    trades = live.get("trades") or []
    wins = len([t for t in trades if t.get("pnl_pct", 0) > 0])
    eq_hist = live.get("equity_history") or []
    paused_until = float(live.get("paused_until", 0) or 0)

    return {
        "now": int(now),
        "mode": "DRY-RUN" if live.get("dry_run", True) else "LIVE",
        "updated_at": best.get("updated_at"),
        "market_regime": {k: market.get(k) for k in
                          ("state", "confidence", "transition_risk", "source",
                           "state_probs", "markov_transition_risk", "updated_at")},
        "symbol_regimes": sym_regimes,
        "symbols": symbols,
        "active_count": sum(1 for s in symbols if s["active"]),
        "positions_by_sleeve": by_sleeve,
        "n_positions": len(positions),
        "equity": live.get("equity"),
        "equity_history": eq_hist[-600:],
        "equity_curves": _equity_curves(now),
        "trades_count": len(trades),
        "winrate": (wins / len(trades)) if trades else None,
        "recent_trades": list(reversed(trades[-20:])),
        "exec_stats": live.get("exec_stats"),
        "gate_stats": live.get("gate_stats"),
        "paused": now < paused_until,
        "paused_remaining_min": max(0, int((paused_until - now) / 60)),
        "allocs": {"momentum": config.MOMENTUM_ALLOC, "adaptive_ema": config.EMA_ALLOC,
                   "breakout": config.BREAKOUT_ALLOC},
    }


INDEX_HTML = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<title>SuperBot — Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 :root{--bg:#0d1117;--panel:#161b22;--fg:#d6dde6;--mut:#7a8595;--grn:#3fb950;--red:#f85149;--acc:#58a6ff;--bord:#2a313c;--gold:#f5a623}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.4 -apple-system,Segoe UI,Roboto,sans-serif}
 header{padding:10px 18px;background:var(--panel);border-bottom:1px solid var(--bord);display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px}
 header h1{margin:0;font-size:15px}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:12px;padding:12px}
 .card{background:var(--panel);border:1px solid var(--bord);border-radius:6px;padding:12px}
 .card.wide{grid-column:1/-1}
 .card h2{margin:0 0 10px;font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.5px}
 table{width:100%;border-collapse:collapse;font-size:12px}
 th,td{padding:4px 6px;text-align:right;border-bottom:1px solid var(--bord);white-space:nowrap}
 th:first-child,td:first-child{text-align:left} th{color:var(--mut);font-weight:500}
 .grn{color:var(--grn)} .red{color:var(--red)} .acc{color:var(--acc)} .mut{color:var(--mut)}
 .big{font-size:24px;font-weight:600}
 .row{display:flex;gap:14px;margin-bottom:8px;flex-wrap:wrap} .row>div{flex:1;min-width:90px}
 .lbl{font-size:11px;color:var(--mut);text-transform:uppercase}
 .badge{display:inline-block;padding:2px 8px;border-radius:3px;background:var(--bord);font-size:10px;text-transform:uppercase;font-weight:600}
 .badge.live{background:#3a1c1c;color:var(--red)} .badge.dry{background:#3a2b1c;color:var(--gold)}
 .badge.on{background:#12331d;color:var(--grn)} .badge.off{background:#2a313c;color:var(--mut)}
 .badge.warn{background:#3a1c1c;color:var(--red)}
 .mono{font-family:Menlo,Consolas,monospace}
 .probbar{display:flex;height:14px;border-radius:3px;overflow:hidden;margin:6px 0}
 .probbar div{height:100%} .scroll{max-height:340px;overflow:auto}
 svg{display:block;width:100%;height:110px}
</style></head><body>
<header><h1>SuperBot <span class="badge" id="mode">…</span></h1>
<div class="mut" id="lastrf">…</div></header>
<div class="grid">

<div class="card">
 <h2>Régime marché — HMM BTC 4h</h2>
 <div class="row">
  <div><div class="lbl">État</div><div class="big" id="mstate">…</div></div>
  <div><div class="lbl">Confiance</div><div class="big" id="mconf">…</div></div>
  <div><div class="lbl">Transition</div><div class="big" id="mrisk">…</div></div>
 </div>
 <div class="probbar" id="mprobs"></div>
 <div class="mut" id="mmeta" style="font-size:11px"></div>
</div>

<div class="card">
 <h2>Equity paper + kill-switch</h2>
 <div class="row">
  <div><div class="lbl">Equity</div><div class="big" id="equity">…</div></div>
  <div><div class="lbl">Trades</div><div class="big" id="ntr">…</div></div>
  <div><div class="lbl">Winrate</div><div class="big" id="wr">…</div></div>
  <div><div class="lbl">Kill</div><div class="big"><span class="badge" id="kill">…</span></div></div>
 </div>
 <svg id="eqchart" viewBox="0 0 1000 110" preserveAspectRatio="none"></svg>
 <div class="mut" id="execs" style="font-size:11px"></div>
</div>

<div class="card wide">
 <h2>Régimes par symbole (HMM K=3)</h2>
 <table id="regtbl"><thead><tr><th>Symbole</th><th>État</th><th>Conf</th><th>Risk</th><th>Long</th><th>Short</th><th>Source</th><th>TF</th></tr></thead><tbody></tbody></table>
</div>

<div class="card wide">
 <h2>Symboles — walk-forward multi-sleeves</h2>
 <div class="scroll"><table id="symtbl"><thead><tr>
  <th>Symbole</th><th>État</th><th>Sleeve</th><th>TF</th><th>Params</th>
  <th>PF train</th><th>PF valid</th><th>PnL valid</th><th>WR</th><th>n</th><th>HMM</th><th>Motif</th>
 </tr></thead><tbody></tbody></table></div>
</div>

<div class="card wide">
 <h2>Positions ouvertes par sleeve</h2>
 <div id="poswrap"></div>
</div>

<div class="card wide" id="trcard">
 <h2>Derniers trades</h2>
 <div class="scroll"><table id="trtbl"><thead><tr>
  <th>Symbole</th><th>Sleeve</th><th>Sens</th><th>Entrée</th><th>Sortie</th><th>PnL</th><th>Motif</th>
 </tr></thead><tbody></tbody></table></div>
</div>

</div>
<script>
const fmt=(n,d=2)=>(n==null||isNaN(n))?'–':Number(n).toFixed(d);
const usd=(n)=>(n==null||isNaN(n))?'–':'$'+Number(n).toFixed(2);
const pct=(n,d=2)=>(n==null||isNaN(n))?'–':((n*100>=0?'+':'')+(n*100).toFixed(d)+'%');
const cls=(n)=>(n==null||isNaN(n))?'mut':(n>=0?'grn':'red');
const COLORS={bull_orderly:'#3fb950',bear_orderly:'#f85149',range_compressed:'#58a6ff',high_vol_chaotic:'#f5a623'};

function drawEq(hist){
 const svg=document.getElementById('eqchart');
 if(!hist||hist.length<2){svg.innerHTML='';return}
 const W=1000,H=110,p=6,ys=hist.map(x=>x[1]);
 let lo=Math.min(...ys),hi=Math.max(...ys); if(hi===lo){hi+=1;lo-=1}
 const X=i=>p+i*(W-2*p)/(hist.length-1), Y=v=>H-p-(v-lo)*(H-2*p)/(hi-lo);
 let d=''; hist.forEach((pt,i)=>{d+=(i?'L':'M')+X(i).toFixed(1)+' '+Y(pt[1]).toFixed(1)+' '});
 const c=ys[ys.length-1]>=ys[0]?'#3fb950':'#f85149';
 svg.innerHTML=`<path d="${d}" fill="none" stroke="${c}" stroke-width="1.6"/>`;
}

async function refresh(){
 let s; try{ s=await (await fetch('/api/state',{cache:'no-store'})).json() }
 catch(e){ document.getElementById('lastrf').textContent='erreur: '+e.message; return }
 document.getElementById('lastrf').textContent=new Date().toLocaleTimeString();
 const m=document.getElementById('mode'); m.textContent=s.mode;
 m.className='badge '+(s.mode==='LIVE'?'live':'dry');

 const mr=s.market_regime||{};
 const ms=document.getElementById('mstate'); ms.textContent=mr.state||'–';
 ms.style.color=COLORS[mr.state]||'var(--fg)';
 document.getElementById('mconf').textContent=fmt(mr.confidence,2);
 document.getElementById('mrisk').textContent=fmt(mr.transition_risk,2);
 const probs=mr.state_probs||{}; const bar=document.getElementById('mprobs'); bar.innerHTML='';
 Object.entries(probs).forEach(([k,v])=>{const d=document.createElement('div');
  d.style.width=(v*100)+'%'; d.style.background=COLORS[k]||'#555'; d.title=k+' '+(v*100).toFixed(0)+'%'; bar.appendChild(d)});
 document.getElementById('mmeta').textContent='source '+(mr.source||'–')+' · markov risk '+fmt(mr.markov_transition_risk,2)+' · '+(mr.updated_at||'');

 document.getElementById('equity').textContent=usd(s.equity);
 document.getElementById('ntr').textContent=s.trades_count??0;
 document.getElementById('wr').textContent=s.winrate==null?'–':(s.winrate*100).toFixed(0)+'%';
 const k=document.getElementById('kill');
 if(s.paused){k.textContent='PAUSE '+s.paused_remaining_min+'min';k.className='badge warn'}
 else{k.textContent='armé';k.className='badge on'}
 drawEq(s.equity_history);
 const ex=s.exec_stats||{}; const ga=s.gate_stats||{};
 document.getElementById('execs').textContent=
  'exec maker '+(ex.maker||0)+' / taker '+(ex.taker||0)+' / mixed '+(ex.mixed||0)
  +' · gates: '+Object.entries(ga).map(([k,v])=>k+':'+v).join(' ');

 const tbR=document.querySelector('#regtbl tbody'); tbR.innerHTML='';
 Object.entries(s.symbol_regimes||{}).forEach(([sym,r])=>{
  const tr=document.createElement('tr');
  const sc=r.state==='trending_up'?'grn':(r.state==='trending_down'?'red':'mut');
  tr.innerHTML=`<td>${sym}</td><td class="${sc}">${r.state||'–'}</td>
   <td class="mono">${fmt(r.confidence,2)}</td><td class="mono">${fmt(r.transition_risk,2)}</td>
   <td>${(r.allowed||{}).long?'✅':'❌'}</td><td>${(r.allowed||{}).short?'✅':'❌'}</td>
   <td class="mut">${r.source||'–'}</td><td class="mono">${r.timeframe||'–'}</td>`;
  tbR.appendChild(tr)});
 if(!Object.keys(s.symbol_regimes||{}).length) tbR.innerHTML='<tr><td colspan="8" class="mut" style="text-align:center">aucun régime symbole encore calculé</td></tr>';

 const tbS=document.querySelector('#symtbl tbody'); tbS.innerHTML='';
 (s.symbols||[]).forEach(r=>{
  const st=r.active?'<span class="badge on">actif</span>':'<span class="badge off">inactif</span>';
  const tv=r.train||{},vv=r.valid||{};
  const tr=document.createElement('tr');
  tr.innerHTML=`<td><b>${r.symbol}</b></td><td>${st}</td><td>${r.sleeve||'–'}</td>
   <td class="mono">${r.timeframe||'–'}</td>
   <td class="mono" style="font-size:10px">${r.params?JSON.stringify(r.params).slice(1,-1).replaceAll('"',''):'–'}</td>
   <td class="mono">${fmt(tv.profit_factor,2)}</td>
   <td class="mono ${vv.profit_factor>=1.4?'grn':''}">${fmt(vv.profit_factor,2)}</td>
   <td class="mono ${cls(vv.total_pnl_pct)}">${vv.total_pnl_pct!=null?pct(vv.total_pnl_pct):'–'}</td>
   <td class="mono">${vv.winrate!=null?(vv.winrate*100).toFixed(0)+'%':'–'}</td>
   <td class="mono">${vv.n_trades??'–'}</td>
   <td class="mut">${r.hmm||'–'}</td>
   <td class="mut" style="text-align:left;white-space:normal">${r.reason||''}</td>`;
  tbS.appendChild(tr)});

 const pw=document.getElementById('poswrap'); pw.innerHTML='';
 const pbs=s.positions_by_sleeve||{};
 if(!Object.keys(pbs).length) pw.innerHTML='<div class="mut">aucune position ouverte</div>';
 Object.entries(pbs).forEach(([sleeve,arr])=>{
  const h=document.createElement('div'); h.className='lbl'; h.style.margin='6px 0 2px';
  h.textContent=sleeve+' ('+arr.length+') — alloc '+(((s.allocs||{})[sleeve]||0)*100)+'%';
  pw.appendChild(h);
  const t=document.createElement('table');
  t.innerHTML='<thead><tr><th>Symbole</th><th>Sens</th><th>Entrée</th><th>SL</th><th>TP</th><th>TF</th><th>Marge</th></tr></thead>';
  const tb=document.createElement('tbody');
  arr.forEach(p=>{const tr=document.createElement('tr');
   tr.innerHTML=`<td>${p.symbol}</td><td class="${p.dir>0?'grn':'red'}">${p.dir>0?'LONG':'SHORT'}</td>
   <td class="mono">${fmt(p.entry,5)}</td><td class="mono">${fmt(p.sl,5)}</td>
   <td class="mono">${p.tp!=null?fmt(p.tp,5):'—'}</td><td class="mono">${p.timeframe||''}</td>
   <td class="mono">${usd(p.margin)}</td>`; tb.appendChild(tr)});
  t.appendChild(tb); pw.appendChild(t)});

 const tbT=document.querySelector('#trtbl tbody'); tbT.innerHTML='';
 (s.recent_trades||[]).forEach(t=>{const tr=document.createElement('tr');
  tr.innerHTML=`<td>${t.symbol}</td><td>${t.sleeve}</td>
   <td class="${t.dir>0?'grn':'red'}">${t.dir>0?'LONG':'SHORT'}</td>
   <td class="mono">${fmt(t.entry,5)}</td><td class="mono">${fmt(t.exit,5)}</td>
   <td class="mono ${cls(t.pnl_pct)}">${pct(t.pnl_pct)}</td>
   <td><span class="badge off">${t.reason||''}</span></td>`;
  tbT.appendChild(tr)});
 document.getElementById('trcard').style.display=(s.recent_trades||[]).length?'':'none';
}
refresh(); setInterval(refresh,5000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not AUTH_PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            user, _, pwd = b64decode(header[6:]).decode("utf-8").partition(":")
        except Exception:
            return False
        return (hmac.compare_digest(user, AUTH_USER)
                and hmac.compare_digest(pwd, AUTH_PASSWORD))

    def do_GET(self) -> None:
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="SuperBot"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/state":
            try:
                self._send(200, json.dumps(build_state()).encode(), "application/json")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")


def main() -> int:
    host = HOST
    if not AUTH_PASSWORD and host not in ("127.0.0.1", "localhost", "::1"):
        print("⚠️  SUPERBOT_DASHBOARD_PASSWORD requis pour bind hors localhost "
              f"— repli sur 127.0.0.1 (demandé: {host}).")
        host = "127.0.0.1"
    server = ThreadingHTTPServer((host, PORT), Handler)
    auth = "🔒 Basic Auth" if AUTH_PASSWORD else "🔓 local uniquement"
    print(f"Dashboard SuperBot — {auth} — http://localhost:{PORT}/")
    if not AUTH_PASSWORD:
        print("  Définir SUPERBOT_DASHBOARD_PASSWORD pour l'accès LAN.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

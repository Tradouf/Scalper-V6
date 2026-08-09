"""
Dashboard XSMom — momentum cross-sectionnel, lecture seule, port 8086.

Ce que la page montre, et pourquoi chaque bloc existe :

  - la NEUTRALITÉ au marché : longs et shorts doivent s'équilibrer. C'est
    l'hypothèse centrale de la stratégie — si l'écart se creuse, le bot n'est
    plus neutre et son résultat dépend de la direction du marché, ce qu'il
    n'est pas censé faire ;
  - l'exposition NETTE par symbole, agrégée sur les 7 tranches : un même
    symbole peut être long dans une tranche et short dans une autre, donc les
    lire tranche par tranche donne une image fausse du risque réel ;
  - l'avancement vers le VERDICT (mi-septembre) et la performance en bps/jour,
    comparée au critère fixé d'avance (+5 à 10 bps/j) — pour qu'on ne
    redéfinisse pas le critère après coup ;
  - le remplissage des 7 tranches et celle qui tourne aujourd'hui : une
    tranche incomplète signale un problème de données ou de notionnel minimum.

Les prix viennent d'un unique appel `allMids` (tous les symboles d'un coup),
donc rafraîchir la page ne martèle pas l'API.

Usage : python -m xsmom.dashboard    (XSMOM_DASHBOARD_PORT pour changer)
"""
from __future__ import annotations

import hmac
import json
import os
import socket
import sys
import time
import urllib.request
from base64 import b64decode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from docpage import render_doc  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
STATE_DIR = REPO / "xsmom" / "state"
PAPER_STATE = Path(os.environ.get("XSMOM_STATE_FILE",
                                  STATE_DIR / "xsmom_state.json"))
LIVE_STATE = Path(os.environ.get("XSMOM_LIVE_STATE_FILE",
                                 STATE_DIR / "xsmom_live_state.json"))

PORT = int(os.environ.get("XSMOM_DASHBOARD_PORT", "8086"))
HOST = os.environ.get("XSMOM_DASHBOARD_HOST", "127.0.0.1")
AUTH_USER = os.environ.get("XSMOM_DASHBOARD_USER", "xsmom")
AUTH_PASSWORD = os.environ.get("XSMOM_DASHBOARD_PASSWORD", "")
DOC_FILE = Path(os.environ.get("SDM_DOC_FILE", REPO / "ANALYSE_FONCTIONNELLE.md"))

N_TRANCHES = 7
VERDICT_TS = 1789430400.0          # 2026-09-15, critère fixé d'avance
TARGET_BPS_DAY = (5.0, 10.0)       # fourchette attendue par le backtest
_MIDS: Dict[str, Any] = {"ts": 0.0, "data": {}}
MIDS_TTL = 20.0


def _is_private_host(host: str) -> bool:
    if host in ("127.0.0.1", "::1", "localhost"):
        return True
    return host.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18.",
                            "172.19.", "172.2", "172.30.", "172.31."))


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


def all_mids() -> Dict[str, float]:
    """Tous les prix en UN appel — mis en cache 20 s."""
    now = time.time()
    if now - _MIDS["ts"] < MIDS_TTL and _MIDS["data"]:
        return _MIDS["data"]
    try:
        req = urllib.request.Request(
            "https://api.hyperliquid.xyz/info",
            data=json.dumps({"type": "allMids"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = json.load(r)
        data = {k: float(v) for k, v in raw.items()}
    except Exception:
        data = _MIDS["data"]          # on garde le dernier bon jeu de prix
    _MIDS["data"] = data
    _MIDS["ts"] = now
    return data


def build_state() -> Dict[str, Any]:
    now = time.time()
    paper = _read_json(PAPER_STATE)
    live = _read_json(LIVE_STATE)
    mids = all_mids()
    tranches: List[Dict[str, Any]] = paper.get("tranches") or []

    # ── exposition agrégée par symbole (un symbole peut vivre dans
    #    plusieurs tranches, éventuellement dans les deux sens) ──────────────
    per_sym: Dict[str, Dict[str, Any]] = {}
    gross_long = gross_short = unreal = 0.0
    n_pos = 0
    for k, tr in enumerate(tranches):
        for sym, p in (tr or {}).items():
            d = int(p.get("dir", 0))
            ntl = float(p.get("notional", 0.0))
            entry = float(p.get("entry", 0.0))
            mid = mids.get(sym)
            u = (d * (mid - entry) / entry * ntl
                 if mid and entry > 0 else None)
            e = per_sym.setdefault(sym, {"sym": sym, "net": 0.0, "gross": 0.0,
                                         "tranches": [], "upnl": 0.0,
                                         "has_upnl": False})
            e["net"] += d * ntl
            e["gross"] += ntl
            e["tranches"].append(k)
            if u is not None:
                e["upnl"] += u
                e["has_upnl"] = True
                unreal += u
            gross_long += ntl if d > 0 else 0.0
            gross_short += ntl if d < 0 else 0.0
            n_pos += 1

    rows = sorted(per_sym.values(), key=lambda x: -abs(x["net"]))
    gross = gross_long + gross_short
    equity = float(paper.get("equity") or 0.0)

    # ── performance rapportée au critère fixé d'avance ──────────────────────
    started = float(paper.get("started_at") or now)
    days = max((now - started) / 86_400.0, 1e-9)
    start_eq = 1000.0
    hist = paper.get("equity_history") or []
    if hist:
        start_eq = float(hist[0][1])
    pnl = equity - start_eq
    bps_day = (pnl / start_eq) * 1e4 / days if start_eq > 0 else 0.0

    rebs = (paper.get("rebalances") or [])[-8:]
    rebs = [{"day": r.get("day"), "tranche": r.get("tranche"),
             "realized": r.get("realized_usd"), "equity": r.get("equity"),
             "longs": r.get("longs"), "shorts": r.get("shorts")}
            for r in reversed(rebs)]

    return {
        "now": now,
        "bot": "XSMom — momentum cross-sectionnel",
        "mode": "live" if live and not live.get("dry_run", True) else "paper",
        "armed": bool(live) and not live.get("dry_run", True),
        "equity": equity,
        "start_equity": start_eq,
        "pnl": pnl,
        "days": days,
        "bps_day": bps_day,
        "target_bps": list(TARGET_BPS_DAY),
        "verdict_days": max(0.0, (VERDICT_TS - now) / 86_400.0),
        "fees_paid": paper.get("fees_paid"),
        "funding_net": paper.get("funding_net"),
        "n_positions": n_pos,
        "gross_long": gross_long,
        "gross_short": gross_short,
        "gross": gross,
        "net_exposure": gross_long - gross_short,
        "net_pct_equity": ((gross_long - gross_short) / equity * 100.0
                           if equity > 0 else None),
        "unrealized": unreal,
        "tranche_today": int(now // 86_400) % N_TRANCHES,
        "tranche_fill": [len(t or {}) for t in tranches],
        "last_rebalance_day": paper.get("last_rebalance_day"),
        "symbols": rows,
        "rebalances": rebs,
        "equity_curve": [[t, v] for t, v in hist][-60:],
    }


INDEX_HTML = """<!doctype html>
<meta charset="utf-8"><title>XSMom — momentum cross-sectionnel</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1115;--card:#171a21;--fg:#e6e8ee;--mut:#9aa3b2;--ok:#38b26b;
--bad:#e0554e;--warn:#d9a441;--line:#242833}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:20px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--mut);margin-bottom:18px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.k{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.v{font-size:22px;font-weight:600;margin-top:2px}
.hint{color:var(--mut);font-size:12px;margin-top:2px}
.badge{display:inline-block;padding:3px 10px;border-radius:99px;font-weight:600;font-size:12px}
.paper{background:rgba(56,178,107,.12);color:var(--ok);border:1px solid var(--ok)}
.live{background:rgba(224,85,78,.15);color:var(--bad);border:1px solid var(--bad)}
table{width:100%;border-collapse:collapse;margin-top:8px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:500;font-size:12px}
td.num,th.num{text-align:right}
.pos{color:var(--ok)}.neg{color:var(--bad)}.mut{color:var(--mut)}
.sec{margin-top:22px}
.scroll{overflow-x:auto}
.doclink{margin:-2px 0 12px}
.doclink a{color:#6ea8fe;text-decoration:none;font-size:13px}
.doclink a:hover{text-decoration:underline}
.bar{height:8px;background:#20242e;border-radius:4px;overflow:hidden;margin-top:6px}
.bar>span{display:block;height:100%}
.alert{background:rgba(217,164,65,.12);border:1px solid var(--warn);color:#f0d08a;
padding:10px 12px;border-radius:8px;margin-top:10px}
</style>
<div class="wrap">
  <h1>XSMom <span class="mut">— acheter les meilleurs, vendre les pires</span></h1>
  <div class="doclink"><a href="/doc">Comment ce bot fonctionne — analyse fonctionnelle</a></div>
  <div class="sub" id="sub">chargement…</div>
  <div class="grid" id="tiles"></div>
  <div id="alerts"></div>

  <div class="sec"><div class="k">Neutralité au marché</div>
    <div class="card" id="neutral"></div></div>

  <div class="sec"><div class="k">Exposition nette par symbole (toutes tranches confondues)</div>
    <div class="scroll"><table id="syms"></table></div></div>

  <div class="sec"><div class="k">Les 7 tranches</div>
    <div class="scroll"><table id="tr"></table></div></div>

  <div class="sec"><div class="k">Derniers rééquilibrages</div>
    <div class="scroll"><table id="reb"></table></div></div>
</div>
<script>
const f=(v,d=2)=>v==null?'—':Number(v).toFixed(d);
const money=v=>v==null?'—':(v>=0?'+':'')+Number(v).toFixed(2)+' $';
const cls=v=>v==null?'mut':(v>0?'pos':(v<0?'neg':'mut'));
async function tick(){
 let s; try{ s=await (await fetch('/api/state',{cache:'no-store'})).json(); }
 catch(e){ document.getElementById('sub').textContent='API injoignable'; return; }
 document.getElementById('sub').innerHTML =
  `<span class="badge ${s.armed?'live':'paper'}">${s.armed?'ORDRES RÉELS':'PAPIER — aucun ordre réel'}</span>`
  +` &nbsp; ${f(s.days,0)} j de test &nbsp; verdict dans ${f(s.verdict_days,0)} j`
  +` &nbsp; maj ${new Date(s.now*1000).toLocaleTimeString()}`;

 const inRange = s.bps_day>=s.target_bps[0] && s.bps_day<=s.target_bps[1];
 document.getElementById('tiles').innerHTML = [
  ['Equity', f(s.equity)+' $', `départ ${f(s.start_equity,0)} $`],
  ['Résultat', money(s.pnl), `dont latent ${money(s.unrealized)}`],
  ['Par jour', f(s.bps_day,1)+' bps',
   `attendu ${s.target_bps[0]}–${s.target_bps[1]} bps` + (inRange?' ✓':'')],
  ['Positions', s.n_positions, `${s.tranche_fill.length} tranches`],
  ['Frais / financement', money(-(s.fees_paid||0)) + ' / ' + money(s.funding_net),
   'depuis le début'],
 ].map(([k,v,h])=>`<div class="card"><div class="k">${k}</div><div class="v">${v}</div>
   <div class="hint">${h||''}</div></div>`).join('');

 let al='';
 if(Math.abs(s.net_pct_equity||0)>15) al+=`<div class="alert">Déséquilibre longs/shorts :
   exposition nette ${f(s.net_pct_equity,1)} % de l'equity — la stratégie n'est plus neutre au marché.</div>`;
 const incomplete = s.tranche_fill.filter(n=>n>0 && n<16).length;
 if(incomplete) al+=`<div class="alert">${incomplete} tranche(s) incomplète(s) — données manquantes ou notionnel sous le minimum.</div>`;
 document.getElementById('alerts').innerHTML=al;

 const L=s.gross_long, S=s.gross_short, T=Math.max(L+S,1e-9);
 document.getElementById('neutral').innerHTML =
  `<div>Achats <b>${f(L)} $</b> &nbsp;·&nbsp; Ventes à découvert <b>${f(S)} $</b>
   &nbsp;·&nbsp; écart <b class="${cls(-Math.abs(s.net_exposure))}">${f(s.net_exposure)} $</b>
   (${f(s.net_pct_equity,1)} % de l'equity)</div>
   <div class="bar"><span style="width:${100*L/T}%;background:var(--ok)"></span></div>
   <div class="hint">Les deux côtés doivent rester proches : le gain vient de l'écart
   entre gagnants et perdants, pas du sens du marché.</div>`;

 document.getElementById('syms').innerHTML =
  '<tr><th>Symbole</th><th>Sens</th><th class="num">Exposition nette</th>'
  +'<th class="num">Brute</th><th class="num">Tranches</th><th class="num">Latent</th></tr>'
  + (s.symbols.length?s.symbols.map(x=>`<tr><td>${x.sym}</td>
     <td class="${x.net>0?'pos':'neg'}">${x.net>0?'achat':'vente'}</td>
     <td class="num">${f(Math.abs(x.net))} $</td><td class="num">${f(x.gross)} $</td>
     <td class="num">${x.tranches.length}</td>
     <td class="num ${cls(x.has_upnl?x.upnl:null)}">${x.has_upnl?money(x.upnl):'—'}</td></tr>`).join('')
   :'<tr><td colspan="6" class="mut">aucune position</td></tr>');

 document.getElementById('tr').innerHTML =
  '<tr><th>Tranche</th><th class="num">Positions</th><th>État</th></tr>'
  + s.tranche_fill.map((n,i)=>`<tr><td>${i}</td><td class="num">${n}</td>
    <td>${i===s.tranche_today?'<b>renouvelée aujourd\\'hui</b>':'en cours'}</td></tr>`).join('');

 document.getElementById('reb').innerHTML =
  '<tr><th>Jour</th><th class="num">Tranche</th><th class="num">Réalisé</th>'
  +'<th class="num">Equity</th><th>Achats</th><th>Ventes</th></tr>'
  + (s.rebalances.length?s.rebalances.map(r=>`<tr><td>${r.day}</td>
     <td class="num">${r.tranche}</td><td class="num ${cls(r.realized)}">${money(r.realized)}</td>
     <td class="num">${f(r.equity)} $</td>
     <td class="mut">${(r.longs||[]).slice(0,4).join(' ')}</td>
     <td class="mut">${(r.shorts||[]).slice(0,4).join(' ')}</td></tr>`).join('')
   :'<tr><td colspan="6" class="mut">aucun rééquilibrage</td></tr>');
}
tick(); setInterval(tick, 15000);
</script>
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
            self.send_header("WWW-Authenticate", 'Basic realm="XSMom"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode("utf-8"),
                       "text/html; charset=utf-8")
        elif path == "/doc":
            self._send(200, render_doc(DOC_FILE), "text/html; charset=utf-8")
        elif path == "/api/state":
            try:
                self._send(200, json.dumps(build_state()).encode(),
                           "application/json")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode(),
                           "application/json")
        else:
            self._send(404, b"not found", "text/plain")


def main() -> int:
    host = HOST
    if not AUTH_PASSWORD and not (host in ("0.0.0.0", "::")
                                  or _is_private_host(host)):
        print(f"⚠️  Bind {host} refusé sans XSMOM_DASHBOARD_PASSWORD "
              f"— repli sur 127.0.0.1")
        host = "127.0.0.1"
    if host in ("0.0.0.0", "::"):
        class _Dual(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        server = _Dual(("::", PORT), Handler)
        try:
            server.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
    else:
        server = ThreadingHTTPServer((host, PORT), Handler)
    print(f"Dashboard XSMom — http://localhost:{PORT}/")
    if host in ("0.0.0.0", "::"):
        print(f"  LAN : http://{_lan_ip()}:{PORT}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Dashboard RSI-MR (« Ricochet ») — lecture seule, port 8085.

Remplace le dashboard SimpleBot : le wallet HL2 n'exécute plus l'EMA-cross
(arrêté le 07-08) mais le rachat de survente RSI-MR. L'ancien dashboard
affichait donc des positions RSI-MR sous une grille d'optimiseur qui n'existe
plus.

Ce qu'il montre, et pourquoi :
  - le MODE (ordres réels vs dry-run), lu dans l'état, jamais deviné ;
  - l'equity via l'endpoint `portfolio` (compte unifié : le solde spot
    collatéralise les perps — `marginSummary.accountValue` ne montrerait que
    la marge engagée) ;
  - les positions du bot CROISÉES avec les positions réellement ouvertes sur
    l'exchange : toute divergence est un bug d'exécution, c'est le contrôle
    le plus important de la page ;
  - les compteurs de signaux SAUTÉS par raison — sans eux, un bot qui ne
    trade rien ressemble à un bot sans signal ;
  - le PAPER en parallèle, qui reste le juge en aveugle jusqu'à mi-septembre.

Usage : python -m rsimr.dashboard    (RSIMR_DASHBOARD_PORT pour changer)
"""
from __future__ import annotations

import hmac
import json
import os
import socket
import sqlite3
import time
import urllib.request
from base64 import b64decode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
STATE_DIR = REPO / "rsimr" / "state"
LIVE_STATE = Path(os.environ.get("RSIMR_LIVE_STATE_FILE",
                                 STATE_DIR / "rsimr_live_state.json"))
PAPER_STATE = Path(os.environ.get("RSIMR_STATE_FILE",
                                  STATE_DIR / "rsimr_state.json"))
LIQ_DB = Path(os.environ.get("LIQFEED_DB", REPO / "rsimr" / "liq.db"))

PORT = int(os.environ.get("RSIMR_DASHBOARD_PORT", "8085"))
HOST = os.environ.get("RSIMR_DASHBOARD_HOST", "127.0.0.1")
AUTH_USER = os.environ.get("RSIMR_DASHBOARD_USER", "rsimr")
AUTH_PASSWORD = os.environ.get("RSIMR_DASHBOARD_PASSWORD", "")

H_BARS = 4
REGIME_LABEL = {0: "calme (exclu)", 1: "normal", 2: "tempête"}
REGIME_SIZE = {0: 0.0, 1: 1.00, 2: 0.55}
_CACHE: Dict[str, Any] = {"ts": 0.0, "hl": None}
HL_TTL = 20.0


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


def _master_address() -> str:
    addr = (os.environ.get("HL2_ACCOUNT_ADDRESS") or "").strip()
    if addr:
        return addr
    env = REPO / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("HL2_ACCOUNT_ADDRESS="):
                return line.split("=", 1)[1].strip()
    return ""


def _hl_info(body: Dict[str, Any]) -> Any:
    req = urllib.request.Request(
        "https://api.hyperliquid.xyz/info", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.load(r)


def _hl_snapshot() -> Dict[str, Any]:
    """Equity unifiée + positions réellement ouvertes. Mis en cache 20 s."""
    now = time.time()
    if _CACHE["hl"] is not None and now - _CACHE["ts"] < HL_TTL:
        return _CACHE["hl"]
    addr = _master_address()
    out: Dict[str, Any] = {"address": addr, "equity": None, "positions": [],
                           "error": None}
    if addr:
        try:
            pf = _hl_info({"type": "portfolio", "user": addr})
            for period, data in pf:
                if period == "day":
                    avh = data.get("accountValueHistory", [])
                    if avh:
                        out["equity"] = float(avh[-1][1])
            st = _hl_info({"type": "clearinghouseState", "user": addr})
            for p in st.get("assetPositions", []):
                q = p["position"]
                out["positions"].append({
                    "coin": q["coin"], "szi": float(q["szi"]),
                    "notional": float(q["positionValue"]),
                    "upnl": float(q["unrealizedPnl"]),
                    "entry": float(q.get("entryPx") or 0)})
        except Exception as e:
            out["error"] = str(e)
    _CACHE["hl"] = out
    _CACHE["ts"] = now
    return out


def _liq_stats() -> Dict[str, Any]:
    if not LIQ_DB.exists():
        return {"available": False}
    try:
        con = sqlite3.connect(f"file:{LIQ_DB}?mode=ro", uri=True, timeout=5)
        n_liq = con.execute("SELECT COUNT(*) FROM liq").fetchone()[0]
        n_sec = con.execute("SELECT COUNT(*) FROM sec").fetchone()[0]
        coins = con.execute("SELECT COUNT(DISTINCT coin) FROM sec").fetchone()[0]
        probes = con.execute("SELECT COUNT(*) FROM probe").fetchone()[0]
        hits = con.execute(
            "SELECT COALESCE(SUM(n_liq),0) FROM probe").fetchone()[0]
        last = con.execute(
            "SELECT ts, coin FROM liq ORDER BY ts DESC LIMIT 1").fetchone()
        con.close()
        return {"available": True, "n_liq": n_liq, "n_sec": n_sec,
                "coins": coins, "probes": probes, "probe_hits": hits,
                "last": {"ts": last[0], "coin": last[1]} if last else None}
    except Exception as e:
        return {"available": False, "error": str(e)}


def build_state() -> Dict[str, Any]:
    now = time.time()
    live = _read_json(LIVE_STATE)
    paper = _read_json(PAPER_STATE)
    hl = _hl_snapshot()

    # Positions = UNION de ce que croit le bot et de ce qui est réellement
    # ouvert sur l'exchange. Ne montrer que l'état du bot masquerait
    # justement le cas qui compte : une position qu'il ignore.
    bot_pos = live.get("positions") or {}
    ex_by_coin = {x["coin"]: x for x in hl["positions"]}
    positions = []
    for sym in sorted(set(bot_pos) | set(ex_by_coin)):
        p = bot_pos.get(sym)
        ex = ex_by_coin.get(sym)
        row: Dict[str, Any] = {
            "sym": sym,
            "known_by_bot": p is not None,
            "on_exchange": ex is not None,
            "upnl": ex["upnl"] if ex else None,
            "size": ex["szi"] if ex else None,
            "notional": (p or {}).get("notional") if p else (
                ex["notional"] if ex else None),
            "entry": (p or {}).get("entry") if p else (ex["entry"] if ex else None),
            "regime": (p or {}).get("regime"),
            "regime_label": REGIME_LABEL.get((p or {}).get("regime"), "—"),
        }
        if p:
            age_h = (now * 1000 - float(p.get("opened_ms", 0))) / 3_600_000
            row["age_h"] = round(age_h, 2)
            row["remaining_h"] = round(max(0.0, H_BARS - age_h), 2)
        else:
            row["age_h"] = None
            row["remaining_h"] = None
        positions.append(row)
    positions.sort(key=lambda x: (x["remaining_h"] is None,
                                  x["remaining_h"] or 0))

    # divergence bot <-> exchange : le contrôle le plus important
    bot_syms = set((live.get("positions") or {}).keys())
    ex_syms = {x["coin"] for x in hl["positions"]}
    divergence = {"bot_seul": sorted(bot_syms - ex_syms),
                  "exchange_seul": sorted(ex_syms - bot_syms)}

    n_paper = int(paper.get("n_trades") or 0)
    paper_block = {
        "n_trades": n_paper,
        "n_wins": paper.get("n_wins"),
        "realized_usd": paper.get("realized_usd"),
        "avg_net_bps": (float(paper["sum_net_bps"]) / n_paper) if n_paper else None,
        "open": len(paper.get("open") or []),
        "started_at": paper.get("started_at"),
    }

    n_live = int(live.get("n_trades") or 0)
    return {
        "now": now,
        "bot": "Ricochet (RSI-MR)",
        "mode": ("dry" if live.get("dry_run", True) else "live"),
        "wallet": hl["address"],
        "equity": hl["equity"],
        "hl_error": hl["error"],
        "positions": positions,
        "divergence": divergence,
        "live": {
            "n_trades": n_live,
            "realized_usd": live.get("realized_usd"),
            "skipped": live.get("skipped") or {},
            "exec_stats": live.get("exec_stats") or {},
            "equity_peak": live.get("equity_peak"),
            "paused_until": live.get("paused_until"),
            "last_sweep_hour": live.get("last_sweep_hour"),
        },
        "paper": paper_block,
        "liq": _liq_stats(),
        "regime_sizing": {str(k): v for k, v in REGIME_SIZE.items()},
    }


INDEX_HTML = """<!doctype html>
<meta charset="utf-8"><title>Ricochet — RSI-MR</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1115;--card:#171a21;--fg:#e6e8ee;--mut:#9aa3b2;--ok:#38b26b;
--bad:#e0554e;--warn:#d9a441;--line:#242833}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:20px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--mut);margin-bottom:18px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.k{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.v{font-size:22px;font-weight:600;margin-top:2px}
.badge{display:inline-block;padding:3px 10px;border-radius:99px;font-weight:600;font-size:12px}
.live{background:rgba(224,85,78,.15);color:var(--bad);border:1px solid var(--bad)}
.dry{background:rgba(56,178,107,.12);color:var(--ok);border:1px solid var(--ok)}
table{width:100%;border-collapse:collapse;margin-top:8px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:500;font-size:12px}
.pos{color:var(--ok)}.neg{color:var(--bad)}.mut{color:var(--mut)}
.sec{margin-top:22px}
.alert{background:rgba(224,85,78,.12);border:1px solid var(--bad);color:#ffb3ae;
padding:10px 12px;border-radius:8px;margin-top:10px}
.scroll{overflow-x:auto}
</style>
<div class="wrap">
  <h1>Ricochet <span class="mut">— rachat de survente RSI</span></h1>
  <div class="sub" id="sub">chargement…</div>
  <div class="grid" id="tiles"></div>
  <div id="alerts"></div>

  <div class="sec"><div class="k">Positions ouvertes</div>
    <div class="scroll"><table id="pos"></table></div></div>

  <div class="sec"><div class="k">Signaux non pris (et pourquoi)</div>
    <div class="scroll"><table id="skip"></table></div></div>

  <div class="sec"><div class="k">Paper — juge en aveugle jusqu'à mi-septembre</div>
    <div class="scroll"><table id="paper"></table></div></div>

  <div class="sec"><div class="k">Flux de liquidations</div>
    <div class="scroll"><table id="liq"></table></div></div>
</div>
<script>
const f=(v,d=2)=>v==null?'—':Number(v).toFixed(d);
const money=v=>v==null?'—':(v>=0?'+':'')+Number(v).toFixed(2)+' $';
const cls=v=>v==null?'mut':(v>0?'pos':(v<0?'neg':'mut'));
const SKIP={regime_calm:"régime calme — aucun edge brut mesuré",
 min_notional:"sous le minimum Hyperliquid (11 $)",
 slots:"8 positions déjà ouvertes", gross_cap:"plafond d'exposition atteint"};
async function tick(){
 let s; try{ s=await (await fetch('/api/state',{cache:'no-store'})).json(); }
 catch(e){ document.getElementById('sub').textContent='API injoignable'; return; }
 const live = s.mode==='live';
 document.getElementById('sub').innerHTML =
   `<span class="badge ${live?'live':'dry'}">${live?'ORDRES RÉELS':'DRY-RUN'}</span>`
   + ` &nbsp; wallet <code>${(s.wallet||'').slice(0,10)}…</code>`
   + ` &nbsp; maj ${new Date(s.now*1000).toLocaleTimeString()}`;
 document.getElementById('tiles').innerHTML = [
   ['Equity', s.equity==null?'—':f(s.equity)+' $'],
   ['PnL réalisé (live)', money(s.live.realized_usd)],
   ['Trades clos', s.live.n_trades],
   ['Positions', s.positions.length+' / 8'],
 ].map(([k,v])=>`<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('');

 let al='';
 if(s.hl_error) al+=`<div class="alert">Lecture Hyperliquid en erreur : ${s.hl_error}</div>`;
 if(s.divergence.bot_seul.length) al+=`<div class="alert">Le bot croit détenir ${s.divergence.bot_seul.join(', ')} — absent de l'exchange.</div>`;
 if(s.divergence.exchange_seul.length) al+=`<div class="alert">Positions sur l'exchange inconnues du bot : ${s.divergence.exchange_seul.join(', ')}.</div>`;
 if(s.live.paused_until && s.live.paused_until>s.now) al+=`<div class="alert">Kill-switch actif — reprise ${new Date(s.live.paused_until*1000).toLocaleString()}</div>`;
 document.getElementById('alerts').innerHTML=al;

 document.getElementById('pos').innerHTML =
  '<tr><th>Symbole</th><th>Taille</th><th>Entrée</th><th>Notionnel</th><th>Régime</th>'
  +'<th>Sortie dans</th><th>PnL latent</th><th>État</th></tr>'
  + (s.positions.length?s.positions.map(p=>{
      let etat = p.known_by_bot && p.on_exchange ? '✓ suivie'
        : (p.known_by_bot ? '<span class="neg">absente de l\\'exchange</span>'
                          : '<span class="neg">inconnue du bot</span>');
      return `<tr><td>${p.sym}</td><td>${p.size==null?'—':f(p.size,4)}</td>
      <td>${f(p.entry,6)}</td><td>${p.notional==null?'—':f(p.notional)+' $'}</td>
      <td>${p.regime_label}</td>
      <td>${p.remaining_h==null?'—':f(p.remaining_h,1)+' h'}</td>
      <td class="${cls(p.upnl)}">${money(p.upnl)}</td><td>${etat}</td></tr>`;}).join('')
    :'<tr><td colspan="8" class="mut">aucune position ouverte</td></tr>');

 const sk=s.live.skipped||{};
 document.getElementById('skip').innerHTML =
  '<tr><th>Raison</th><th>Compte</th></tr>'
  + Object.keys(SKIP).map(k=>`<tr><td>${SKIP[k]}</td><td>${sk[k]||0}</td></tr>`).join('');

 const p=s.paper;
 document.getElementById('paper').innerHTML =
  '<tr><th>Trades</th><th>Gagnants</th><th>PnL</th><th>Moy. nette/trade</th><th>Ouvertes</th></tr>'
  + `<tr><td>${p.n_trades}</td><td>${p.n_wins==null?'—':p.n_wins}</td>
     <td class="${cls(p.realized_usd)}">${money(p.realized_usd)}</td>
     <td class="${cls(p.avg_net_bps)}">${p.avg_net_bps==null?'—':f(p.avg_net_bps,1)+' bps'}</td>
     <td>${p.open}</td></tr>`;

 const l=s.liq;
 document.getElementById('liq').innerHTML = l.available
  ? '<tr><th>Liquidations captées</th><th>Lignes/seconde</th><th>Coins</th><th>Rafales sondées</th><th>Dont confirmées</th></tr>'
    + `<tr><td>${l.n_liq}</td><td>${l.n_sec}</td><td>${l.coins}</td><td>${l.probes}</td><td>${l.probe_hits}</td></tr>`
  : '<tr><td class="mut">collecteur indisponible</td></tr>';
}
tick(); setInterval(tick, 10000);
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
            self.send_header("WWW-Authenticate", 'Basic realm="Ricochet"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode("utf-8"),
                       "text/html; charset=utf-8")
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
        print(f"⚠️  Bind {host} refusé sans RSIMR_DASHBOARD_PASSWORD "
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
    print(f"Dashboard Ricochet (RSI-MR) — http://localhost:{PORT}/")
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

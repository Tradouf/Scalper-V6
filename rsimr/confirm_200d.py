"""
RSI-MR — test confirmatoire figé sur historique étendu (~200 j, 1h).

Suite de la session 07-08 après-midi (candidat RSI-MR long H=4h, +34 bps
placebo-propre sur 65 j). Étapes (a)+(b) convenues avant tout paper/live :
étendre l'historique via API et rejouer la règle FIGÉE + gate placebo.

RÈGLE FIGÉE (aucun degré de liberté restant) :
  - RSI(14) sur closes 1h, warmup 220 barres ;
  - signal LONG quand RSI passe de ≤30 à >30 (rachat de survente) ;
  - sortie au close H=4 barres plus tard (≈4 h) ;
  - frais round-trip 15 bps (taker), univers = les mêmes 48 symboles que la
    découverte (cache 15m ≥ 4000 barres).

CRITÈRES DE SUCCÈS FIGÉS AVANT EXÉCUTION :
  1. segment OOS (jours ANTÉRIEURS à la fenêtre de découverte ~65 j) :
     moyenne brute > 15 bps ET t_cluster/jour ≥ 2 ;
  2. placebo (permutation des barres, 40 tirages) sur fenêtre pleine : p < 0.05.
  Échec de l'un des deux ⇒ candidat mort, pas de « réglage pour que ça passe ».
"""
import json
import math
import random
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "/home/francois/Scalper-V6")
from hl_rate_limit import throttle_before_hl_request
from placebo_gate import shuffle_candles

CACHE = Path("/home/francois/Scalper-V6/state/ohlcv_cache")
OUT = Path(__file__).parent / "ohlcv_1h_200d"
OUT.mkdir(exist_ok=True)

DAYS = 205
H = 4
RSI_N = 14
WARMUP = 220
FEES_BPS = 15.0
N_PLACEBO = 40
MAJORS = {"BTC", "ETH", "SOL"}
HL_URL = "https://api.hyperliquid.xyz/info"
HOUR_MS = 3_600_000

# ── Univers + fenêtre de découverte (déduits du cache 15m existant) ─────────

symbols = []
discovery_start = None  # plus ancien ts du cache 15m = début fenêtre découverte
for p in sorted(CACHE.glob("*__15m.json")):
    c = json.loads(p.read_text())["candles"]
    if len(c) < 4000:
        continue
    symbols.append(p.name.split("__")[0])
    t0 = c[0]["ts"]
    discovery_start = t0 if discovery_start is None else min(discovery_start, t0)

print(f"univers = {len(symbols)} symboles ; découverte depuis "
      f"{time.strftime('%Y-%m-%d', time.gmtime(discovery_start/1000))} "
      f"({(time.time()*1000 - discovery_start)/86_400_000:.0f} j)")

# ── Fetch 1h ~200 j (une requête/symbole, cap API ~5000 bougies) ────────────

now_ms = int(time.time() * 1000)
start_ms = now_ms - DAYS * 86_400_000


def fetch_1h(sym):
    f = OUT / f"{sym}.json"
    if f.exists():
        return json.loads(f.read_text())
    throttle_before_hl_request()
    body = json.dumps({"type": "candleSnapshot", "req": {
        "coin": sym, "interval": "1h",
        "startTime": start_ms, "endTime": now_ms}}).encode()
    req = urllib.request.Request(HL_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = json.loads(r.read())
            break
        except Exception as e:
            if attempt == 3:
                print(f"  {sym}: échec fetch ({e})")
                return []
            time.sleep(2.0 * (attempt + 1))
    candles = [{"ts": int(x["t"]), "open": float(x["o"]), "high": float(x["h"]),
                "low": float(x["l"]), "close": float(x["c"]),
                "volume": float(x["v"])} for x in raw]
    # ne garder que les bougies clôturées
    candles = [c for c in candles if c["ts"] + HOUR_MS <= now_ms]
    f.write_text(json.dumps(candles))
    return candles


data = {}
for s in symbols:
    c = fetch_1h(s)
    if len(c) >= WARMUP + 50:
        data[s] = c
print(f"{len(data)} symboles avec ≥{WARMUP+50} barres 1h ; "
      f"couverture min={min(len(c) for c in data.values())} "
      f"max={max(len(c) for c in data.values())} barres")

# ── Règle figée ──────────────────────────────────────────────────────────────

def rsi(closes, n=RSI_N):
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


def long_signals(closes):
    r = rsi(closes)
    return [1 if i and r[i - 1] <= 30 < r[i] else 0 for i in range(len(r))]


def tstat(xs):
    n = len(xs)
    if n < 3:
        return float("nan"), float("nan")
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return m, m / math.sqrt(var / n) if var > 0 else float("nan")


def pooled(candle_map, ts_min=None, ts_max=None):
    """→ (moy_bps, t_cluster_jour, n_signaux, n_jours)"""
    by_day = defaultdict(list)
    n_sig = 0
    for s, candles in candle_map.items():
        closes = [c["close"] for c in candles]
        sig = long_signals(closes)
        for i, v in enumerate(sig):
            if not v or i < WARMUP or i + H >= len(closes):
                continue
            ts = candles[i]["ts"]
            if ts_min is not None and ts < ts_min:
                continue
            if ts_max is not None and ts >= ts_max:
                continue
            r = (closes[i + H] - closes[i]) / closes[i]
            by_day[ts // 86_400_000].append(r)
            n_sig += 1
    dm = [sum(v) / len(v) for v in by_day.values()]
    m, t = tstat(dm)
    return 1e4 * m, t, n_sig, len(dm)


def report(label, res):
    m, t, n, nd = res
    net = m - FEES_BPS
    print(f"  {label:<34} {m:+8.2f} bps brut  net {net:+8.2f}  "
          f"t_cl={t:+.2f}  n={n:<5} jours={nd}")


print("\n── RÉEL, règle figée (long only, H=4h, RSI 30↑) ──")
full = pooled(data)
report("fenêtre pleine ~200 j", full)
report("OOS pur (avant découverte)", pooled(data, ts_max=discovery_start))
report("fenêtre découverte (~65 j)", pooled(data, ts_min=discovery_start))

# moitiés temporelles de la fenêtre pleine
mid_ts = start_ms + (now_ms - start_ms) // 2
report("1re moitié", pooled(data, ts_max=mid_ts))
report("2e moitié", pooled(data, ts_min=mid_ts))

maj = {s: c for s, c in data.items() if s in MAJORS}
alt = {s: c for s, c in data.items() if s not in MAJORS}
report("majors BTC/ETH/SOL", pooled(maj))
report("alts", pooled(alt))

# largeur par symbole
per_sym = []
for s, candles in data.items():
    closes = [c["close"] for c in candles]
    sig = long_signals(closes)
    rs = [(closes[i + H] - closes[i]) / closes[i]
          for i, v in enumerate(sig)
          if v and i >= WARMUP and i + H < len(closes)]
    if len(rs) >= 10:
        per_sym.append((sum(rs) / len(rs), s, len(rs)))
pos = sum(1 for x, _, _ in per_sym if x > 1e-4 * FEES_BPS / 1e4)
posb = sum(1 for x, _, _ in per_sym if x > 0)
print(f"  largeur : {posb}/{len(per_sym)} symboles bruts>0, "
      f"{pos}/{len(per_sym)} nets>frais (≥10 signaux)")

# ── Gate placebo : permutation des barres, 40 tirages, fenêtre pleine ───────

print(f"\n── PLACEBO ({N_PLACEBO} permutations de barres, fenêtre pleine) ──")
t_real = full[1]
rng = random.Random(7)
placebo_ts = []
for draw in range(N_PLACEBO):
    fake = {}
    for s, candles in data.items():
        sh = shuffle_candles(candles, rng)
        if sh is not None:
            fake[s] = sh
    _, tt, _, _ = pooled(fake)
    placebo_ts.append(tt)

placebo_ts.sort()
ge = sum(1 for x in placebo_ts if x >= t_real)
p = (ge + 1) / (N_PLACEBO + 1)
print(f"  t réel {t_real:+.2f} | placebo min={placebo_ts[0]:+.2f} "
      f"méd={placebo_ts[N_PLACEBO//2]:+.2f} max={placebo_ts[-1]:+.2f}")
print(f"  p = ({ge}+1)/{N_PLACEBO+1} = {p:.3f}  →  "
      f"{'PASSE' if p < 0.05 else 'ÉCHOUE'} (α=0.05)")

# ── Verdict selon les critères figés ────────────────────────────────────────

oos = pooled(data, ts_max=discovery_start)
ok_oos = (oos[0] > FEES_BPS) and (oos[1] >= 2.0)
ok_placebo = p < 0.05
print("\n── VERDICT (critères figés avant exécution) ──")
print(f"  OOS brut>{FEES_BPS:.0f} bps ET t≥2 : {'OUI' if ok_oos else 'NON'} "
      f"({oos[0]:+.1f} bps, t={oos[1]:+.2f})")
print(f"  placebo p<0.05          : {'OUI' if ok_placebo else 'NON'} (p={p:.3f})")
print(f"  ⇒ {'CANDIDAT CONFIRMÉ' if (ok_oos and ok_placebo) else 'CANDIDAT MORT'}")

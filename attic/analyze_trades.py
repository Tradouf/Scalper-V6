#!/usr/bin/env python3
import json
import os
import re
import sys
import csv
from collections import Counter, defaultdict
from statistics import mean

DEFAULT_PATHS = [
    "memory/shared_memory.json",
    "shared_memory.json",
]

def load_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for x in obj:
            yield from walk(x)

def safe_float(x):
    try:
        return float(x)
    except Exception:
        return None

# ---- Détection objets JSON ----

def detect_positions_from_objects(data):
    found = []
    for node in walk(data):
        if not isinstance(node, dict):
            continue
        if {"symbol", "side", "entry"}.issubset(node.keys()):
            sym = str(node.get("symbol", "")).upper()
            side = str(node.get("side", "")).lower()
            entry = safe_float(node.get("entry"))
            sl = safe_float(node.get("sl"))
            tp = safe_float(node.get("tp"))
            qty = safe_float(node.get("qty"))
            ts = node.get("ts")
            if sym and side in ("buy", "sell") and entry is not None:
                found.append({
                    "symbol": sym,
                    "side": side,
                    "entry": entry,
                    "sl": sl,
                    "tp": tp,
                    "qty": qty,
                    "ts": ts,
                    "source": "json-object"
                })
    return found

def detect_scalper_from_objects(data):
    found = []
    for node in walk(data):
        if not isinstance(node, dict):
            continue
        agent = str(node.get("agent", "")).lower()
        action = str(node.get("action", "")).upper()
        symbol = str(node.get("symbol", "")).upper()
        conf = safe_float(node.get("confidence"))
        ts = node.get("ts")
        if agent == "agentscalper" and action == "ENTER" and symbol:
            found.append({
                "symbol": symbol,
                "confidence": conf,
                "ts": ts,
                "source": "json-object"
            })
    return found

def detect_tradehistory_from_objects(data):
    found = []
    for node in walk(data):
        if not isinstance(node, dict):
            continue
        if {"symbol", "side", "entry", "exit", "pnl", "result"}.issubset(node.keys()):
            sym = str(node.get("symbol", "")).upper()
            side = str(node.get("side", "")).lower()
            entry = safe_float(node.get("entry"))
            exit_ = safe_float(node.get("exit"))
            pnl = safe_float(node.get("pnl"))
            result = str(node.get("result", "")).upper()
            ts = node.get("ts")
            if sym and side in ("buy", "sell"):
                found.append({
                    "symbol": sym,
                    "side": side,
                    "entry": entry,
                    "exit": exit_,
                    "pnl": pnl,
                    "result": result,
                    "ts": ts,
                    "source": "json-object"
                })
    return found

# ---- Détection regex texte brut ----

def detect_positions_from_text(text):
    pattern = re.compile(
        r"positions\s+([A-Z0-9_-]+)\s+side\s+(buy|sell),\s*entry\s+([0-9.eE+-]+),\s*sl\s+([0-9.eE+-]+),\s*tp\s+([0-9.eE+-]+),\s*qty\s+([0-9.eE+-]+).*?ts\s+([0-9T:\.-]+)",
        re.IGNORECASE | re.DOTALL
    )
    out = []
    for m in pattern.finditer(text):
        out.append({
            "symbol": m.group(1).upper(),
            "side": m.group(2).lower(),
            "entry": safe_float(m.group(3)),
            "sl": safe_float(m.group(4)),
            "tp": safe_float(m.group(5)),
            "qty": safe_float(m.group(6)),
            "ts": m.group(7),
            "source": "text-regex"
        })
    return out

def detect_scalper_from_text(text):
    pattern = re.compile(
        r"agent\s+agentscalper,\s*symbol\s+([A-Z0-9_-]+),\s*action\s+ENTER,\s*confidence\s+([0-9.]+).*?ts\s+([0-9T:\.-]+)",
        re.IGNORECASE | re.DOTALL
    )
    out = []
    for m in pattern.finditer(text):
        out.append({
            "symbol": m.group(1).upper(),
            "confidence": safe_float(m.group(2)),
            "ts": m.group(3),
            "source": "text-regex"
        })
    return out

def detect_tradehistory_from_text(text):
    pattern = re.compile(
        r"tradehistory\s+symbol\s+([A-Z0-9_-]+),\s*side\s+(buy|sell),\s*entry\s+([0-9.eE+-]+),\s*exit\s+([0-9.eE+-]+),\s*pnl\s+([0-9.eE+-]+),\s*result\s+([A-Z]+),\s*ts\s+([0-9T:\.-]+)",
        re.IGNORECASE | re.DOTALL
    )
    out = []
    for m in pattern.finditer(text):
        out.append({
            "symbol": m.group(1).upper(),
            "side": m.group(2).lower(),
            "entry": safe_float(m.group(3)),
            "exit": safe_float(m.group(4)),
            "pnl": safe_float(m.group(5)),
            "result": m.group(6).upper(),
            "ts": m.group(7),
            "source": "text-regex"
        })
    return out

def dedupe_dicts(rows, keys):
    seen = set()
    out = []
    for r in rows:
        k = tuple(r.get(x) for x in keys)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out

def rr_of_position(p):
    entry, sl, tp, side = p.get("entry"), p.get("sl"), p.get("tp"), p.get("side")
    if None in (entry, sl, tp) or side not in ("buy", "sell"):
        return None
    if side == "buy":
        risk = entry - sl
        reward = tp - entry
    else:
        risk = sl - entry
        reward = entry - tp
    if risk is None or reward is None or risk <= 0:
        return None
    return reward / risk

def pnl_stats(trades):
    pnls = [t["pnl"] for t in trades if t.get("pnl") is not None]
    if not pnls:
        return None
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    return {
        "count": len(pnls),
        "winrate": (len(wins) / len(pnls)) * 100.0,
        "avg_pnl": mean(pnls),
        "sum_pnl": sum(pnls),
        "avg_win": mean(wins) if wins else 0.0,
        "avg_loss": mean(losses) if losses else 0.0,
        "wins": len(wins),
        "losses": len(losses),
    }

def per_symbol_stats(trades):
    by_sym = defaultdict(list)
    for t in trades:
        if t.get("symbol") and t.get("pnl") is not None:
            by_sym[t["symbol"]].append(t)

    rows = []
    for sym, ts in by_sym.items():
        s = pnl_stats(ts)
        if not s:
            continue
        rows.append({
            "symbol": sym,
            "trades": s["count"],
            "winrate": s["winrate"],
            "pnl_sum": s["sum_pnl"],
            "pnl_avg": s["avg_pnl"],
            "avg_win": s["avg_win"],
            "avg_loss": s["avg_loss"],
            "wins": s["wins"],
            "losses": s["losses"],
        })
    # trier par PnL cumulé croissant (pires en haut)
    rows.sort(key=lambda r: r["pnl_sum"])
    return rows

def export_csv(symbol_rows, path="trade_report.csv"):
    if not symbol_rows:
        return None
    fieldnames = [
        "symbol",
        "trades",
        "winrate",
        "pnl_sum",
        "pnl_avg",
        "avg_win",
        "avg_loss",
        "wins",
        "losses",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in symbol_rows:
            writer.writerow(r)
    return os.path.abspath(path)

def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        for p in DEFAULT_PATHS:
            if os.path.exists(p):
                path = p
                break

    if not path or not os.path.exists(path):
        print("Fichier shared_memory introuvable.")
        sys.exit(1)

    raw_text = load_text(path)

    try:
        data = load_json(path)
    except Exception:
        data = raw_text

    positions = []
    scalper = []
    tradehistory = []

    if isinstance(data, (dict, list)):
        positions.extend(detect_positions_from_objects(data))
        scalper.extend(detect_scalper_from_objects(data))
        tradehistory.extend(detect_tradehistory_from_objects(data))

    positions.extend(detect_positions_from_text(raw_text))
    scalper.extend(detect_scalper_from_text(raw_text))
    tradehistory.extend(detect_tradehistory_from_text(raw_text))

    positions = dedupe_dicts(positions, ["symbol", "side", "entry", "ts"])
    scalper = dedupe_dicts(scalper, ["symbol", "confidence", "ts"])
    tradehistory = dedupe_dicts(tradehistory, ["symbol", "side", "entry", "exit", "ts"])

    long_count = sum(1 for p in positions if p["side"] == "buy")
    short_count = sum(1 for p in positions if p["side"] == "sell")
    symbol_counts = Counter(p["symbol"] for p in positions)
    signal_counts = Counter(s["symbol"] for s in scalper)
    confidences = [s["confidence"] for s in scalper if s.get("confidence") is not None]
    rrs = [rr_of_position(p) for p in positions]
    rrs = [x for x in rrs if x is not None]

    notional_by_symbol = defaultdict(float)
    for p in positions:
        if p.get("entry") is not None and p.get("qty") is not None:
            notional_by_symbol[p["symbol"]] += p["entry"] * p["qty"]

    trade_stats = pnl_stats(tradehistory)
    symbol_rows = per_symbol_stats(tradehistory)
    csv_path = export_csv(symbol_rows)

    print("=" * 72)
    print("ANALYSE SIMPLE DU BOT — shared_memory.json (V2)")
    print("=" * 72)
    print(f"Fichier analysé : {path}")
    print(f"Positions retrouvées : {len(positions)}")
    print(f"Signaux scalper ENTER retrouvés : {len(scalper)}")
    print(f"Trades historiques retrouvés : {len(tradehistory)}")
    print()

    if not positions and not scalper and not tradehistory:
        print("Aucune donnée exploitable trouvée.")
        return

    if positions:
        print("POSITIONS")
        print(f"- Longs : {long_count}")
        print(f"- Shorts : {short_count}")
        print("- Top symboles en position :")
        for sym, n in symbol_counts.most_common(10):
            notion = notional_by_symbol.get(sym, 0.0)
            print(f"  • {sym:<8} {n:>3} positions | notional estimé: {notion:,.2f}")
        print()

    if rrs:
        print("RISK / REWARD THÉORIQUE (sur positions actuelles)")
        print(f"- RR moyen : {mean(rrs):.2f}")
        print(f"- RR min   : {min(rrs):.2f}")
        print(f"- RR max   : {max(rrs):.2f}")
        print()

    if scalper:
        print("SIGNAUX AGENTSCALPER")
        if confidences:
            print(f"- Confiance moyenne : {mean(confidences):.2f}")
            print(f"- Confiance min/max : {min(confidences):.2f} / {max(confidences):.2f}")
        print("- Symboles les plus signalés :")
        for sym, n in signal_counts.most_common(10):
            print(f"  • {sym:<8} {n:>3} ENTER")
        print()

    if trade_stats:
        print("TRADE HISTORY (global)")
        print(f"- Nombre de trades : {trade_stats['count']}")
        print(f"- Winrate          : {trade_stats['winrate']:.1f}%")
        print(f"- PnL moyen/trade  : {trade_stats['avg_pnl']:.5f}")
        print(f"- PnL cumulé       : {trade_stats['sum_pnl']:.5f}")
        print(f"- Gain moyen       : {trade_stats['avg_win']:.5f}")
        print(f"- Perte moyenne    : {trade_stats['avg_loss']:.5f}")
        print(f"- Wins / Losses    : {trade_stats['wins']} / {trade_stats['losses']}")
        print()

    if symbol_rows:
        print("TRADE HISTORY PAR SYMBOLE (trié du pire au meilleur PnL cumulé)")
        print(f"{'SYM':<6} {'TRD':>3} {'WIN%':>6} {'PnL_sum':>10} {'PnL_avg':>9} {'AvgWin':>9} {'AvgLoss':>9}")
        for r in symbol_rows:
            print(
                f"{r['symbol']:<6} "
                f"{r['trades']:>3} "
                f"{r['winrate']:>6.1f} "
                f"{r['pnl_sum']:>10.5f} "
                f"{r['pnl_avg']:>9.5f} "
                f"{r['avg_win']:>9.5f} "
                f"{r['avg_loss']:>9.5f}"
            )
        print()

    print("LECTURE RAPIDE")
    if positions:
        bias = "long" if long_count > short_count else "short" if short_count > long_count else "équilibré"
        print(f"- Biais actuel : {bias}")
    if rrs and mean(rrs) < 1.2:
        print("- Alerte : RR moyen théorique faible, scalping potentiellement mangé par frais/slippage.")
    if confidences and mean(confidences) < 0.70:
        print("- Alerte : confiance moyenne modeste, beaucoup de signaux borderline.")
    if trade_stats and trade_stats["sum_pnl"] < 0:
        print("- Alerte : historique négatif, revoir filtres d'entrée et logique de sortie.")
    elif trade_stats and trade_stats["sum_pnl"] > 0:
        print("- Historique positif sur l'échantillon détecté.")
    if csv_path:
        print(f"- Rapport détaillé exporté dans : {csv_path}")
    print("=" * 72)

if __name__ == "__main__":
    main()

"""
CLI du GridAgent.

    python -m grid.run fetch       # charge et contrôle les deux fenêtres §9.3
    python -m grid.run backtest    # un backtest sur une fenêtre
    python -m grid.run explain     # pourquoi la grille ne se déploie pas
    python -m grid.run validate    # protocole §9 complet (bloquant)

`validate` est la seule commande qui compte pour une décision de déploiement.
Aucune commande ne passe d'ordre.

**Contrôle anti-retouche.** Comme pour le candidat n°1, `validate` refuse de
rendre un verdict si la configuration a bougé depuis son gel. Le hash couvre ici
le COUPLE `grid.yaml` + `confluence.yaml` : l'activation §2 lit les seuils de
régime du ConfluenceAgent, ils font donc partie de l'hypothèse gelée.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional

from confluence import config as conf_config
from confluence.data import load_funding, load_history
from grid import config as grid_config
from grid.backtest import GridBacktester
from grid.validate import WindowData, ab_handoff, acceptance, run_placebo, run_variant, sensitivity

logger = logging.getLogger("sdm.grid.run")

FROZEN = Path(__file__).resolve().parent / "state" / "frozen" / "FROZEN.json"

# Les deux fenêtres du §9.3. La seconde est OBLIGATOIRE : « une grille validée
# uniquement en marché calme est invalide par définition ».
WINDOWS = [
    {"label": "recente", "days": 1100, "end_ms": None, "funding_source": "hyperliquid"},
    {"label": "bear_2020_2023", "days": 1460, "end_ms": 1704067200000,
     "funding_source": "binance"},
]


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stderr)


# ── Gel de configuration ────────────────────────────────────────────────────

def combined_hash() -> Optional[str]:
    """Hash du couple de fichiers, dans l'ordre déterministe du gel."""
    files = [Path("config/confluence.yaml"), Path("config/grid.yaml")]
    h = hashlib.sha256()
    for f in sorted(files):
        try:
            h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
        except OSError:
            return None
    return h.hexdigest()


def check_frozen() -> Optional[str]:
    """Rend un problème si la config a bougé depuis le gel, sinon None."""
    if not FROZEN.exists():
        return ("aucune configuration figée (grid/state/frozen/FROZEN.json) — "
                "geler AVANT le tirage, sinon le gate placebo ne prouve rien")
    try:
        frozen = json.loads(FROZEN.read_text(encoding="utf-8")).get("sha256")
    except (OSError, json.JSONDecodeError):
        return "FROZEN.json illisible"
    live = combined_hash()
    if live is None:
        return "configuration illisible pour le calcul de hash"
    if live != frozen:
        return (f"la configuration a CHANGÉ depuis son gel (figée {frozen[:12]}, "
                f"actuelle {live[:12]}) — relancer sur une config modifiée après "
                f"avoir vu un résultat est du multiple-testing ; re-geler et "
                f"enregistrer une NOUVELLE entrée au registre")
    return None


# ── Chargement des fenêtres ─────────────────────────────────────────────────

def load_window(spec: dict, no_cache: bool = False) -> WindowData:
    days = spec["days"]
    end_ms = spec["end_ms"]
    warmup_days = 120        # percentile ATR 90 j + lookback range 4 j + marge

    hist = load_history("BTC", days + warmup_days, timeframes=("1m", "15m", "1h"),
                        end_ms=end_ms, source="binance", cache=not no_cache)
    funding, provenance = load_funding("BTC", days + warmup_days, end_ms=end_ms,
                                       source=spec["funding_source"])
    logger.info("[%s] %s", spec["label"], provenance.summary())

    end = int(end_ms) if end_ms else int(time.time() * 1000)
    return WindowData(
        label=spec["label"],
        candles_1m=hist.candles.get("1m", []),
        candles_15m=hist.candles.get("15m", []),
        candles_1h=hist.candles.get("1h", []),
        funding=funding,
        start_ms=end - int(days * 86_400_000),
    )


def _preflight(windows: List[WindowData]) -> List[str]:
    problems = []
    tampered = check_frozen()
    if tampered:
        problems.append(tampered)
    for w in windows:
        if not w.candles_1m:
            problems.append(f"{w.label}: aucune bougie 1m")
            continue
        if w.days < 300:
            problems.append(f"{w.label}: seulement {w.days:.0f} j de 1m")
        if not w.funding:
            problems.append(f"{w.label}: aucun funding — le filtre §2 vetera tout")
    return problems


# ── Commandes ───────────────────────────────────────────────────────────────

def cmd_fetch(args) -> int:
    for spec in WINDOWS:
        w = load_window(spec, args.no_cache)
        print(f"[{w.label}] 1m={len(w.candles_1m)} 15m={len(w.candles_15m)} "
              f"1h={len(w.candles_1h)} funding={len(w.funding)} — {w.days:.0f} j")
    print(f"\ngel: {check_frozen() or 'config intacte ✓'}")
    return 0


def cmd_backtest(args) -> int:
    gcfg, ccfg = grid_config.load(), conf_config.load()
    spec = next(s for s in WINDOWS if s["label"] == args.window)
    window = load_window(spec, args.no_cache)
    result = run_variant(gcfg, ccfg, window, handoff=gcfg.exits.breakout_handoff,
                         equity=args.equity)
    print(json.dumps(result.metrics, indent=2, ensure_ascii=False))
    print("\nMotifs de non-déploiement :")
    for reason, count in result.vetoes:
        print(f"  {count:>8}  {reason}")
    return 0


def cmd_explain(args) -> int:
    gcfg, ccfg = grid_config.load(), conf_config.load()
    for spec in WINDOWS:
        window = load_window(spec, args.no_cache)
        bt = GridBacktester(gcfg, ccfg, args.equity)
        res = bt.run(window.candles_1m, window.candles_15m, window.candles_1h,
                     funding=window.funding, start_ms=window.start_ms)
        total = sum(res.activation_vetoes.values()) or 1
        print(f"\n=== {window.label} — {len(res.sessions)} sessions déployées ===")
        for reason, count in res.veto_distribution(15):
            print(f"  {count:>8} ({count/total:5.1%})  {reason}")
    return 0


def cmd_validate(args) -> int:
    gcfg, ccfg = grid_config.load(), conf_config.load()
    windows = [load_window(spec, args.no_cache) for spec in WINDOWS]

    problems = _preflight(windows)
    if problems and not args.force:
        print("REFUS DE VALIDER — les données ou le gel ne permettent pas un "
              "verdict interprétable :\n")
        for p in problems:
            print(f"  ✗ {p}")
        print("\n--force passe outre, en sachant qu'un échec de données est alors")
        print("indiscernable d'un échec de stratégie.")
        return 2

    print(f"Protocole §9 — GridAgent | config gelée {(combined_hash() or '')[:12]}")
    print(f"Seuil placebo α={gcfg.backtest.placebo.alpha} "
          f"({gcfg.backtest.placebo.n_draws} tirages) — registre entrée n°2, n=2\n")

    # §9.5 A/B AVANT tout : c'est lui qui fixe la variante à valider.
    print("── A/B breakout handoff (§9.5) " + "─" * 36)
    ab = ab_handoff(gcfg, ccfg, windows, args.equity)
    for row in ab["per_window"]:
        print(f"  {row['window']:>16} : A(flatten)={row['A_flatten_net_mtm']:>10.2f}  "
              f"B(handoff)={row['B_handoff_net_mtm']:>10.2f}  "
              f"Δ={row['delta']:>+9.2f}  {'B≥A' if row['B_better_or_equal'] else 'A>B'}")
    print(f"  → {ab['decision']}")
    handoff = ab["adopt_handoff"]

    print("\n── Résultats par fenêtre (net_mtm_pnl seul, §7) " + "─" * 20)
    results = [run_variant(gcfg, ccfg, w, handoff, args.equity) for w in windows]
    for r in results:
        m = r.metrics
        print(f"  {r.label:>16} : net={m['net_mtm_pnl']:>10.2f}  "
              f"sessions={m['sessions']:>3}  PF={m.get('profit_factor')}  "
              f"frais={m.get('fee_ratio')}  pire perte={m.get('worst_session_loss_pct')}")
        print(f"                     réalisé={m.get('realized_grid_pnl')} "
              f"inventaire={m.get('inventory_pnl')} — écart d'illusion visible ici")

    print("\n── Critères d'acceptation §9.4 " + "─" * 36)
    verdict = acceptance(gcfg, results)
    for name, c in verdict["checks"].items():
        print(f"  [{'PASS' if c['passed'] else 'FAIL'}] {name}: {c['value']} "
              f"(seuil {c['threshold']})")
    print(f"  motifs d'arrêt: {verdict['stop_reasons']}")

    sens = None
    if not args.skip_sensitivity:
        print("\n── Sensibilité ±20 % " + "─" * 46)
        sens = sensitivity(gcfg, ccfg, windows, handoff, args.equity)
        print(f"  référence: net={sens['base_net_mtm']}")
        for v in sens["variants"]:
            if "error" in v:
                print(f"  {v['param']} {v['delta']:+.0%}: {v['error']}")
                continue
            flag = "  ← EFFONDREMENT" if v["collapsed"] else ""
            print(f"  {v['param']:>28} {v['delta']:+.0%} → {v['value']}: "
                  f"net={v['net_mtm_pnl']}{flag}")
        print(f"  fragile: {sens['fragile']}")

    gate = None
    if not args.skip_placebo:
        print(f"\n── Gate placebo (α={gcfg.backtest.placebo.alpha}, "
              f"{gcfg.backtest.placebo.n_draws} tirages) " + "─" * 20)
        gate = run_placebo(gcfg, ccfg, windows[0], handoff, jobs=args.jobs,
                           equity=args.equity)
        print(f"  {gate.summary()}")

    overall = (verdict["passed"] and (sens is None or not sens["fragile"])
               and (gate is None or gate.passed))
    print("\n" + "=" * 70)
    print("VERDICT : " + ("candidat recevable pour le paper testnet (§9.7)"
                          if overall else "REJETÉ — pas de paper, pas de mainnet"))
    print("=" * 70)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps({
            "candidate": "GridAgent", "registry_entry": 2,
            "config_frozen": combined_hash(),
            "config_untouched": check_frozen() is None,
            "decision_metric": "net_mtm_pnl (§7)",
            "ab_handoff": ab, "handoff_adopted": handoff,
            "windows": [{"label": r.label, "metrics": r.metrics, "vetoes": r.vetoes}
                        for r in results],
            "acceptance": verdict, "sensitivity": sens,
            "placebo": None if gate is None else {
                "real_count": gate.real_count, "p_value": gate.p_value,
                "alpha": gate.alpha, "passed": gate.passed},
            "overall_passed": overall,
            "funding_note": ("NON VALIDÉ — estimation historique sur positions simulées ; "
                             "à mesurer en paper trading avant tout mainnet"),
        }, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        print(f"\nrapport écrit dans {args.out}")
    return 0 if overall else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="grid.run", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--equity", type=float, default=10_000.0)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--jobs", type=int, default=1)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("-v", "--verbose", action="store_true")

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch")
    b = sub.add_parser("backtest")
    b.add_argument("--window", default="recente", choices=[w["label"] for w in WINDOWS])
    sub.add_parser("explain")
    v = sub.add_parser("validate")
    v.add_argument("--skip-placebo", action="store_true")
    v.add_argument("--skip-sensitivity", action="store_true")
    v.add_argument("--force", action="store_true")
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    return {"fetch": cmd_fetch, "backtest": cmd_backtest,
            "explain": cmd_explain, "validate": cmd_validate}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())

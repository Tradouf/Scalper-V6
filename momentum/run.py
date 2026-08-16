"""
CLI du MomentumAgent.

    python -m momentum.run fetch      # charge les deux fenêtres §9.3
    python -m momentum.run backtest   # un backtest sur une fenêtre
    python -m momentum.run validate   # protocole §9 complet (bloquant)

Aucune commande ne passe d'ordre. `validate` refuse de rendre un verdict si la
config a bougé depuis son gel — le hash porte sur `momentum.yaml` SEUL (§9.3).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Dict, Optional

from momentum import config as config_mod
from momentum.data import MultiAssetHistory, load_history
from momentum.validate import (
    acceptance,
    branch_alerts,
    run_placebo,
    run_window,
    sensitivity,
)

logger = logging.getLogger("sdm.momentum.run")
FROZEN = Path(__file__).resolve().parent / "state" / "frozen" / "FROZEN.json"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        stream=sys.stderr)


def config_hash() -> Optional[str]:
    try:
        return hashlib.sha256(config_mod.DEFAULT_PATH.read_bytes()).hexdigest()
    except OSError:
        return None


def check_frozen() -> Optional[str]:
    """Garde anti-retouche. Rend un problème, ou None."""
    if not FROZEN.exists():
        return ("aucune configuration figée — geler AVANT le tirage, sinon le "
                "gate placebo ne prouve rien")
    try:
        frozen = json.loads(FROZEN.read_text(encoding="utf-8")).get("sha256")
    except (OSError, json.JSONDecodeError):
        return "FROZEN.json illisible"
    live = config_hash()
    if live != frozen:
        return (f"la configuration a CHANGÉ depuis son gel (figée {frozen[:12]}, "
                f"actuelle {(live or '?')[:12]}) — relancer sur une config modifiée "
                f"après avoir vu un résultat est du multiple-testing")
    return None


def load_windows(cfg, no_cache: bool = False,
                 throttle: float = 0.12) -> Dict[str, MultiAssetHistory]:
    out: Dict[str, MultiAssetHistory] = {}
    for w in cfg.backtest.windows:
        warmup = cfg.signal.total_days + cfg.universe.liquidity_lookback_d + 10
        out[w.label] = load_history(w.days + warmup, w.end_ms,
                                    basket_pool=max(25, cfg.universe.basket_size * 2),
                                    cache=not no_cache, throttle_s=throttle)
        logger.info("[%s] %s", w.label, out[w.label].summary())
    return out


def cmd_fetch(cfg, args) -> int:
    for label, hist in load_windows(cfg, args.no_cache, args.throttle).items():
        s = hist.summary()
        print(f"[{label}] {s['symbols']} actifs, {s['daily_bars']} bougies 1d, "
              f"{s['hourly_bars']} bougies 1h, {s['funding_points']} règlements, "
              f"médiane {s['median_span_days']:.0f} j")
    print(f"\ngel: {check_frozen() or 'config intacte ✓'}")
    return 0


def cmd_backtest(cfg, args) -> int:
    hists = load_windows(cfg, args.no_cache, args.throttle)
    for label, hist in hists.items():
        run = run_window(cfg, hist, label)
        print(f"\n=== {label} ===")
        print(json.dumps(run.metrics, indent=2, ensure_ascii=False))
        if run.never_taken:
            print(f"⚠ branches jamais empruntées : {run.never_taken}")
    return 0


def cmd_validate(cfg, args) -> int:
    tampered = check_frozen()
    if tampered and not args.force:
        print(f"REFUS DE VALIDER — {tampered}")
        return 2

    hists = load_windows(cfg, args.no_cache, args.throttle)
    for label, hist in hists.items():
        if len(hist.daily) < cfg.universe.basket_size:
            print(f"REFUS DE VALIDER — [{label}] seulement {len(hist.daily)} actifs "
                  f"chargés pour un panier de {cfg.universe.basket_size}")
            if not args.force:
                return 2

    print(f"Protocole §9 — MomentumAgent | config gelée {(config_hash() or '')[:12]}")
    print(f"Placebo α={cfg.backtest.placebo.alpha} "
          f"({cfg.backtest.placebo.n_draws} tirages) — registre entrée n°3, n=3\n")

    print("── Résultats par fenêtre (net_mtm_pnl seul, §7) " + "─" * 20)
    runs = [run_window(cfg, hist, label) for label, hist in hists.items()]
    for r in runs:
        m = r.metrics
        print(f"  {r.label:>16} : net={m['net_mtm_pnl']:>10.2f}  "
              f"rebal={m['rebalances']:>4}  PF={m.get('profit_factor')}  "
              f"DD={m.get('max_drawdown_pct')}  frais={m.get('fee_ratio')}")
        print(f"       long={m.get('pnl_long')} short={m.get('pnl_short')} "
              f"funding L/S={m.get('funding_long')}/{m.get('funding_short')}")
        print(f"       {m.get('edge_location')}")

    alerts = branch_alerts(runs)
    if alerts:
        print("\n⚠ Compteurs de branche (§9.3) :")
        for a in alerts:
            print(f"    {a}")

    sens = None
    if not args.skip_sensitivity:
        print("\n── Sensibilité ±20 % " + "─" * 46)
        sens = sensitivity(cfg, hists)
        print(f"  référence: net={sens['base_net_mtm']}")
        for v in sens["variants"]:
            if "error" in v:
                print(f"  {v['param']} {v['delta']:+.0%}: {v['error']}")
                continue
            print(f"  {v['param']:>24} {v['delta']:+.0%} → {v['value']}: "
                  f"net={v['net_mtm_pnl']}")
        for peak in sens["isolated_peaks"]:
            print(f"  ⚠ {peak['param']}: {peak['verdict']} "
                  f"(base {peak['base']} vs meilleur voisin {peak['best_neighbour']})")

    gate = None
    if not args.skip_placebo:
        print(f"\n── Gate placebo (α={cfg.backtest.placebo.alpha}, "
              f"{cfg.backtest.placebo.n_draws} tirages) — CRITÈRE PRINCIPAL " + "─" * 5)
        gate = run_placebo(cfg, hists, seed=args.seed)
        print(f"  réel={gate['real_net']}  |  nulle: médiane={gate['null_median']} "
              f"max={gate['null_max']}")
        print(f"  p = {gate['p_value']:.4f} (α={gate['alpha']}) → "
              f"{'PASSE' if gate['passed'] else 'ÉCHOUE'}")

    print("\n── Critères d'acceptation §9.4 " + "─" * 36)
    verdict = acceptance(cfg, runs, gate)
    for name, c in verdict["checks"].items():
        tag = " (PRINCIPAL)" if c.get("principal") else ""
        print(f"  [{'PASS' if c['passed'] else 'FAIL'}] {name}{tag}: {c['value']} "
              f"(seuil {c['threshold']})")

    overall = verdict["passed"] and (sens is None or not sens["fragile"])
    print("\n" + "=" * 70)
    print("VERDICT : " + ("candidat recevable pour le paper testnet (§9.5)"
                          if overall else "REJETÉ — pas de paper, pas de mainnet"))
    print("=" * 70)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps({
            "candidate": "MomentumAgent", "registry_entry": 3,
            "config_frozen": config_hash(), "config_untouched": check_frozen() is None,
            "decision_metric": "net_mtm_pnl (§7)",
            "windows": [{"label": r.label, "metrics": r.metrics,
                         "branches": r.branches, "never_taken": r.never_taken}
                        for r in runs],
            "placebo": gate, "sensitivity": sens, "acceptance": verdict,
            "branch_alerts": alerts, "overall_passed": overall,
            "funding_note": ("proxy de LIEU (Binance, pas Hyperliquid) — NON VALIDÉ "
                             "jusqu'à mesure en paper trading"),
        }, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        print(f"\nrapport écrit dans {args.out}")
    return 0 if overall else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="momentum.run", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--throttle", type=float, default=0.12)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch")
    sub.add_parser("backtest")
    v = sub.add_parser("validate")
    v.add_argument("--skip-placebo", action="store_true")
    v.add_argument("--skip-sensitivity", action="store_true")
    v.add_argument("--force", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    cfg = config_mod.load()
    return {"fetch": cmd_fetch, "backtest": cmd_backtest,
            "validate": cmd_validate}[args.command](cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())

"""
CLI du module ConfluenceAgent.

    python -m confluence.run fetch       # charge et contrôle l'historique
    python -m confluence.run backtest    # un backtest sur toute la période
    python -m confluence.run validate    # le protocole §9 complet (bloquant)
    python -m confluence.run explain     # pourquoi le bot ne trade pas, en clair

`validate` est la seule commande qui compte pour une décision de déploiement.
Les autres servent à mettre au point ; elles ne valident rien.

Aucune de ces commandes ne passe d'ordre. Le module n'a pas de chemin vers le
mainnet tant que le verdict §9.4 n'est pas rendu — c'est délibéré (§9.6).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

from confluence import config as config_mod
from confluence.backtest import Backtester
from confluence.data import History, load_funding, load_history
from confluence.walkforward import (
    DEFAULT_GRID,
    acceptance,
    run_placebo,
    sensitivity,
    walk_forward,
    windows_for,
)

logger = logging.getLogger("sdm.confluence.run")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    if not verbose:
        # Le log JSON par évaluation (§8) est fait pour le LIVE, où il y a un
        # réveil toutes les 15 minutes. En backtest il y en a 105 000 par
        # passage et une centaine de passages : le protocole §9 écrivait
        # 2,4 millions de lignes et passait plus de temps à sérialiser des
        # dictionnaires qu'à simuler. `-v` le rétablit quand on débogue une
        # décision précise.
        logging.getLogger("sdm.confluence.agent").setLevel(logging.WARNING)


def _load(cfg, args) -> History:
    days = args.days or cfg.backtest.history_days
    # On charge PLUS que la période testée : chaque couche a besoin de sa
    # fenêtre pleine avant la première décision (cf. config.WINDOW_SLACK).
    warmup_days = max(
        cfg.bias_1d.window_bars,                       # bougies daily
        cfg.regime_1h.window_bars / 24.0,
        cfg.timing_15m.window_bars / 96.0,
    )
    hist = load_history(cfg.symbol, days + warmup_days, end_ms=args.end_ms,
                        cache=not args.no_cache, throttle_s=args.throttle,
                        source=args.source)
    # Borne explicite de la fenêtre de DÉCISION : `--days` doit désigner la
    # période testée, pas la période chargée (warmup compris).
    end = int(args.end_ms) if args.end_ms else int(time.time() * 1000)
    hist.decision_start_ms = end - int(days * 86_400_000)
    covered = hist.reports.get("15m")
    if covered is not None and covered.days < days:
        logger.warning(
            "15m ne couvre que %.0f j sur les %.0f demandés (source=%s) — "
            "le §9.2 exige 3 ans ; voir confluence/sources.py",
            covered.days, days, args.source)
    try:
        hist.funding, hist.funding_provenance = load_funding(
            cfg.symbol, days + warmup_days, end_ms=args.end_ms,
            source=args.funding_source)
    except Exception as exc:                            # noqa: BLE001 — réseau
        # Sans funding, la couche 1h oppose son veto en permanence (§4.2). On
        # le dit fort : un backtest à zéro trade pour cette raison ressemble à
        # s'y méprendre à une stratégie très sélective.
        logger.error("funding indisponible (%s) — la couche 1h vetera TOUT", exc)
    return hist


def cmd_fetch(cfg, args) -> int:
    hist = _load(cfg, args)
    for tf, report in hist.reports.items():
        print(report.summary())
        for gap in report.gaps[:5]:
            print(f"    trou {tf}: {gap[2]} barres manquantes")
    print(hist.funding_provenance.summary())
    return 0


def cmd_collect(cfg, args) -> int:
    """Accumule les bougies Hyperliquid natives dans une archive locale.

    L'API ne rend que ses 5000 dernières bougies : le passé est perdu, mais le
    présent peut être conservé. Lancé régulièrement (hebdomadaire suffit pour
    le 15m, dont la fenêtre couvre 52 jours), ce collecteur construit la série
    native qui permettra un jour de rejouer le §9 sans proxy.
    """
    from confluence.sources import collect_native, load_archive

    added = collect_native(cfg.symbol)
    for tf, count in added.items():
        series = load_archive(cfg.symbol, tf)
        span = ((series[-1]["ts"] - series[0]["ts"]) / 86_400_000.0) if series else 0.0
        print(f"{tf}: {len(series)} bougies archivées (+{count}), {span:.0f} j couverts")
    return 0


def cmd_overlap(cfg, args) -> int:
    """Chiffre l'écart entre le proxy profond et la série Hyperliquid native.

    C'est le contrôle qui décide si un résultat obtenu sur le proxy peut être
    transposé à Hyperliquid. Sans lui, le §9 validerait une stratégie sur un
    marché qu'on ne trade pas.
    """
    from confluence.sources import compare_overlap

    days = args.days or 45
    # `source=` porte À LA FOIS le fetcher et la clé de cache. Passer un
    # fetcher sans changer la clé ferait relire la série de l'autre source et
    # rendrait une corrélation de 1,0000 parfaitement rassurante et fausse.
    native = load_history(cfg.symbol, days, timeframes=("15m", "1h"),
                          cache=not args.no_cache, source="native")
    proxy = load_history(cfg.symbol, days, timeframes=("15m", "1h"),
                         cache=not args.no_cache, source="binance")

    usable = True
    for tf in ("15m", "1h"):
        report = compare_overlap(native.candles.get(tf, []), proxy.candles.get(tf, []), tf)
        print(report.summary())
        usable = usable and report.usable
    if not usable:
        print("\n⚠ le proxy ne reproduit pas fidèlement le marché Hyperliquid : "
              "un verdict §9 obtenu dessus ne vaut PAS pour Hyperliquid.")
    return 0 if usable else 1


def cmd_backtest(cfg, args) -> int:
    hist = _load(cfg, args)
    result = Backtester(cfg, args.equity).run(hist)
    print(json.dumps(result.metrics(), indent=2, ensure_ascii=False))
    print(f"\n{hist.funding_provenance.summary()}")
    print("\nMotifs de veto (§9.3) :")
    for reason, count in result.veto_distribution(15):
        print(f"  {count:>7}  {reason}")
    if args.out:
        Path(args.out).write_text(json.dumps({
            "metrics": result.metrics(),
            "vetoes": result.veto_distribution(50),
            "trades": [t.__dict__ for t in result.trades],
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nécrit dans {args.out}")
    return 0


def cmd_explain(cfg, args) -> int:
    """Diagnostic : où la cascade s'arrête, et à quelle fréquence.

    C'est la commande à lancer quand le bot ne trade pas. Le §5 le dit
    explicitement : la distribution des vetos est la donnée qui permet de
    distinguer un filtre qui travaille d'un module cassé.
    """
    hist = _load(cfg, args)
    result = Backtester(cfg, args.equity).run(hist)
    total = result.evaluations or 1
    print(f"{result.evaluations} évaluations, {result.signals} signaux, "
          f"{len(result.trades)} trades exécutés, "
          f"{result.abandoned} ordres maker abandonnés faute de fill\n")
    print("Couche bloquante :")
    for layer, count in result.veto_counts.most_common():
        print(f"  {layer:>5} : {count:>7} ({count / total:6.1%})")
    print("\nMotifs détaillés :")
    for reason, count in result.veto_distribution(25):
        print(f"  {count:>7} ({count / total:5.1%})  {reason}")
    return 0


def _preflight(cfg, hist, args) -> list:
    """Raisons pour lesquelles un verdict §9 ne serait PAS interprétable.

    Ce contrôle existe à cause d'un incident concret : un HTTP 429 pendant le
    chargement du funding a laissé la liste vide ; la couche 1h a opposé son
    veto à toutes les évaluations (§4.2) ; et le protocole a produit 90
    backtests à zéro trade avec l'aplomb d'un vrai résultat. Un « échec » causé
    par des données manquantes est indiscernable d'un échec de stratégie — sauf
    si on refuse de le rendre.
    """
    problems = []
    days = args.days or cfg.backtest.history_days

    tampered = check_config_frozen(getattr(args, "config", None))
    if tampered:
        problems.append(tampered)

    prov = hist.funding_provenance
    if not prov.points:
        problems.append("aucun taux de funding chargé — la couche 1h veterait TOUT (§4.2)")
    else:
        c15 = hist.candles.get("15m", [])
        if c15:
            # Le funding doit couvrir la FENÊTRE DE DÉCISION, pas seulement une
            # partie de l'historique chargé.
            warm_end = max(Backtester(cfg)._warmup_end_ms(hist),   # noqa: SLF001
                           getattr(hist, "decision_start_ms", 0))
            if prov.first_ms > warm_end:
                manque = (prov.first_ms - warm_end) / 86_400_000.0
                problems.append(
                    f"le funding ne commence qu'au {_iso_day(prov.first_ms)}, soit "
                    f"{manque:.0f} j après le début de la fenêtre de décision — "
                    f"ces jours seraient vetés pour données manquantes, pas par la stratégie")

    for tf in ("1d", "1h", "15m"):
        report = hist.reports.get(tf)
        if report is None or not report.bars:
            problems.append(f"série {tf} absente")
            continue
        if report.missing_bars:
            problems.append(f"{tf}: {report.missing_bars} barres manquantes (trous non comblés)")

    covered = hist.reports.get("15m")
    if covered is not None and covered.days < days * 0.9:
        problems.append(f"15m ne couvre que {covered.days:.0f} j sur {days:.0f} demandés")
    return problems


def _iso_day(ts_ms: int) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ts_ms / 1000, tz=_dt.timezone.utc).strftime("%Y-%m-%d")


def cmd_validate(cfg, args) -> int:
    hist = _load(cfg, args)

    problems = _preflight(cfg, hist, args)
    if problems and not args.force:
        print("REFUS DE VALIDER — les données ne permettent pas un verdict interprétable :\n")
        for problem in problems:
            print(f"  ✗ {problem}")
        print("\nCorriger les données, ou relancer avec --force en sachant que le")
        print("verdict obtenu ne distinguera pas un échec de stratégie d'un trou de données.")
        return 2
    if problems:
        print("⚠ --force : verdict produit MALGRÉ des données incomplètes :")
        for problem in problems:
            print(f"  ✗ {problem}")
        print()

    start_ms = getattr(hist, "decision_start_ms", None)
    wins = windows_for(hist, cfg, start_ms=start_ms)
    print(f"Protocole §9 — {len(wins)} fenêtres walk-forward "
          f"({cfg.backtest.walkforward.is_months} mois IS / "
          f"{cfg.backtest.walkforward.oos_months} mois OOS, "
          f"pas {cfg.backtest.walkforward.step_months} mois)\n")
    if len(wins) < 3:
        print("⚠ moins de 3 fenêtres : le §9.2 exige au minimum 3 ans de données.")

    report = walk_forward(cfg, hist, initial_equity=args.equity,
                          start_ms=start_ms)
    verdict = acceptance(report, cfg)

    print("── Walk-forward (§9.2–9.3) " + "─" * 40)
    print(json.dumps(report.summary(), indent=2, ensure_ascii=False))
    for w in report.windows:
        print(f"  fenêtre {w.index}: params={w.params} "
              f"OOS PF={w.oos_metrics['profit_factor']} trades={w.oos_trades}")
    for note in report.notes:
        print(f"  note: {note}")

    print("\n── Critères d'acceptation (§9.4) " + "─" * 34)
    for name, check in verdict["checks"].items():
        mark = "PASS" if check["passed"] else "FAIL"
        print(f"  [{mark}] {name}: {check['value']} (seuil {check['threshold']})")

    sens = None
    if not args.skip_sensitivity:
        print("\n── Sensibilité ±20 % (§9.5) " + "─" * 39)
        sens = sensitivity(cfg, hist, initial_equity=args.equity)
        base_pf = sens["base"]["profit_factor"]
        print(f"  référence: PF={base_pf}, {sens['base']['trades']} trades")
        for v in sens["variants"]:
            if "error" in v:
                print(f"  {v['param']} {v['delta']:+.0%}: {v['error']}")
                continue
            flag = "  ← effondrement" if v["collapsed"] else ""
            print(f"  {v['param']} {v['delta']:+.0%} → {v['value']}: "
                  f"PF={v['profit_factor']} ({v['trades']} trades){flag}")
        if sens["fragile"]:
            print("  ⚠ §9.5 : un paramètre au moins fait s'effondrer le résultat "
                  "hors de sa valeur exacte — REJET.")

    gate = None
    if not args.skip_placebo:
        print(f"\n── Gate placebo ({args.placebo_draws} tirages) " + "─" * 30)
        gate = run_placebo(cfg, hist, n_draws=args.placebo_draws,
                           alpha=cfg.backtest.placebo.alpha, jobs=args.jobs,
                           initial_equity=args.equity)
        print(f"  réel: {gate.real_count} fenêtres OOS rentables")
        print(f"  p = {gate.p_value:.3f} (α={gate.alpha}) → "
              f"{'PASSE' if gate.passed else 'ÉCHOUE'}")
        for note in gate.notes:
            print(f"  note: {note}")

    print("\n── Provenance des coûts " + "─" * 42)
    print(f"  {hist.funding_provenance.summary()}")
    print("  ⚠ le funding est le poste le moins vérifiable du §9 : il reste NON VALIDÉ")
    print("    tant que le paper trading ne l'a pas mesuré en réel, position par position.")
    print("    Un verdict favorable ci-dessous ne vaut donc PAS validation du portage.")

    overall = (verdict["passed"]
               and (sens is None or not sens["fragile"])
               and (gate is None or gate.passed))
    print("\n" + "=" * 66)
    print("VERDICT : " + ("candidat recevable pour le paper testnet (§9.6)"
                          if overall else "REJETÉ — pas de paper, pas de mainnet"))
    print("=" * 66)
    if overall:
        print("Étapes restantes avant mainnet (§9.6) : paper testnet 2 semaines "
              "minimum, puis mainnet avec risk_pct divisé par 2 le premier mois.")

    if args.out:
        Path(args.out).write_text(json.dumps({
            "walkforward": [w.as_log() for w in report.windows],
            "summary": report.summary(),
            "acceptance": verdict,
            "sensitivity": sens,
            "placebo": None if gate is None else {
                "real_count": gate.real_count, "p_value": gate.p_value,
                "alpha": gate.alpha, "passed": gate.passed,
            },
            "overall_passed": overall,
            "funding": {
                "source": hist.funding_provenance.source,
                "points": hist.funding_provenance.points,
                "settlement_hours": hist.funding_provenance.settlement_hours,
                "validated": hist.funding_provenance.validated,
                "note": ("NON VALIDÉ — estimation historique appliquée à des positions "
                         "simulées ; à mesurer en paper trading avant tout mainnet"),
            },
            "price_source": args.source,
            "config_frozen": _frozen_config_hash(),
            "config_live": _live_config_hash(getattr(args, "config", None)),
            "config_untouched": check_config_frozen(getattr(args, "config", None)) is None,
        }, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        print(f"\nrapport écrit dans {args.out}")
    return 0 if overall else 1


def _frozen_config_hash() -> Optional[str]:
    """Hash de la configuration figée avant le run, s'il existe."""
    frozen = Path(__file__).resolve().parent / "state" / "frozen" / "FROZEN.json"
    if not frozen.exists():
        return None
    try:
        return json.loads(frozen.read_text(encoding="utf-8")).get("sha256")
    except (OSError, json.JSONDecodeError):
        return None


def _live_config_hash(path: Optional[Path]) -> Optional[str]:
    import hashlib

    p = Path(path) if path else config_mod.DEFAULT_PATH
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def check_config_frozen(config_path: Optional[Path]) -> Optional[str]:
    """Vérifie que la config n'a pas bougé depuis son gel. Rend un problème ou None.

    C'est la traduction exécutable d'une règle de méthode : **on ne retouche pas
    les paramètres pour faire passer le test.** Le gate placebo suppose un
    pipeline figé AVANT le tirage ; ajuster un seuil après avoir vu un résultat,
    puis relancer, est du multiple-testing — et la p-value obtenue ne veut alors
    plus rien dire, sans que rien dans le rapport ne le signale.

    La mémoire humaine est un mauvais gardien pour ça : trois semaines plus
    tard, personne ne se souvient si le `k_stop` a bougé entre deux runs. Le
    hash, lui, s'en souvient.
    """
    frozen = _frozen_config_hash()
    if frozen is None:
        return ("aucune configuration figée (confluence/state/frozen/FROZEN.json) — "
                "geler la config AVANT le tirage, sinon le gate placebo ne prouve rien")
    live = _live_config_hash(config_path)
    if live is None:
        return "configuration illisible pour le calcul de hash"
    if live != frozen:
        return (f"la configuration a CHANGÉ depuis son gel "
                f"(figée {frozen[:12]}, actuelle {live[:12]}) — relancer sur une config "
                f"modifiée après avoir vu un résultat est du multiple-testing ; "
                f"re-geler explicitement et enregistrer une NOUVELLE entrée au registre")
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="confluence.run", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", type=Path, default=None, help="chemin du YAML")
    p.add_argument("--days", type=float, default=None, help="période testée en jours")
    p.add_argument("--end-ms", type=int, default=None, help="borne haute (ms) pour figer un run")
    p.add_argument("--equity", type=float, default=10_000.0)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--throttle", type=float, default=0.0, help="pause entre requêtes API (s)")
    p.add_argument("--jobs", type=int, default=1)
    p.add_argument("--source", choices=("native", "binance"), default="native",
                   help="native = Hyperliquid (52 j de 15m seulement) ; "
                        "binance = proxy profond pour atteindre les 3 ans du §9.2")
    p.add_argument("--funding-source", choices=("hyperliquid", "binance"),
                   default="hyperliquid",
                   help="source des taux de funding. `fundingHistory` honore startTime, "
                        "donc le funding NATIF remonte jusqu'au lancement HL (2023) même "
                        "quand les prix viennent du proxy. Avant 2023, seul binance existe "
                        "(règlement 8h, autre lieu). Dans TOUS les cas le funding reste "
                        "NON VALIDÉ jusqu'au paper trading.")
    p.add_argument("--out", type=Path, default=None, help="rapport JSON")
    p.add_argument("-v", "--verbose", action="store_true")

    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch", help="charge et contrôle l'intégrité de l'historique")
    sub.add_parser("collect", help="accumule les bougies Hyperliquid natives dans l'archive")
    sub.add_parser("overlap", help="mesure la fidélité du proxy profond vs Hyperliquid")
    sub.add_parser("backtest", help="un backtest sur toute la période")
    sub.add_parser("explain", help="distribution des vetos — pourquoi le bot ne trade pas")
    v = sub.add_parser("validate", help="protocole §9 complet (bloquant avant mainnet)")
    v.add_argument("--placebo-draws", type=int, default=30)
    v.add_argument("--skip-placebo", action="store_true",
                   help="saute le gate placebo (déconseillé : c'est lui qui tue les faux edges)")
    v.add_argument("--skip-sensitivity", action="store_true")
    v.add_argument("--force", action="store_true",
                   help="produire un verdict malgré des données incomplètes (déconseillé : "
                        "un échec par trou de données ressemble à un échec de stratégie)")
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    cfg = config_mod.load(args.config)
    logger.info("configuration chargée: %s, grille par défaut %s", cfg.symbol, DEFAULT_GRID)
    handlers = {
        "fetch": cmd_fetch,
        "collect": cmd_collect,
        "overlap": cmd_overlap,
        "backtest": cmd_backtest,
        "explain": cmd_explain,
        "validate": cmd_validate,
    }
    return handlers[args.command](cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())

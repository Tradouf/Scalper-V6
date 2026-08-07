"""
Gate placebo — test obligatoire de toute sélection de stratégie/paramètres.

Principe (établi le 2026-08-07, cf. simplebot/VERDICT_2026-08-07.md) : un
pipeline de sélection (optimiseur walk-forward, filtre qualité, screening de
symboles…) n'a de valeur que s'il sélectionne PLUS sur les vraies données que
sur des séries où l'edge est nul par construction. Sinon, sa sélection est du
bruit — c'est exactement ce qui a condamné l'optimiseur EMA-cross de SimpleBot
(p = 0.83–0.90 : le placebo « confirmait » autant de symboles que le réel).

Null utilisé : permutation de l'ordre des barres en conservant
  - le rendement close-à-close de chaque barre,
  - la forme interne de chaque barre (open/close, high/close, low/close),
  - la série des timestamps et volumes d'origine (position par position).
⇒ distribution des rendements et des ranges préservée, autocorrélation
détruite : tout edge directionnel ou de persistance disparaît.

Usage programmatique :

    from placebo_gate import run_gate

    def mon_selecteur(candles_by_symbol: dict) -> set:
        ...retourne les symboles retenus (ou un int = leur nombre)...

    report = run_gate(candles_by_symbol, mon_selecteur, n_placebo=30, jobs=8)
    if not report.passed:
        # la sélection ne bat pas le bruit → ne PAS trader dessus
        ...

Usage CLI (référence : rejoue le pipeline SimpleBot sur le cache local) :

    python placebo_gate.py --n 30 --jobs 8

Règle de décision : passed ⇔ p_value < alpha (défaut 0.05), où
p_value = fraction des tirages placebo qui sélectionnent AU MOINS autant que
le réel. Une sélection qui ne passe pas n'est pas « à ajuster jusqu'à ce
qu'elle passe » — re-tester après modification = multiple-testing : figer le
pipeline AVANT de lancer le gate.
"""

from __future__ import annotations

import argparse
import random
from dataclasses import dataclass, field
from multiprocessing import Pool
from typing import Callable, Dict, List, Optional, Union

Candle = dict          # {"ts","open","high","low","close","volume"}
Selection = Union[int, "set[str]", "list[str]", "tuple[str, ...]"]


# ── Null : permutation des barres à rendements/formes conservés ──────────────

def shuffle_candles(candles: List[Candle], rng: random.Random) -> Optional[List[Candle]]:
    """
    Permute l'ordre des barres en conservant le rendement close-à-close et la
    forme interne (o/c, h/c, l/c) de chaque barre, puis recolle le chemin de
    prix. Timestamps et volumes restent ceux de la série d'origine, position
    par position (le sélecteur voit une série « normale »).

    Retourne None si la série est inutilisable (close ≤ 0, < 3 barres).
    """
    n = len(candles)
    if n < 3:
        return None
    rets, shapes = [], []
    for i in range(1, n):
        p, c = candles[i - 1], candles[i]
        if p["close"] <= 0 or c["close"] <= 0:
            return None
        rets.append(c["close"] / p["close"])
        shapes.append((c["open"] / c["close"], c["high"] / c["close"], c["low"] / c["close"]))
    order = list(range(len(rets)))
    rng.shuffle(order)
    out = [dict(candles[0])]
    px = candles[0]["close"]
    for k, j in enumerate(order):
        px *= rets[j]
        o, h, l = shapes[j]
        src = candles[k + 1]
        out.append({
            "ts": src["ts"],
            "open": px * o, "high": px * h, "low": px * l, "close": px,
            "volume": src["volume"],
        })
    return out


# ── Gate ─────────────────────────────────────────────────────────────────────

@dataclass
class PlaceboReport:
    real_count: int
    null_counts: List[int]
    p_value: float
    alpha: float
    passed: bool
    real_selection: Optional[list] = None
    n_symbols: int = 0
    notes: List[str] = field(default_factory=list)

    def summary(self) -> str:
        nc = sorted(self.null_counts)
        med = nc[len(nc) // 2] if nc else 0
        verdict = "PASSE" if self.passed else "ÉCHOUE (sélection ≈ bruit)"
        return (
            f"réel={self.real_count}/{self.n_symbols} sélectionnés | "
            f"placebo({len(nc)} tirages): méd={med} max={nc[-1] if nc else 0} | "
            f"p={self.p_value:.3f} (α={self.alpha}) → {verdict}"
        )


def _count(sel: Selection) -> int:
    return sel if isinstance(sel, int) else len(sel)


# Globals de worker (Pool) — évite de pickler les données à chaque tirage.
_WORK: dict = {}


def _init_worker(candles_by_symbol, selector):
    _WORK["data"] = candles_by_symbol
    _WORK["selector"] = selector


def _one_placebo(seed: int) -> int:
    rng = random.Random(seed)
    shuffled = {}
    for sym, candles in _WORK["data"].items():
        s = shuffle_candles(candles, rng)
        if s is not None:
            shuffled[sym] = s
    return _count(_WORK["selector"](shuffled))


def run_gate(
    candles_by_symbol: Dict[str, List[Candle]],
    selector: Callable[[Dict[str, List[Candle]]], Selection],
    n_placebo: int = 30,
    alpha: float = 0.05,
    seed: int = 0,
    jobs: int = 1,
) -> PlaceboReport:
    """
    Exécute le sélecteur sur le réel puis sur n_placebo permutations, et
    compare. `selector` reçoit {symbole: bougies} et retourne les symboles
    retenus (collection) ou leur nombre (int). Il doit être une fonction
    top-level (picklable) si jobs > 1.

    p_value = fraction des tirages placebo dont la sélection est ≥ au réel
    (avec correction +1/+1 pour ne jamais rendre p=0 sur peu de tirages).
    """
    notes = []
    # p minimal atteignable = 1/(n_placebo+1) : en dessous de n_placebo ≥ 1/α,
    # le gate ne PEUT pas passer (ex. α=0.05 exige ≥ 20 tirages).
    if 1.0 / (n_placebo + 1) >= alpha:
        notes.append(
            f"n_placebo={n_placebo} trop faible pour α={alpha} "
            f"(p min = {1.0/(n_placebo+1):.3f}) — augmenter n_placebo"
        )
    data = {s: c for s, c in candles_by_symbol.items() if len(c) >= 3}
    dropped = len(candles_by_symbol) - len(data)
    if dropped:
        notes.append(f"{dropped} symbole(s) ignoré(s) (série trop courte)")
    if not data:
        return PlaceboReport(0, [], 1.0, alpha, False, [], 0, notes + ["aucune donnée"])

    real_sel = selector(data)
    real_count = _count(real_sel)

    seeds = [seed * 1_000_003 + k for k in range(n_placebo)]
    if jobs > 1:
        with Pool(jobs, initializer=_init_worker, initargs=(data, selector)) as pool:
            null_counts = pool.map(_one_placebo, seeds)
    else:
        _init_worker(data, selector)
        null_counts = [_one_placebo(s) for s in seeds]

    # Correction de continuité (+1/+1) : p ne peut pas être 0 par chance.
    ge = sum(1 for c in null_counts if c >= real_count)
    p_value = (ge + 1) / (len(null_counts) + 1)
    passed = p_value < alpha

    return PlaceboReport(
        real_count=real_count,
        null_counts=null_counts,
        p_value=p_value,
        alpha=alpha,
        passed=passed,
        real_selection=sorted(real_sel) if not isinstance(real_sel, int) else None,
        n_symbols=len(data),
        notes=notes,
    )


# ── Référence CLI : pipeline SimpleBot sur le cache local ────────────────────

def _simplebot_selector(candles_by_symbol: Dict[str, List[Candle]]) -> set:
    """Rejoue optimize_symbol() + check_quality_gate() (pipeline historique)."""
    from simplebot.optimizer import BacktestOptimizerAgent
    from simplebot.symbol_filter import check_quality_gate
    agent = BacktestOptimizerAgent(symbols=list(candles_by_symbol))
    kept = set()
    for sym, candles in candles_by_symbol.items():
        entry = agent.optimize_symbol(candles)
        if entry.get("active") and check_quality_gate(sym, entry)[0]:
            kept.add(sym)
    return kept


def _load_cache(cache_dir: str, interval: str, min_bars: int) -> Dict[str, List[Candle]]:
    import json
    from pathlib import Path
    out = {}
    for p in sorted(Path(cache_dir).glob(f"*__{interval}.json")):
        candles = json.loads(p.read_text())["candles"]
        if len(candles) >= min_bars:
            out[p.name.split("__")[0]] = candles
    return out


if __name__ == "__main__":
    import logging
    import os
    logging.basicConfig(level=logging.WARNING)
    os.environ.setdefault("SIMPLEBOT_SYMBOLS", "BTC")   # pas de fetch univers

    ap = argparse.ArgumentParser(description="Gate placebo — la sélection bat-elle le bruit ?")
    ap.add_argument("--cache", default="state/ohlcv_cache")
    ap.add_argument("--interval", default="15m")
    ap.add_argument("--min-bars", type=int, default=4000)
    ap.add_argument("--n", type=int, default=30, help="tirages placebo")
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--jobs", type=int, default=1)
    args = ap.parse_args()

    data = _load_cache(args.cache, args.interval, args.min_bars)
    print(f"{len(data)} symboles chargés depuis {args.cache} ({args.interval})")
    report = run_gate(data, _simplebot_selector, n_placebo=args.n,
                      alpha=args.alpha, seed=args.seed, jobs=args.jobs)
    print(report.summary())
    if report.real_selection is not None:
        print("sélection réelle :", ", ".join(report.real_selection) or "(vide)")
    for note in report.notes:
        print("note :", note)
    raise SystemExit(0 if report.passed else 1)

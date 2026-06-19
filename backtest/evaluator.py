"""
Walk-forward evaluator — le garde-fou anti-overfit du backtest.

Pourquoi : un balayage qui choisit les meilleurs paramètres sur TOUTE la série et
rapporte leur PnL ment (cf. AO zero-cross 1h : +18% in-sample → −12% out-of-sample,
2026-06-18). Le seul chiffre honnête est la performance OUT-OF-SAMPLE : on choisit
les params sur une fenêtre d'entraînement, on les juge sur la fenêtre SUIVANTE jamais
vue, et on agrège sur plusieurs blocs (walk-forward). Un gate refuse une stratégie
qui ne tient pas en OOS net de frais.

Usage :
    from backtest.evaluator import WalkForwardEvaluator, expand_grid
    ev = WalkForwardEvaluator(backtester)
    combos = [{"tp_pct": 0.02, "sl_pct": 0.01}, {"tp_pct": 0.06, "sl_pct": 0.02}]
    rep = ev.evaluate(df, "BTC", "ao_zerocross", combos)
    print(rep.summary()); print("PASS" if rep.passed else "REJET", rep.gate_reasons)
"""
from __future__ import annotations

import itertools
import math
import statistics
from dataclasses import dataclass, field
from typing import Dict, List


def gate_report(
    rep: "WalkForwardReport",
    n_folds: int,
    min_total_oos_trades: int = 30,
    min_positive_fold_frac: float = 0.8,
    min_oos_median_pf: float = 1.05,
    min_oos_tstat: float = 1.5,
) -> tuple[bool, List[str]]:
    """Gate OOS partagé (OHLCV mono-symbole ET panier cross-sectionnel). Calibré à
    ~4% de faux positifs sur marche aléatoire. Renseigne passed + raisons sur rep."""
    reasons: List[str] = []
    if not rep.folds:
        reasons.append("aucun fold exploitable")
    else:
        if rep.oos_total_pnl <= 0:
            reasons.append(f"OOS pnl ≤ 0 ({rep.oos_total_pnl:.2f}%)")
        need_pos = math.ceil(n_folds * min_positive_fold_frac)
        if rep.positive_folds < need_pos:
            reasons.append(f"folds positifs {rep.positive_folds}/{n_folds} < {need_pos} requis")
        if rep.oos_total_trades < min_total_oos_trades:
            reasons.append(f"trades OOS {rep.oos_total_trades} < {min_total_oos_trades}")
        if rep.oos_median_pf < min_oos_median_pf:
            reasons.append(f"PF médian OOS {rep.oos_median_pf:.2f} < {min_oos_median_pf}")
        if rep.oos_tstat < min_oos_tstat:
            reasons.append(f"t-stat OOS {rep.oos_tstat:.2f} < {min_oos_tstat} (régularité insuffisante)")
    passed = not reasons
    rep.passed = passed
    rep.gate_reasons = reasons or ["tous critères OOS satisfaits"]
    return passed, rep.gate_reasons


def expand_grid(param_grid: Dict[str, list]) -> List[dict]:
    """Produit cartésien d'une grille {param: [valeurs]} → liste de dicts."""
    if not param_grid:
        return [{}]
    keys = list(param_grid.keys())
    return [dict(zip(keys, combo)) for combo in itertools.product(*[param_grid[k] for k in keys])]


@dataclass
class FoldResult:
    index: int
    train_n: int
    test_n: int
    best_params: dict
    train_pnl: float
    oos_pnl: float          # PnL net OUT-OF-SAMPLE (%, le chiffre qui compte)
    oos_trades: int
    oos_winrate: float
    oos_pf: float


@dataclass
class WalkForwardReport:
    symbol: str
    strategy: str
    n_folds: int
    fee_pct: float
    folds: List[FoldResult] = field(default_factory=list)
    # Benchmark de l'illusion : meilleur PnL plein-échantillon (ce qu'un balayage
    # naïf rapporterait) — à comparer à l'OOS agrégé pour mesurer l'overfit.
    in_sample_best_pnl: float = 0.0
    in_sample_best_params: dict = field(default_factory=dict)
    passed: bool = False
    gate_reasons: List[str] = field(default_factory=list)

    @property
    def oos_total_pnl(self) -> float:
        return sum(f.oos_pnl for f in self.folds)

    @property
    def oos_total_trades(self) -> int:
        return sum(f.oos_trades for f in self.folds)

    @property
    def positive_folds(self) -> int:
        return sum(1 for f in self.folds if f.oos_pnl > 0)

    @property
    def oos_median_pf(self) -> float:
        pfs = [f.oos_pf for f in self.folds if f.oos_trades > 0]
        return statistics.median(pfs) if pfs else 0.0

    @property
    def oos_tstat(self) -> float:
        """t-stat des PnL OOS par fold : moyenne / (écart-type / √k). Contrôle le
        bruit — un edge réel est positif ET régulier entre folds, pas un fold chanceux."""
        pnls = [f.oos_pnl for f in self.folds]
        if len(pnls) < 2:
            return 0.0
        sd = statistics.stdev(pnls)
        if sd <= 1e-9:
            return math.inf if statistics.mean(pnls) > 0 else 0.0
        return statistics.mean(pnls) / (sd / math.sqrt(len(pnls)))

    def summary(self) -> str:
        lines = [
            f"Walk-forward {self.strategy} {self.symbol}  ({self.n_folds} folds, frais {self.fee_pct:.3%}/côté)",
            f"{'fold':>4} {'train':>6} {'test':>6} {'OOS pnl%':>9} {'trades':>7} {'winr':>6} {'PF':>6}  params",
        ]
        for f in self.folds:
            lines.append(
                f"{f.index:>4} {f.train_n:>6} {f.test_n:>6} {f.oos_pnl:>9.2f} "
                f"{f.oos_trades:>7} {f.oos_winrate:>6.2f} {f.oos_pf:>6.2f}  {f.best_params}"
            )
        lines.append(
            f"\nOOS agrégé : pnl={self.oos_total_pnl:.2f}%  trades={self.oos_total_trades}  "
            f"folds positifs={self.positive_folds}/{self.n_folds}  PF médian={self.oos_median_pf:.2f}  "
            f"t-stat={self.oos_tstat:.2f}"
        )
        lines.append(
            f"In-sample (illusion) : {self.in_sample_best_pnl:.2f}% @ {self.in_sample_best_params}  "
            f"→ écart overfit = {self.in_sample_best_pnl - self.oos_total_pnl:.2f} pts"
        )
        lines.append(f"GATE : {'✅ PASS' if self.passed else '❌ REJET'}  {self.gate_reasons}")
        return "\n".join(lines)


class WalkForwardEvaluator:
    def __init__(self, backtester, fee_pct: float | None = None) -> None:
        self._bt = backtester
        self._fee = backtester.DEFAULT_FEE_PCT if fee_pct is None else fee_pct

    def evaluate(
        self,
        df,
        symbol: str,
        strategy: str,
        combos: List[dict],             # liste de jeux de params explicites (tp/sl appariés)
        n_folds: int = 5,
        train_frac: float = 0.6,
        select_metric: str = "pnl",     # "pnl" ou "pf"
        min_trades_train: int = 8,
        # Gate OOS (calibré ~3% faux positifs sur marche aléatoire, cf. test_evaluator).
        min_total_oos_trades: int = 30,
        min_positive_fold_frac: float = 0.8,
        min_oos_median_pf: float = 1.05,
        min_oos_tstat: float = 1.5,
    ) -> WalkForwardReport:
        df = df.reset_index(drop=True)
        grid = combos
        rep = WalkForwardReport(symbol=symbol, strategy=strategy, n_folds=n_folds, fee_pct=self._fee)

        # Benchmark in-sample (l'illusion) : meilleur PnL plein-échantillon.
        best_full_pnl, best_full_params = -math.inf, {}
        for params in grid:
            r = self._bt.run_on_df(df, symbol, strategy, fee_pct=self._fee, **params)
            if r.total_pnl > best_full_pnl:
                best_full_pnl, best_full_params = r.total_pnl, params
        rep.in_sample_best_pnl = best_full_pnl
        rep.in_sample_best_params = best_full_params

        # Folds walk-forward : blocs disjoints consécutifs, chacun split train→test.
        block = len(df) // n_folds
        for k in range(n_folds):
            seg = df.iloc[k * block:(k + 1) * block]
            split = int(len(seg) * train_frac)
            train, test = seg.iloc[:split], seg.iloc[split:]
            if len(train) < 50 or len(test) < 50:
                continue

            # Sélection des params sur le TRAIN.
            best_params, best_score = None, -math.inf
            for params in grid:
                tr = self._bt.run_on_df(train, symbol, strategy, fee_pct=self._fee, **params)
                if tr.nb_trades < min_trades_train:
                    continue
                score = tr.profit_factor if select_metric == "pf" else tr.total_pnl
                if score > best_score:
                    best_score, best_params = score, params
            if best_params is None:
                continue

            # Jugement sur le TEST (OOS, jamais vu pendant la sélection).
            oos = self._bt.run_on_df(test, symbol, strategy, fee_pct=self._fee, **best_params)
            rep.folds.append(FoldResult(
                index=k, train_n=len(train), test_n=len(test), best_params=best_params,
                train_pnl=best_score if select_metric == "pnl" else
                self._bt.run_on_df(train, symbol, strategy, fee_pct=self._fee, **best_params).total_pnl,
                oos_pnl=oos.total_pnl, oos_trades=oos.nb_trades,
                oos_winrate=oos.winrate, oos_pf=oos.profit_factor,
            ))

        # Gate OOS (fonction partagée).
        gate_report(
            rep, n_folds,
            min_total_oos_trades=min_total_oos_trades,
            min_positive_fold_frac=min_positive_fold_frac,
            min_oos_median_pf=min_oos_median_pf,
            min_oos_tstat=min_oos_tstat,
        )
        return rep

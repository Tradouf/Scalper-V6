"""
Adaptive scalper — « Le Danseur » (prototype offline, 2026-06-18).

Thèse (francois) : le marché bouge en RYTHMES successifs ; un scalper simple
(croisement de moyennes confirmé par RSI) dont on RÉ-OPTIMISE les paramètres en
continu (toutes les 15 min en live) pourrait épouser ces rythmes. Le risque mortel
est l'overfit : ré-ajuster 96×/jour = 96 occasions de fitter le bruit.

Ce module répond OFFLINE à la SEULE question qui décide si l'idée peut vivre :
  « Un scalper ré-optimisé sur une fenêtre glissante (sélection sur le passé,
    jugement sur le futur jamais vu) bat-il (a) le même scalper à paramètres
    FIXES et (b) les frais, en OUT-OF-SAMPLE ? »
Si la version adaptative ne bat pas des params figés + les frais EN BACKTEST,
elle ne le fera jamais en live.

Architecture honnête :
  - `compute_signals` : MA rapide × MA lente + confirmation RSI, AUCUN look-ahead
    (croisement lu en [i,i-1], RSI en [i], entrée au close de [i]).
  - `rolling_walkforward` : à chaque pas, sélectionne θ* sur la fenêtre de TRAIN
    (passé connu), le juge sur la fenêtre de TEST suivante (jamais vue), avance.
    L'agrégat des TEST = la courbe OOS honnête.
  - Réutilise `Backtester._simulate` (mêmes barrières TP/SL + mêmes frais nets) et
    `evaluator.gate_report` (même gate anti-overfit que tout le reste du repo).

Le LLM « chorégraphe » (rétrécir l'espace de recherche selon le rythme détecté)
n'intervient PAS ici : on valide d'abord que l'adaptation a un edge brut. Phase 2.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

from backtest.backtester import Backtester
from backtest.evaluator import FoldResult, WalkForwardReport, gate_report

# Instance sans client : on n'appelle que _simulate / _compute_result (sans réseau),
# pour garantir des barrières TP/SL et un calcul de frais IDENTIQUES au reste du repo.
_BT = Backtester(None)


@dataclass(frozen=True)
class ScalpParams:
    fast: int          # période MA rapide (2-20)
    slow: int          # période MA lente (30-50)
    rsi_period: int    # période RSI (2-20)
    rsi_thr: float     # confirmation : long si RSI≥thr, short si RSI≤100-thr (50-65)
    tp_pct: float      # take-profit (fraction du prix)
    sl_pct: float      # stop-loss (0 = TP seul)

    def key(self) -> tuple:
        return (self.fast, self.slow, self.rsi_period, self.rsi_thr, self.tp_pct, self.sl_pct)

    def label(self) -> dict:
        return {"fast": self.fast, "slow": self.slow, "rsi_p": self.rsi_period,
                "rsi_thr": self.rsi_thr, "tp": self.tp_pct, "sl": self.sl_pct}


def compute_rsi(close: pd.Series, period: int) -> pd.Series:
    """RSI Wilder (EMA, com=period-1) — même recette que Backtester._add_indicators."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    com = max(period - 1, 1)
    avg_gain = gain.ewm(com=com, adjust=False).mean()
    avg_loss = loss.ewm(com=com, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_signals(df: pd.DataFrame, p: ScalpParams) -> pd.Series:
    """Croisement MA confirmé par RSI. +1 = long, −1 = short, 0 = rien.

    LONG  : MA rapide croise AU-DESSUS de la lente ∧ RSI ≥ rsi_thr (momentum confirmé).
    SHORT : MA rapide croise EN-DESSOUS ∧ RSI ≤ 100−rsi_thr (symétrique).
    Pas de look-ahead : le croisement compare [i] et [i−1], le RSI est lu en [i],
    et l'entrée se fait au close de [i] (cf. _simulate). Tout est connu à la clôture."""
    close = df["close"]
    fast = close.rolling(p.fast).mean()
    slow = close.rolling(p.slow).mean()
    rsi = compute_rsi(close, p.rsi_period)
    cross_up = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    cross_dn = (fast < slow) & (fast.shift(1) >= slow.shift(1))
    long_ok = cross_up & (rsi >= p.rsi_thr)
    short_ok = cross_dn & (rsi <= (100.0 - p.rsi_thr))
    sig = pd.Series(0, index=df.index)
    sig[long_ok] = 1
    sig[short_ok] = -1
    return sig


def run_params(df: pd.DataFrame, symbol: str, p: ScalpParams, fee: float):
    """Backtest d'un jeu de params sur un df (métriques NETTES de frais)."""
    sig = compute_signals(df, p)
    trades = _BT._simulate(df, sig, p.tp_pct, p.sl_pct, fee_pct=fee)
    return _BT._compute_result(symbol, "ma_rsi", trades)


def default_grid() -> List[ScalpParams]:
    """Espace de recherche borné (les bornes demandées par francois). Modeste à
    dessein : plus la grille est large, plus on multiplie les tests → plus le gate
    doit être strict. Le chorégraphe LLM (Phase 2) rétrécira ça selon le rythme."""
    grid: List[ScalpParams] = []
    for fast in (3, 5, 8, 13):
        for slow in (30, 40, 50):
            for rsi_p in (3, 7, 14):
                for rsi_thr in (50.0, 55.0):
                    for tp in (0.005, 0.008):
                        sl = tp / 2.0  # ratio TP/SL = 2 (cohérent avec l'AO)
                        grid.append(ScalpParams(fast, slow, rsi_p, rsi_thr, tp, sl))
    return grid


def _precompute_signals(df: pd.DataFrame, grid: List[ScalpParams]) -> Dict[tuple, pd.Series]:
    """Signaux vectorisés une fois sur tout le df (warmup = historique complet),
    puis tranchés par fenêtre dans le rolling. Évite le recalcul par pas."""
    return {p.key(): compute_signals(df, p) for p in grid}


@dataclass
class AdaptiveReport:
    symbol: str
    train_bars: int
    test_bars: int
    fee: float
    n_steps: int
    adaptive_oos_pnl: float           # Σ pnl OOS de la version ADAPTATIVE (le chiffre clé)
    fixed_oos_pnl: float              # Σ pnl OOS d'un θ FIXE (consensus des choix)
    fixed_params: dict
    in_sample_best_pnl: float         # meilleur θ plein-échantillon (l'illusion)
    in_sample_best_params: dict
    n_distinct_theta: int             # combien de θ distincts choisis (= « la danse »)
    n_switches: int                   # combien de fois θ a changé d'un pas à l'autre
    report: WalkForwardReport         # folds OOS adaptatifs + verdict du gate

    def summary(self) -> str:
        r = self.report
        lines = [
            f"Adaptive scalper MA×RSI — {self.symbol}  "
            f"(train={self.train_bars} test={self.test_bars} bars, frais {self.fee:.3%}/côté)",
            f"pas walk-forward : {self.n_steps}  ·  θ distincts choisis : {self.n_distinct_theta}  "
            f"·  changements de θ : {self.n_switches}",
            "",
            f"  ADAPTATIF  (ré-optimisé)   OOS = {self.adaptive_oos_pnl:>8.2f}%   "
            f"folds+={r.positive_folds}/{r.n_folds}  PF méd={r.oos_median_pf:.2f}  t={r.oos_tstat:.2f}  "
            f"trades={r.oos_total_trades}",
            f"  FIXE       (θ consensus)   OOS = {self.fixed_oos_pnl:>8.2f}%   {self.fixed_params}",
            f"  IN-SAMPLE  (illusion)      pln = {self.in_sample_best_pnl:>8.2f}%   {self.in_sample_best_params}",
            "",
            f"  → adaptatif − fixe   = {self.adaptive_oos_pnl - self.fixed_oos_pnl:+.2f} pts "
            f"(l'adaptation {'AJOUTE' if self.adaptive_oos_pnl > self.fixed_oos_pnl else 'DÉTRUIT'} de la valeur OOS)",
            f"  → overfit (IS−OOS)   = {self.in_sample_best_pnl - self.adaptive_oos_pnl:+.2f} pts",
            f"  GATE : {'✅ PASS' if r.passed else '❌ REJET'}  {r.gate_reasons}",
        ]
        return "\n".join(lines)


def rolling_walkforward(
    df: pd.DataFrame,
    symbol: str,
    grid: List[ScalpParams] | None = None,
    train_bars: int = 1000,
    test_bars: int = 100,
    fee: float = Backtester.DEFAULT_FEE_PCT,
    min_trades_train: int = 5,
    select: str = "pnl",         # "pnl" ou "pf"
    # Gate (mêmes valeurs que l'evaluator ; le caller peut durcir).
    min_total_oos_trades: int = 30,
    min_positive_fold_frac: float = 0.8,
    min_oos_median_pf: float = 1.05,
    min_oos_tstat: float = 1.5,
) -> AdaptiveReport:
    """Walk-forward GLISSANT : à chaque pas, sélectionne θ* sur [t−train_bars, t[
    (passé), le juge sur [t, t+test_bars[ (futur jamais vu), avance de test_bars.
    Compare la courbe OOS adaptative à un θ FIXE (consensus) et à l'illusion in-sample."""
    if grid is None:
        grid = default_grid()
    df = df.reset_index(drop=True)
    n = len(df)
    sigs = _precompute_signals(df, grid)

    def _pnl(seg_slice: slice, p: ScalpParams):
        sub = df.iloc[seg_slice]
        sig = sigs[p.key()].iloc[seg_slice]
        trades = _BT._simulate(sub, sig, p.tp_pct, p.sl_pct, fee_pct=fee)
        return _BT._compute_result(symbol, "ma_rsi", trades)

    folds: List[FoldResult] = []
    chosen: List[ScalpParams] = []
    test_starts: List[int] = []
    idx = 0
    t = train_bars
    while t + test_bars <= n:
        train_sl = slice(t - train_bars, t)
        best, best_score = None, -math.inf
        for p in grid:
            r = _pnl(train_sl, p)
            if r.nb_trades < min_trades_train:
                continue
            score = r.profit_factor if select == "pf" else r.total_pnl
            if score > best_score:
                best_score, best = score, p
        if best is None:
            t += test_bars
            continue
        oos = _pnl(slice(t, t + test_bars), best)
        folds.append(FoldResult(
            index=idx, train_n=train_bars, test_n=test_bars, best_params=best.label(),
            train_pnl=best_score, oos_pnl=oos.total_pnl, oos_trades=oos.nb_trades,
            oos_winrate=oos.winrate, oos_pf=oos.profit_factor,
        ))
        chosen.append(best)
        test_starts.append(t)
        idx += 1
        t += test_bars

    rep = WalkForwardReport(symbol=symbol, strategy="ma_rsi_adaptive",
                            n_folds=len(folds), fee_pct=fee, folds=folds)

    # Illusion in-sample : meilleur θ sur tout le df.
    is_best_pnl, is_best_p = -math.inf, None
    for p in grid:
        r = run_params(df, symbol, p, fee)
        if r.total_pnl > is_best_pnl:
            is_best_pnl, is_best_p = r.total_pnl, p
    rep.in_sample_best_pnl = is_best_pnl
    rep.in_sample_best_params = is_best_p.label() if is_best_p else {}

    # Baseline FIXE : θ le plus souvent choisi (le « rythme consensus »), appliqué
    # sur EXACTEMENT les mêmes fenêtres de test OOS → comparaison équitable.
    fixed_oos = 0.0
    fixed_p = None
    if chosen:
        fixed_key = Counter(p.key() for p in chosen).most_common(1)[0][0]
        fixed_p = next(p for p in grid if p.key() == fixed_key)
        for ts in test_starts:
            fixed_oos += _pnl(slice(ts, ts + test_bars), fixed_p).total_pnl

    gate_report(rep, max(len(folds), 1),
                min_total_oos_trades=min_total_oos_trades,
                min_positive_fold_frac=min_positive_fold_frac,
                min_oos_median_pf=min_oos_median_pf,
                min_oos_tstat=min_oos_tstat)

    n_switches = sum(1 for a, b in zip(chosen, chosen[1:]) if a.key() != b.key())
    return AdaptiveReport(
        symbol=symbol, train_bars=train_bars, test_bars=test_bars, fee=fee,
        n_steps=len(folds),
        adaptive_oos_pnl=rep.oos_total_pnl,
        fixed_oos_pnl=round(fixed_oos, 2),
        fixed_params=fixed_p.label() if fixed_p else {},
        in_sample_best_pnl=round(is_best_pnl, 2) if is_best_p else 0.0,
        in_sample_best_params=rep.in_sample_best_params,
        n_distinct_theta=len({p.key() for p in chosen}),
        n_switches=n_switches,
        report=rep,
    )

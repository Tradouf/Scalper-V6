"""
Stat-arb par cointégration (pairs trading) — hypothèse #9, recherche Opus 2026-06-18.

MÉCANISME (pourquoi un edge devrait persister) : deux actifs partageant un facteur
commun (ex. deux L1 majeurs) ont un SPREAD log(pa) − β·log(pb) qui oscille autour
d'un équilibre. Quand un flux temporaire écarte le spread (un gros ordre sur un seul
des deux), il revient — et celui qui paie est l'agent pressé qui a creusé l'écart.
Market-NEUTRAL par construction (long une jambe, short l'autre) → PAS le tail risk
directionnel qui a tué le funding fade. Famille « valeur relative », non testée jusqu'ici
(≠ cross-sectional momentum, qui classait des rendements absolus).

Cointégration sans statsmodels : ratio de couverture β par OLS sur les logs, puis
test de Dickey-Fuller (t-stat du coefficient de retour à la moyenne, 0 lag, constante)
sur le résidu. t < ~−2,9 ≈ stationnaire à 5% → paire cointégrée.

DISCIPLINE ANTI-OVERFIT : sélection des paires + β + seuils UNIQUEMENT sur le train de
chaque fold ; z-score CAUSAL (rolling, pas de fuite) ; jugement OOS ; gate de
`evaluator.py` avec barre t-stat RELEVÉE (on teste C(n,2) paires × grille → multi-testing).
"""
from __future__ import annotations

import math
from itertools import combinations
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from backtest.backtester import BacktestResult
from backtest.cross_sectional import _result_from_periods
from backtest.evaluator import FoldResult, WalkForwardReport, gate_report


# ─── Cointégration (Engle-Granger maison) ──────────────────────────────────────
def hedge_ratio(log_a: np.ndarray, log_b: np.ndarray) -> Tuple[float, float]:
    """OLS log_a = α + β·log_b → (α, β). β = ratio de couverture."""
    X = np.column_stack([np.ones_like(log_b), log_b])
    coef, *_ = np.linalg.lstsq(X, log_a, rcond=None)
    return float(coef[0]), float(coef[1])


def adf_tstat(resid: np.ndarray) -> float:
    """t-stat de Dickey-Fuller (0 lag, constante) sur le résidu. Régression
    Δr_t = a + b·r_{t−1} ; t-stat de b. Plus négatif = plus stationnaire
    (retour à la moyenne fort). ≈ −2,9 = seuil 5%, −3,4 = seuil 1%."""
    r = np.asarray(resid, dtype=float)
    if len(r) < 20:
        return 0.0
    dr = np.diff(r)
    rlag = r[:-1]
    X = np.column_stack([np.ones_like(rlag), rlag])
    coef, *_ = np.linalg.lstsq(X, dr, rcond=None)
    fitted = X @ coef
    resid2 = dr - fitted
    dof = len(dr) - 2
    if dof <= 0:
        return 0.0
    s2 = float(resid2 @ resid2) / dof
    try:
        xtx_inv = np.linalg.inv(X.T @ X)
    except np.linalg.LinAlgError:
        return 0.0
    se_b = math.sqrt(max(s2 * xtx_inv[1, 1], 1e-18))
    return float(coef[1] / se_b)


def half_life(resid: np.ndarray) -> float:
    """Demi-vie de retour à la moyenne (barres), depuis l'AR(1) du résidu."""
    r = np.asarray(resid, dtype=float)
    dr = np.diff(r)
    rlag = r[:-1]
    X = np.column_stack([np.ones_like(rlag), rlag])
    coef, *_ = np.linalg.lstsq(X, dr, rcond=None)
    b = coef[1]
    if b >= 0:
        return math.inf
    return float(-math.log(2) / math.log(1 + b)) if (1 + b) > 0 else math.inf


def select_pairs(closes_train: pd.DataFrame, adf_thr: float = -2.9,
                 max_pairs: int = 5, hl_min: float = 2.0, hl_max: float = 200.0
                 ) -> List[dict]:
    """Sur le TRAIN uniquement : teste toutes les paires, garde les cointégrées
    (ADF < seuil ET demi-vie raisonnable), triées par stationnarité (ADF le plus
    négatif). Renvoie [{a, b, beta, adf, hl}] (au plus max_pairs)."""
    syms = list(closes_train.columns)
    logs = {s: np.log(closes_train[s].to_numpy(dtype=float)) for s in syms}
    out: List[dict] = []
    for a, b in combinations(syms, 2):
        la, lb = logs[a], logs[b]
        if np.any(~np.isfinite(la)) or np.any(~np.isfinite(lb)):
            continue
        _, beta = hedge_ratio(la, lb)
        if not np.isfinite(beta) or abs(beta) < 1e-6:
            continue
        resid = la - beta * lb
        t = adf_tstat(resid)
        hl = half_life(resid)
        if t < adf_thr and hl_min <= hl <= hl_max:
            out.append({"a": a, "b": b, "beta": beta, "adf": t, "hl": hl})
    out.sort(key=lambda d: d["adf"])  # plus négatif d'abord
    return out[:max_pairs]


# ─── Backtest d'une paire ───────────────────────────────────────────────────────
def _causal_z(spread: np.ndarray, window: int) -> np.ndarray:
    """Z-score CAUSAL : moyenne/écart-type glissants sur les `window` barres
    PRÉCÉDENTES (shift implicite → la barre t n'utilise que [t−window, t−1])."""
    s = pd.Series(spread)
    mean = s.rolling(window).mean().shift(1)
    std = s.rolling(window).std().shift(1)
    z = (s - mean) / std.replace(0, np.nan)
    return z.to_numpy()


def pair_backtest(close_a: pd.Series, close_b: pd.Series, beta: float,
                  entry_z: float = 2.0, exit_z: float = 0.5, z_window: int = 100,
                  hold_max: int = 96, fee_pct: float = 0.00045,
                  strategy: str = "pairs") -> BacktestResult:
    """Trade le spread s = log(a) − β·log(b). |z| > entry_z → entrée (short le
    spread si z>0, long si z<0) ; sortie quand |z| < exit_z, retournement de signe,
    ou hold_max atteint. PnL par trade normalisé gross=1 = signe·Δs/(1+|β|), net du
    round-trip (2·fee, deux jambes ramenées à gross unitaire). Pas de look-ahead."""
    la = np.log(close_a.to_numpy(dtype=float))
    lb = np.log(close_b.to_numpy(dtype=float))
    n = len(la)
    spread = la - beta * lb
    z = _causal_z(spread, z_window)
    roundtrip = 2.0 * (fee_pct or 0.0)
    norm = 1.0 + abs(beta)

    periods: List[float] = []
    pos = 0          # 0 flat, +1 long spread, −1 short spread
    entry_s = 0.0
    entry_i = 0
    for i in range(n):
        zi = z[i]
        if not np.isfinite(zi):
            continue
        if pos == 0:
            if zi > entry_z:
                pos, entry_s, entry_i = -1, spread[i], i      # spread riche → short
            elif zi < -entry_z:
                pos, entry_s, entry_i = +1, spread[i], i      # spread bas → long
        else:
            exit_now = (abs(zi) < exit_z) or (i - entry_i >= hold_max) \
                or (pos == +1 and zi > entry_z) or (pos == -1 and zi < -entry_z)
            if exit_now:
                pnl = pos * (spread[i] - entry_s) / norm - roundtrip
                periods.append(float(pnl))
                pos = 0
    # Clôture mark-to-market d'une position encore ouverte.
    if pos != 0:
        pnl = pos * (spread[-1] - entry_s) / norm - roundtrip
        periods.append(float(pnl))

    return _result_from_periods(periods, "pair", strategy)


# ─── Walk-forward OOS (pair + params re-sélectionnés par fold) ──────────────────
class PairsWalkForward:
    """Walk-forward : par fold, sélectionne sur le TRAIN la meilleure (paire, params)
    parmi les paires cointégrées, juge OOS sur le TEST. Gate avec barre relevée."""

    def __init__(self, fee_pct: float = 0.00045) -> None:
        self._fee = fee_pct

    def evaluate(self, closes: pd.DataFrame, combos: List[dict], n_folds: int = 6,
                 train_frac: float = 0.6, select_metric: str = "pnl",
                 min_trades_train: int = 8, adf_thr: float = -2.9, max_pairs: int = 5,
                 # Gate RELEVÉ (multi-testing : C(n,2) paires × grille).
                 min_total_oos_trades: int = 30, min_positive_fold_frac: float = 0.8,
                 min_oos_median_pf: float = 1.10, min_oos_tstat: float = 2.0,
                 ) -> WalkForwardReport:
        closes = closes.reset_index(drop=True)
        rep = WalkForwardReport(symbol=f"pairs({closes.shape[1]})",
                                strategy="pairs_coint", n_folds=n_folds, fee_pct=self._fee)

        # Illusion in-sample : meilleure (paire, params) sur tout l'historique.
        full_pairs = select_pairs(closes, adf_thr=adf_thr, max_pairs=max_pairs)
        best_full, best_full_p = -math.inf, {}
        for pr in full_pairs:
            for c in combos:
                r = pair_backtest(closes[pr["a"]], closes[pr["b"]], pr["beta"],
                                  fee_pct=self._fee, **c)
                if r.total_pnl > best_full:
                    best_full = r.total_pnl
                    best_full_p = {"pair": f"{pr['a']}/{pr['b']}", **c}
        rep.in_sample_best_pnl = best_full
        rep.in_sample_best_params = best_full_p

        block = len(closes) // n_folds
        for kf in range(n_folds):
            lo, hi = kf * block, (kf + 1) * block
            split = lo + int((hi - lo) * train_frac)
            if (split - lo) < 80 or (hi - split) < 60:
                continue
            train = closes.iloc[lo:split]
            test = closes.iloc[split:hi]
            pairs = select_pairs(train, adf_thr=adf_thr, max_pairs=max_pairs)
            if not pairs:
                continue
            best_params, best_score, best_pair = None, -math.inf, None
            for pr in pairs:
                for c in combos:
                    tr = pair_backtest(train[pr["a"]], train[pr["b"]], pr["beta"],
                                       fee_pct=self._fee, **c)
                    if tr.nb_trades < min_trades_train:
                        continue
                    score = tr.profit_factor if select_metric == "pf" else tr.total_pnl
                    if score > best_score:
                        best_score, best_params, best_pair = score, c, pr
            if best_params is None:
                continue
            # OOS : β estimé sur le TRAIN (best_pair["beta"]), z causal sur le test.
            oos = pair_backtest(test[best_pair["a"]], test[best_pair["b"]],
                                best_pair["beta"], fee_pct=self._fee, **best_params)
            rep.folds.append(FoldResult(
                index=kf, train_n=(split - lo), test_n=(hi - split),
                best_params={"pair": f"{best_pair['a']}/{best_pair['b']}",
                             "adf": round(best_pair["adf"], 2), **best_params},
                train_pnl=best_score, oos_pnl=oos.total_pnl, oos_trades=oos.nb_trades,
                oos_winrate=oos.winrate, oos_pf=oos.profit_factor,
            ))

        gate_report(rep, n_folds, min_total_oos_trades=min_total_oos_trades,
                    min_positive_fold_frac=min_positive_fold_frac,
                    min_oos_median_pf=min_oos_median_pf, min_oos_tstat=min_oos_tstat)
        return rep

    def evaluate_book(self, closes: pd.DataFrame, combos: List[dict], n_folds: int = 6,
                      train_frac: float = 0.6, book_size: int = 5, adf_thr: float = -2.9,
                      min_trades_train: int = 8,
                      min_total_oos_trades: int = 30, min_positive_fold_frac: float = 0.8,
                      min_oos_median_pf: float = 1.10, min_oos_tstat: float = 2.0,
                      ) -> WalkForwardReport:
        """Variante BOOK : par fold, trade les `book_size` paires les plus cointégrées
        du train, ÉQUIPONDÉRÉES (chacune 1/N du gross), avec un jeu de params COMMUN
        (sélectionné sur le PnL agrégé du book au train → moins de multi-testing que
        des params par paire). Diversifie le risque de décohérence idiosyncratique
        (cf. fold qui explose en single-pair) → réduit la variance inter-fold → t-stat.
        Le PnL du book = moyenne des PnL de paire (chaque trade de paire crédité 1/N)."""
        closes = closes.reset_index(drop=True)
        rep = WalkForwardReport(symbol=f"book({closes.shape[1]})",
                                strategy=f"pairs_book{book_size}", n_folds=n_folds, fee_pct=self._fee)

        def book_periods(panel, pairs, combo) -> List[float]:
            """Périodes poolées du book : pour chaque paire, ses trades crédités 1/N."""
            n = max(len(pairs), 1)
            pooled: List[float] = []
            for pr in pairs:
                r = pair_backtest(panel[pr["a"]], panel[pr["b"]], pr["beta"],
                                  fee_pct=self._fee, **combo)
                pooled.extend(float(t["pnl"]) / n for t in r.trades)
            return pooled

        # Illusion in-sample sur le book complet.
        full_pairs = select_pairs(closes, adf_thr=adf_thr, max_pairs=book_size)
        best_full = -math.inf
        for c in combos:
            pooled = book_periods(closes, full_pairs, c)
            tot = sum(pooled) * 100
            if tot > best_full:
                best_full, rep.in_sample_best_params = tot, dict(c)
        rep.in_sample_best_pnl = round(best_full, 2)

        block = len(closes) // n_folds
        for kf in range(n_folds):
            lo, hi = kf * block, (kf + 1) * block
            split = lo + int((hi - lo) * train_frac)
            if (split - lo) < 80 or (hi - split) < 60:
                continue
            train, test = closes.iloc[lo:split], closes.iloc[split:hi]
            pairs = select_pairs(train, adf_thr=adf_thr, max_pairs=book_size)
            if not pairs:
                continue
            best_c, best_score = None, -math.inf
            for c in combos:
                pooled = book_periods(train, pairs, c)
                if len(pooled) < min_trades_train:
                    continue
                score = sum(pooled)
                if score > best_score:
                    best_score, best_c = score, c
            if best_c is None:
                continue
            oos_pooled = book_periods(test, pairs, best_c)
            oos = _result_from_periods(oos_pooled, "book", "pairs_book")
            rep.folds.append(FoldResult(
                index=kf, train_n=(split - lo), test_n=(hi - split),
                best_params={"pairs": [f"{p['a']}/{p['b']}" for p in pairs], **best_c},
                train_pnl=round(best_score * 100, 2), oos_pnl=oos.total_pnl,
                oos_trades=oos.nb_trades, oos_winrate=oos.winrate, oos_pf=oos.profit_factor,
            ))

        gate_report(rep, n_folds, min_total_oos_trades=min_total_oos_trades,
                    min_positive_fold_frac=min_positive_fold_frac,
                    min_oos_median_pf=min_oos_median_pf, min_oos_tstat=min_oos_tstat)
        return rep


def default_combos() -> List[dict]:
    """Grille de seuils (modeste : multi-testing oblige)."""
    out = []
    for entry_z in (1.5, 2.0, 2.5):
        for exit_z in (0.0, 0.5):
            for z_window in (60, 120):
                out.append({"entry_z": entry_z, "exit_z": exit_z,
                            "z_window": z_window, "hold_max": 96})
    return out

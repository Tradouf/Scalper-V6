"""
Backtest cross-sectionnel (panier multi-symboles) — hypothèse #5.

Momentum/reversal RELATIF, market-neutral : à chaque rebalancement, on classe les
symboles par leur rendement sur `lookback` barres, on est LONG le top-k / SHORT le
bottom-k (momentum, sign=+1) ou l'inverse (reversal, sign=−1). Gross=1 (0,5 long +
0,5 short) → exposition nette ≈ 0 (enlève le beta BTC). Edge visé = prime de
momentum cross-sectionnelle, pas un motif de prix mono-actif.

Diffère du backtester TP/SL (mono-position) : ici on mesure le rendement de
portefeuille par période de détention, net de frais de rotation. On réutilise le
gate walk-forward OOS de `evaluator.py` (FoldResult + gate_report).
"""
from __future__ import annotations

import math
from typing import Dict, List

import numpy as np
import pandas as pd

from backtest.backtester import BacktestResult
from backtest.evaluator import FoldResult, WalkForwardReport, gate_report


def fetch_panel(client, symbols: List[str], interval: str, days: int) -> pd.DataFrame:
    """Récupère les closes alignés (inner-join sur ts) → DataFrame colonnes=symboles."""
    cols = {}
    for sym in symbols:
        try:
            rows = client.get_ohlcv(sym, interval=interval, days=days)
        except Exception:
            rows = None
        if not rows:
            continue
        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        s = df.set_index("ts")["close"].astype(float).sort_index()
        # Dédoublonne les ts (candleSnapshot HL peut renvoyer des doublons) → garde le dernier.
        cols[sym] = s[~s.index.duplicated(keep="last")]
    if len(cols) < 2:
        raise ValueError("Panel insuffisant (< 2 symboles avec données)")
    closes = pd.concat(cols, axis=1, join="inner").sort_index()
    closes.columns = list(cols.keys())
    return closes


def build_funding_matrix(adapter, symbols: List[str], closes_index, days: int) -> pd.DataFrame:
    """Funding ACCRU par barre : somme les settlements horaires HL tombant dans
    [open_barre, open_barre_suivante). Correct pour toute taille de barre (1h = 1
    settlement/barre comme avant ; 4h = somme des ~4). `adapter` doit exposer
    get_funding_history(coin, days) -> [(ts_ms, rate)]."""
    import time
    idx = pd.Index(closes_index)
    idx_vals = idx.astype("int64").to_numpy()
    order = np.argsort(idx_vals)
    sorted_idx = idx_vals[order]
    cols = {}
    for j, sym in enumerate(symbols):
        if j:
            time.sleep(0.3)  # politesse anti-429 (la pagination funding martèle l'API)
        hist = adapter.get_funding_history(sym, days=days)
        if not hist:
            cols[sym] = np.nan
            continue
        h = np.array(hist, dtype=float)  # colonnes [ts, rate]
        pos = np.searchsorted(sorted_idx, h[:, 0], side="right") - 1  # barre contenant le settlement
        valid = pos >= 0
        accrued = np.zeros(len(idx), dtype=float)
        np.add.at(accrued, order[pos[valid]], h[valid, 1])
        cols[sym] = accrued
    return pd.DataFrame(cols, index=idx)


def cs_backtest(closes: pd.DataFrame, lookback: int = 24, k: int = 2, sign: int = 1,
                rebal: int = 24, fee_pct: float = 0.00045,
                rank_matrix: pd.DataFrame | None = None,
                carry_matrix: pd.DataFrame | None = None) -> BacktestResult:
    """Backteste le panier sur une matrice de closes (index positionnel ignoré).

    Le CLASSEMENT à la barre t utilise soit le momentum prix (rank_matrix=None :
    closes[t]/closes[t−lookback]−1), soit une matrice fournie (rank_matrix, ex.
    funding cumulé). sign=+1 → LONG le top du classement / SHORT le bottom ; sign=−1
    inverse (reversal, ou « fade » du funding). Rendement par période = Σ poids ×
    rendement forward des PRIX, net du coût de rotation (round-trip sur gross=1).
    """
    closes = closes.reset_index(drop=True)
    if rank_matrix is not None:
        rank_matrix = rank_matrix.reset_index(drop=True)
    if carry_matrix is not None:
        carry_matrix = carry_matrix.reset_index(drop=True)
    n, n_sym = len(closes), closes.shape[1]
    k = max(1, min(k, n_sym // 2))
    roundtrip = 2.0 * (fee_pct or 0.0)
    periods: List[float] = []

    t = lookback
    while t < n - 1:
        t2 = min(t + rebal, n - 1)
        if rank_matrix is None:
            score = (closes.iloc[t] / closes.iloc[t - lookback] - 1.0)
        else:
            score = rank_matrix.iloc[t]
        score = score.replace([np.inf, -np.inf], np.nan).dropna()
        if len(score) >= 2 * k:
            ranked = score.sort_values()
            shorts = list(ranked.index[:k])
            longs = list(ranked.index[-k:])
            if sign < 0:
                longs, shorts = shorts, longs
            fwd = (closes.iloc[t2] / closes.iloc[t] - 1.0)
            w = 0.5 / k
            port = w * fwd[longs].sum() - w * fwd[shorts].sum()
            # Carry de funding encaissé pendant la détention (t, t2] : le short reçoit
            # le funding (f>0), le long le paie. C'est l'edge du harvest market-neutral.
            if carry_matrix is not None:
                seg = carry_matrix.iloc[t + 1:t2 + 1]
                port += w * (seg[shorts].sum().sum() - seg[longs].sum().sum())
            periods.append(float(port) - roundtrip)
        t = t2

    tag = "cs_fund" if rank_matrix is not None else "cs_mom"
    return _result_from_periods(periods, f"panel({n_sym})", f"{tag}(L{lookback},k{k},s{sign})")


def _result_from_periods(periods: List[float], symbol: str, strategy: str) -> BacktestResult:
    if not periods:
        return BacktestResult(symbol=symbol, strategy=strategy, nb_trades=0,
                              total_pnl=0.0, winrate=0.0, profit_factor=0.0, max_drawdown=0.0)
    arr = np.array(periods, dtype=float)
    winners = arr[arr > 0]
    losers = arr[arr < 0]
    gp, gl = float(winners.sum()), float(abs(losers.sum()))
    pf = gp / gl if gl > 0 else (999.0 if gp > 0 else 0.0)
    cum = np.cumsum(arr)
    peak = np.maximum.accumulate(cum)
    max_dd = float(np.max(peak - cum)) * 100
    return BacktestResult(
        symbol=symbol, strategy=strategy, nb_trades=len(arr),
        total_pnl=round(float(arr.sum()) * 100, 2),
        winrate=round(len(winners) / len(arr), 3),
        profit_factor=round(pf, 2), max_drawdown=round(max_dd, 2),
        trades=[{"pnl": float(p)} for p in periods],
    )


class CrossSectionalWalkForward:
    """Walk-forward OOS pour le panier cross-sectionnel (même gate que l'évaluateur OHLCV)."""

    def __init__(self, fee_pct: float = 0.00045) -> None:
        self._fee = fee_pct

    def evaluate(self, closes: pd.DataFrame, combos: List[dict], n_folds: int = 5,
                 train_frac: float = 0.6, select_metric: str = "pnl",
                 min_trades_train: int = 8, rank_matrices: List[pd.DataFrame] | None = None,
                 carry_matrix: pd.DataFrame | None = None,
                 strategy: str = "cs_momentum") -> WalkForwardReport:
        """rank_matrices : None (momentum prix) ou liste ALIGNÉE sur combos (une
        matrice de classement par combo, ex. funding cumulé pour chaque lookback).
        carry_matrix : funding par barre (crédite le carry encaissé), partagé."""
        closes = closes.reset_index(drop=True)
        if rank_matrices is not None:
            rank_matrices = [rm.reset_index(drop=True) for rm in rank_matrices]
        if carry_matrix is not None:
            carry_matrix = carry_matrix.reset_index(drop=True)
        rep = WalkForwardReport(symbol=f"panel({closes.shape[1]})",
                                strategy=strategy, n_folds=n_folds, fee_pct=self._fee)

        def rm_for(i):
            return None if rank_matrices is None else rank_matrices[i]

        def slice_rm(i, lo, hi):
            rm = rm_for(i)
            return None if rm is None else rm.iloc[lo:hi]

        def slice_carry(lo, hi):
            return None if carry_matrix is None else carry_matrix.iloc[lo:hi]

        # Benchmark in-sample (l'illusion).
        best_full, best_full_params = -math.inf, {}
        for i, params in enumerate(combos):
            r = cs_backtest(closes, fee_pct=self._fee, rank_matrix=rm_for(i),
                            carry_matrix=carry_matrix, **params)
            if r.total_pnl > best_full:
                best_full, best_full_params = r.total_pnl, params
        rep.in_sample_best_pnl = best_full
        rep.in_sample_best_params = best_full_params

        block = len(closes) // n_folds
        for kf in range(n_folds):
            lo, hi = kf * block, (kf + 1) * block
            seg_len = hi - lo
            split = lo + int(seg_len * train_frac)
            if (split - lo) < 50 or (hi - split) < 50:
                continue
            best_i, best_params, best_score = None, None, -math.inf
            for i, params in enumerate(combos):
                tr = cs_backtest(closes.iloc[lo:split], fee_pct=self._fee,
                                 rank_matrix=slice_rm(i, lo, split),
                                 carry_matrix=slice_carry(lo, split), **params)
                if tr.nb_trades < min_trades_train:
                    continue
                score = tr.profit_factor if select_metric == "pf" else tr.total_pnl
                if score > best_score:
                    best_score, best_i, best_params = score, i, params
            if best_params is None:
                continue
            oos = cs_backtest(closes.iloc[split:hi], fee_pct=self._fee,
                              rank_matrix=slice_rm(best_i, split, hi),
                              carry_matrix=slice_carry(split, hi), **best_params)
            train_best = cs_backtest(closes.iloc[lo:split], fee_pct=self._fee,
                                     rank_matrix=slice_rm(best_i, lo, split),
                                     carry_matrix=slice_carry(lo, split), **best_params)
            rep.folds.append(FoldResult(
                index=kf, train_n=(split - lo), test_n=(hi - split), best_params=best_params,
                train_pnl=train_best.total_pnl, oos_pnl=oos.total_pnl, oos_trades=oos.nb_trades,
                oos_winrate=oos.winrate, oos_pf=oos.profit_factor,
            ))

        gate_report(rep, n_folds)
        return rep

"""
Funding fade à SEUIL EXTRÊME, avec carry — hypothèse #1 raffinée (2026-06-18).

Correction d'un défaut du 1er test cross-sectionnel : il ne comptait que le PnL
PRIX. Or fader un funding extrême, c'est SHORT un actif à funding très positif → on
REÇOIT ce funding à chaque heure (carry positif) en plus de la mean-reversion du
positionnement surpeuplé. Le carry EST l'edge structurel — il faut le créditer.

Mécanique (par symbole, indépendant) :
  - funding annualisé f_ann = funding_horaire × 24 × 365.
  - Entrée : f_ann > +entry_thr → SHORT (fade) ; f_ann < −entry_thr → LONG.
  - Pendant la détention : carry += −side × funding_horaire (short reçoit si f>0).
  - Sortie : |f_ann| < exit_thr (hystérésis, le funding s'est normalisé) OU max_hold
    barres atteint OU fin de données. PnL = side×(px_sortie/px_entrée−1) + carry − frais.

Moins de trades (seuil = haute conviction) → moins de frais (le tueur identifié au
1er test). Trades poolés sur tous les symboles → assez pour le gate. Jugé en
walk-forward OOS net de frais (même gate que le reste du harnais).
"""
from __future__ import annotations

import math
from typing import Dict, List

import numpy as np
import pandas as pd

from backtest.backtester import BacktestResult
from backtest.cross_sectional import _result_from_periods
from backtest.evaluator import FoldResult, WalkForwardReport, gate_report

HOURS_PER_YEAR = 24 * 365


def _fade_symbol(px: np.ndarray, f_hourly: np.ndarray, entry_thr: float, exit_thr: float,
                 max_hold: int, fee_pct: float) -> List[float]:
    """Trades (PnL fractionnaires, prix + carry − frais) d'un symbole. Seuils en
    funding ANNUALISÉ (fraction, ex. 0.30 = 30%/an)."""
    n = len(px)
    roundtrip = 2.0 * (fee_pct or 0.0)
    f_ann = f_hourly * HOURS_PER_YEAR
    trades: List[float] = []
    side = 0          # 0 flat, +1 long, -1 short
    entry_px = 0.0
    entry_i = 0
    carry = 0.0
    for i in range(n):
        if not np.isfinite(f_ann[i]) or not np.isfinite(px[i]):
            continue
        if side == 0:
            if f_ann[i] > entry_thr:
                side, entry_px, entry_i, carry = -1, px[i], i, 0.0   # fade funding positif → SHORT
            elif f_ann[i] < -entry_thr:
                side, entry_px, entry_i, carry = 1, px[i], i, 0.0     # fade funding négatif → LONG
        else:
            carry += -side * f_hourly[i]   # short reçoit le funding quand f>0
            normalized = abs(f_ann[i]) < exit_thr
            maxed = (i - entry_i) >= max_hold
            if normalized or maxed or i == n - 1:
                price_ret = side * (px[i] / entry_px - 1.0)
                trades.append(float(price_ret + carry - roundtrip))
                side = 0
    return trades


def funding_fade_backtest(closes: pd.DataFrame, funding: pd.DataFrame, entry_thr: float = 0.30,
                          exit_thr: float = 0.10, max_hold: int = 168,
                          fee_pct: float = 0.00045) -> BacktestResult:
    """Poole les trades funding-fade de tous les symboles → un BacktestResult."""
    closes = closes.reset_index(drop=True)
    funding = funding.reset_index(drop=True)
    all_trades: List[float] = []
    for sym in closes.columns:
        if sym not in funding.columns:
            continue
        all_trades += _fade_symbol(
            closes[sym].to_numpy(float), funding[sym].to_numpy(float),
            entry_thr, exit_thr, max_hold, fee_pct,
        )
    return _result_from_periods(all_trades, f"panel({closes.shape[1]})",
                                f"fund_fade(e{entry_thr:g},x{exit_thr:g},h{max_hold})")


class FundingFadeWalkForward:
    """Walk-forward OOS du funding fade à seuil (même gate que le harnais)."""

    def __init__(self, fee_pct: float = 0.00045) -> None:
        self._fee = fee_pct

    def evaluate(self, closes: pd.DataFrame, funding: pd.DataFrame, combos: List[dict],
                 n_folds: int = 5, train_frac: float = 0.6, select_metric: str = "pnl",
                 min_trades_train: int = 8) -> WalkForwardReport:
        closes = closes.reset_index(drop=True)
        funding = funding.reset_index(drop=True)
        rep = WalkForwardReport(symbol=f"panel({closes.shape[1]})",
                                strategy="cs_funding_fade_thr", n_folds=n_folds, fee_pct=self._fee)

        best_full, best_full_params = -math.inf, {}
        for params in combos:
            r = funding_fade_backtest(closes, funding, fee_pct=self._fee, **params)
            if r.total_pnl > best_full:
                best_full, best_full_params = r.total_pnl, params
        rep.in_sample_best_pnl = best_full
        rep.in_sample_best_params = best_full_params

        block = len(closes) // n_folds
        for kf in range(n_folds):
            lo, hi = kf * block, (kf + 1) * block
            split = lo + int((hi - lo) * train_frac)
            if (split - lo) < 50 or (hi - split) < 50:
                continue
            c_tr, f_tr = closes.iloc[lo:split], funding.iloc[lo:split]
            c_te, f_te = closes.iloc[split:hi], funding.iloc[split:hi]
            best_params, best_score = None, -math.inf
            for params in combos:
                tr = funding_fade_backtest(c_tr, f_tr, fee_pct=self._fee, **params)
                if tr.nb_trades < min_trades_train:
                    continue
                score = tr.profit_factor if select_metric == "pf" else tr.total_pnl
                if score > best_score:
                    best_score, best_params = score, params
            if best_params is None:
                continue
            oos = funding_fade_backtest(c_te, f_te, fee_pct=self._fee, **best_params)
            train_best = funding_fade_backtest(c_tr, f_tr, fee_pct=self._fee, **best_params)
            rep.folds.append(FoldResult(
                index=kf, train_n=(split - lo), test_n=(hi - split), best_params=best_params,
                train_pnl=train_best.total_pnl, oos_pnl=oos.total_pnl, oos_trades=oos.nb_trades,
                oos_winrate=oos.winrate, oos_pf=oos.profit_factor,
            ))

        gate_report(rep, n_folds)
        return rep

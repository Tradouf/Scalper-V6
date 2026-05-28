"""
Features de marché pour la détection de régime.

Toutes les features sont calculées sur une fenêtre de bougies dont la dernière
correspond à l'instant t. Aucune feature ne lit au-delà de l'index t (test
no-leak en test_regime_features.py).

Conventions :
  - les entrées sont des np.ndarray de float
  - les retours sont des float (NaN si données insuffisantes)
"""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np


def _as_array(x: Sequence[float]) -> np.ndarray:
    return np.asarray(x, dtype=float)


def _wilder_smoothing(x: np.ndarray, period: int) -> np.ndarray:
    """Lissage de Wilder (RMA) : SMA initiale puis récurrence alpha=1/period.

    Tolère les NaN d'entrée : on cherche la première fenêtre de `period`
    valeurs finies consécutives pour initialiser, et on skipe les NaN ensuite
    en réutilisant la dernière valeur lissée valide.

    Returns : array de même longueur, NaN avant l'init.
    """
    n = len(x)
    out = np.full(n, np.nan)
    if n < period:
        return out
    # Cherche la 1re fenêtre de `period` valeurs finies consécutives
    init_idx = None
    for start in range(0, n - period + 1):
        window = x[start : start + period]
        if np.all(np.isfinite(window)):
            init_idx = start + period - 1
            out[init_idx] = float(np.mean(window))
            break
    if init_idx is None:
        return out
    # Récurrence : x[i] NaN → on garde out[i-1]
    for i in range(init_idx + 1, n):
        prev = out[i - 1]
        if not np.isfinite(x[i]):
            out[i] = prev
        else:
            out[i] = (prev * (period - 1) + x[i]) / period
    return out


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> np.ndarray:
    """True Range = max(H-L, |H-Cprev|, |L-Cprev|). Renvoie array de longueur N.
    TR[0] = H[0]-L[0] (pas de close précédent)."""
    h = _as_array(highs)
    l = _as_array(lows)
    c = _as_array(closes)
    n = len(h)
    tr = np.zeros(n)
    if n == 0:
        return tr
    tr[0] = h[0] - l[0]
    if n > 1:
        prev_c = c[:-1]
        tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - prev_c), np.abs(l[1:] - prev_c)])
    return tr


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> Optional[float]:
    """ATR (Wilder) sur la fenêtre. Renvoie ATR(t=last) ou None si insuffisant."""
    tr = true_range(highs, lows, closes)
    if len(tr) < period:
        return None
    smoothed = _wilder_smoothing(tr, period)
    val = smoothed[-1]
    return float(val) if np.isfinite(val) else None


def adx(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> Optional[float]:
    """ADX (Average Directional Index) Wilder. Mesure la force de la tendance,
    indépendamment du sens. ADX > 25 = tendance forte, < 20 = range.

    Returns :
        float entre 0 et 100, ou None si données insuffisantes.
    """
    h = _as_array(highs)
    l = _as_array(lows)
    c = _as_array(closes)
    n = len(h)
    if n < 2 * period + 1:
        return None

    # Mouvements directionnels +DM, -DM
    up_move = np.diff(h)
    down_move = -np.diff(l)
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    # True Range décalé (on aligne avec dm)
    tr = true_range(h, l, c)[1:]  # même longueur que plus_dm

    # Wilder smoothing
    atr_sm = _wilder_smoothing(tr, period)
    plus_di = 100.0 * _wilder_smoothing(plus_dm, period) / np.maximum(atr_sm, 1e-12)
    minus_di = 100.0 * _wilder_smoothing(minus_dm, period) / np.maximum(atr_sm, 1e-12)

    # DX
    di_sum = plus_di + minus_di
    dx = 100.0 * np.abs(plus_di - minus_di) / np.maximum(di_sum, 1e-12)

    # ADX = Wilder smoothing du DX
    adx_arr = _wilder_smoothing(dx, period)
    val = adx_arr[-1]
    return float(val) if np.isfinite(val) else None


def hurst_rs(prices: Sequence[float], min_chunk: int = 8) -> Optional[float]:
    """Exposant de Hurst par analyse R/S.

    H ≈ 0.5  : marche aléatoire (random walk)
    H > 0.5  : persistance (trend follower)
    H < 0.5  : anti-persistance (mean reverting)

    Returns :
        float entre 0 et 1 (théoriquement [0,1] mais le calcul peut sortir
        légèrement en dehors sur petits échantillons), ou None si données
        insuffisantes.

    Méthode : R/S sur chunks de taille croissante (puissances de 2). On régresse
    log(R/S) sur log(n) ; H = pente.
    """
    p = _as_array(prices)
    if len(p) < min_chunk * 2:
        return None
    # Travail sur log-returns (stationnaire). Le R/S sur le niveau marche aussi
    # mais log-returns est plus standard.
    rets = np.diff(np.log(np.maximum(p, 1e-12)))
    n = len(rets)
    if n < min_chunk * 2:
        return None

    # Tailles de chunk : puissances de 2 jusqu'à n/2 inclus
    chunk_sizes = []
    k = min_chunk
    while k <= n // 2:
        chunk_sizes.append(k)
        k *= 2
    if len(chunk_sizes) < 2:
        return None

    rs_values = []
    for size in chunk_sizes:
        n_chunks = n // size
        rs_per_chunk = []
        for i in range(n_chunks):
            chunk = rets[i * size : (i + 1) * size]
            mean = chunk.mean()
            std = chunk.std(ddof=0)
            if std < 1e-12:
                continue
            centered = chunk - mean
            z = np.cumsum(centered)
            r = z.max() - z.min()
            rs_per_chunk.append(r / std)
        if rs_per_chunk:
            rs_values.append(np.mean(rs_per_chunk))
        else:
            rs_values.append(np.nan)

    # Filtre les NaN
    sizes_arr = np.array(chunk_sizes, dtype=float)
    rs_arr = np.array(rs_values, dtype=float)
    mask = np.isfinite(rs_arr) & (rs_arr > 0)
    if mask.sum() < 2:
        return None
    log_n = np.log(sizes_arr[mask])
    log_rs = np.log(rs_arr[mask])
    # Régression OLS
    slope, _ = np.polyfit(log_n, log_rs, 1)
    return float(slope)


def realized_vol(prices: Sequence[float], window: int = 24) -> Optional[float]:
    """Volatilité réalisée = std des log-returns sur la dernière fenêtre.

    Pas annualisée (échelle relative). Useful for percentile.
    """
    p = _as_array(prices)
    if len(p) < window + 1:
        return None
    rets = np.diff(np.log(np.maximum(p[-window - 1:], 1e-12)))
    if len(rets) < 2:
        return None
    return float(rets.std(ddof=1))


def vol_percentile(prices: Sequence[float], window: int = 24, lookback: int = 100) -> Optional[float]:
    """Percentile de la vol courante dans l'histogramme glissant des vols
    précédentes (sur lookback bars).

    Returns :
        float ∈ [0, 1], ou None si données insuffisantes.

    Implémentation no-leak : on calcule rolling_vol(t) avec window bars finissant
    à t, puis on prend les `lookback` valeurs jusqu'à t inclus. Le percentile
    de la dernière valeur dans cette série.
    """
    p = _as_array(prices)
    needed = window + lookback
    if len(p) < needed:
        return None
    # rolling vol : pour chaque t ∈ [window, len-1], std des window log-returns
    # finissant à t.
    log_p = np.log(np.maximum(p, 1e-12))
    rets = np.diff(log_p)  # len = N-1
    if len(rets) < window:
        return None
    # Vol[i] = std(rets[i-window+1 : i+1]) pour i ∈ [window-1, len-1]
    n_rets = len(rets)
    vols = np.full(n_rets, np.nan)
    cum = np.cumsum(rets)
    cum_sq = np.cumsum(rets ** 2)
    for i in range(window - 1, n_rets):
        s = cum[i] - (cum[i - window] if i >= window else 0.0)
        sq = cum_sq[i] - (cum_sq[i - window] if i >= window else 0.0)
        mean = s / window
        var = sq / window - mean ** 2
        if var <= 0:
            continue
        vols[i] = np.sqrt(var)
    valid = vols[np.isfinite(vols)]
    if len(valid) < 5:
        return None
    current = vols[-1]
    if not np.isfinite(current):
        return None
    # percentile = fraction des valeurs précédentes (≤ lookback) inférieures
    recent = valid[-lookback:] if len(valid) > lookback else valid
    rank = float((recent < current).sum() + 0.5 * (recent == current).sum()) / len(recent)
    return rank


def autocorr_lag1(prices: Sequence[float], window: int = 50) -> Optional[float]:
    """Autocorrélation lag-1 des log-returns sur la dernière fenêtre.

    > 0 : momentum (return positif suit return positif)
    < 0 : mean reversion
    ~ 0 : random walk

    Returns : float ∈ [-1, 1], ou None si insuffisant.
    """
    p = _as_array(prices)
    if len(p) < window + 2:
        return None
    rets = np.diff(np.log(np.maximum(p[-window - 1:], 1e-12)))
    if len(rets) < 3:
        return None
    a = rets[:-1]
    b = rets[1:]
    var_a = float(a.var(ddof=0))
    if var_a < 1e-12:
        return None
    cov = float(((a - a.mean()) * (b - b.mean())).mean())
    return cov / var_a


def returns_slope_zscore(prices: Sequence[float], window: int = 48) -> Optional[float]:
    """Pente de log-prix régressée sur le temps, normalisée par std des returns.

    z = slope_per_bar / std_returns
    Plus grand en valeur absolue = trend plus fort.
    Signé : > 0 = trend haussier, < 0 = trend baissier.

    Returns : float (potentiellement non borné), ou None.
    """
    p = _as_array(prices)
    if len(p) < window + 2:
        return None
    log_p = np.log(np.maximum(p[-window:], 1e-12))
    x = np.arange(window, dtype=float)
    slope, _ = np.polyfit(x, log_p, 1)
    rets = np.diff(log_p)
    sd = rets.std(ddof=1)
    if sd < 1e-12:
        return None
    return float(slope / sd)


def supertrend(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 10,
    multiplier: float = 3.0,
) -> Optional[tuple]:
    """Supertrend indicator (ATR-based trend filter).

    Calcul itératif standard :
      median       = (high + low) / 2
      upper_band   = median + multiplier × ATR(period)
      lower_band   = median - multiplier × ATR(period)
      if close > prev_supertrend : supertrend = max(lower_band, prev_supertrend)  direction=+1
      if close < prev_supertrend : supertrend = min(upper_band, prev_supertrend)  direction=-1

    Returns :
        (st_value_at_last_bar, direction_at_last_bar) ou None si insuffisant.
        direction ∈ {+1, -1}. st_value = niveau de stop trailing courant.

    Garantit no-leak : utilise uniquement les bars jusqu'à l'index t inclus.
    """
    h = _as_array(highs)
    l = _as_array(lows)
    c = _as_array(closes)
    n = len(c)
    if n < period + 2:
        return None

    # ATR (Wilder)
    tr = true_range(h, l, c)
    atr_arr = _wilder_smoothing(tr, period)

    # Premiers index avec ATR valide
    valid_idx = None
    for i in range(n):
        if np.isfinite(atr_arr[i]):
            valid_idx = i
            break
    if valid_idx is None or valid_idx >= n - 1:
        return None

    median = (h + l) / 2.0
    upper = median + multiplier * atr_arr
    lower = median - multiplier * atr_arr

    # État initial : on choisit la direction selon close vs median
    st = np.full(n, np.nan)
    direction = np.full(n, 0, dtype=int)
    st[valid_idx] = lower[valid_idx]  # default trend up
    direction[valid_idx] = 1 if c[valid_idx] >= median[valid_idx] else -1
    if direction[valid_idx] == -1:
        st[valid_idx] = upper[valid_idx]

    for i in range(valid_idx + 1, n):
        if not np.isfinite(upper[i]) or not np.isfinite(lower[i]):
            st[i] = st[i - 1]
            direction[i] = direction[i - 1]
            continue
        prev_dir = direction[i - 1]
        prev_st = st[i - 1]
        # Trend continuation by default
        if prev_dir == 1:
            # En trend haussier, st suit le lower_band en montant
            new_st = max(lower[i], prev_st) if prev_st <= c[i - 1] else lower[i]
            if c[i] < new_st:
                # Flip vers trend baissier
                direction[i] = -1
                st[i] = upper[i]
            else:
                direction[i] = 1
                st[i] = new_st
        else:  # prev_dir == -1
            new_st = min(upper[i], prev_st) if prev_st >= c[i - 1] else upper[i]
            if c[i] > new_st:
                direction[i] = 1
                st[i] = lower[i]
            else:
                direction[i] = -1
                st[i] = new_st

    last_st = float(st[-1])
    last_dir = int(direction[-1])
    if not np.isfinite(last_st) or last_dir == 0:
        return None
    return last_st, last_dir


def supertrend_with_history(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 10,
    multiplier: float = 3.0,
) -> Optional[tuple]:
    """Variante qui retourne l'historique complet (st_arr, dir_arr) en plus
    du dernier point. Utile pour détecter un FLIP à l'instant t :
        flip = (dir[-1] != dir[-2])

    Returns : (st_arr, dir_arr, last_st, last_dir) ou None.
    """
    res = supertrend(highs, lows, closes, period, multiplier)
    if res is None:
        return None
    # On refait le calcul en exposant les arrays (légère duplication mais clarté)
    h = _as_array(highs)
    l = _as_array(lows)
    c = _as_array(closes)
    n = len(c)
    tr = true_range(h, l, c)
    atr_arr = _wilder_smoothing(tr, period)
    median = (h + l) / 2.0
    upper = median + multiplier * atr_arr
    lower = median - multiplier * atr_arr
    valid_idx = None
    for i in range(n):
        if np.isfinite(atr_arr[i]):
            valid_idx = i
            break
    if valid_idx is None:
        return None
    st = np.full(n, np.nan)
    direction = np.full(n, 0, dtype=int)
    st[valid_idx] = lower[valid_idx]
    direction[valid_idx] = 1 if c[valid_idx] >= median[valid_idx] else -1
    if direction[valid_idx] == -1:
        st[valid_idx] = upper[valid_idx]
    for i in range(valid_idx + 1, n):
        if not np.isfinite(upper[i]) or not np.isfinite(lower[i]):
            st[i] = st[i - 1]
            direction[i] = direction[i - 1]
            continue
        prev_dir = direction[i - 1]
        prev_st = st[i - 1]
        if prev_dir == 1:
            new_st = max(lower[i], prev_st) if prev_st <= c[i - 1] else lower[i]
            if c[i] < new_st:
                direction[i] = -1
                st[i] = upper[i]
            else:
                direction[i] = 1
                st[i] = new_st
        else:
            new_st = min(upper[i], prev_st) if prev_st >= c[i - 1] else upper[i]
            if c[i] > new_st:
                direction[i] = 1
                st[i] = lower[i]
            else:
                direction[i] = -1
                st[i] = new_st
    return st, direction, float(st[-1]), int(direction[-1])

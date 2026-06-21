"""
Pool de stratégies paramétrées sur OHLCV (recherche rotation 2026-06-21).

Chaque stratégie = fonction(df) → Series de POSITION cible ∈ [-1,+1] (direction × conviction),
NON décalée (le moteur de backtest applique le shift causal). Familles : trend (MA, breakout,
ts-mom), mean-reversion (RSI, Bollinger, z-score), chandelles brutes (range, momentum n-barres),
+ variantes de SORTIE (reverse implicite, time-stop, trailing). Tout passe par `strat_returns`
qui applique le shift causal, le sizing optionnel et les FRAIS sur le turnover.

But : alimenter run_rotation.py (persistance + méta-rotation). Aucune dépendance au reste du repo
hormis pandas/numpy → réutilisable et testable isolément.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

FEE = 0.00045  # taker/côté HL


# ─── Indicateurs ────────────────────────────────────────────────────────────────

def _rsi(close: pd.Series, n: int) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50.0)


def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


# ─── Familles de signaux (position ∈ [-1,1], NON décalée) ───────────────────────

def s_ma_cross(df, fast, slow):
    c = df["close"]
    return np.sign(c.rolling(fast).mean() - c.rolling(slow).mean()).fillna(0.0)


def s_breakout(df, n):
    c = df["close"]
    hi, lo = c.rolling(n).max(), c.rolling(n).min()
    pos = pd.Series(0.0, index=c.index)
    pos[c >= hi] = 1.0
    pos[c <= lo] = -1.0
    return pos.replace(0.0, np.nan).ffill().fillna(0.0)   # Donchian persistant


def s_tsmom(df, lb):
    c = df["close"]
    return np.sign(c / c.shift(lb) - 1.0).fillna(0.0)


def s_rsi_mr(df, n, lo, hi):
    r = _rsi(df["close"], n)
    pos = pd.Series(np.nan, index=df.index)
    pos[r < lo] = 1.0          # survendu → long
    pos[r > hi] = -1.0         # suracheté → short
    pos[(r > 50 - 2) & (r < 50 + 2)] = 0.0   # sortie au retour vers le neutre
    return pos.ffill().fillna(0.0)


def s_bbands_mr(df, n, k):
    c = df["close"]
    m, sd = c.rolling(n).mean(), c.rolling(n).std()
    z = (c - m) / sd.replace(0, np.nan)
    pos = pd.Series(np.nan, index=df.index)
    pos[z < -k] = 1.0
    pos[z > k] = -1.0
    pos[z.abs() < 0.3] = 0.0
    return pos.ffill().fillna(0.0)


def s_zscore_fade(df, n):
    c = df["close"]
    m, sd = c.rolling(n).mean(), c.rolling(n).std()
    z = (c - m) / sd.replace(0, np.nan)
    return (-np.tanh(z)).fillna(0.0)        # fade continu borné


def s_nbar_mom(df, n):
    c = df["close"]
    return np.sign(c - c.shift(n)).fillna(0.0)


def s_range_expansion(df, n):
    # Chandelle brute : si la clôture est dans le haut de la range des n dernières → long.
    c, hi, lo = df["close"], df["high"].rolling(n).max(), df["low"].rolling(n).min()
    pct = (c - lo) / (hi - lo).replace(0, np.nan)
    return ((pct - 0.5) * 2).clip(-1, 1).fillna(0.0)   # position dans le canal, ∈[-1,1]


def s_accel(df, n):
    # Accélération du prix (momentum du momentum) sur chandelles.
    c = df["close"]
    mom = c.pct_change(n)
    return np.sign(mom - mom.shift(n)).fillna(0.0)


# ─── Volatilité ─────────────────────────────────────────────────────────────────

def s_keltner(df, n, mult):
    """Breakout de canal de Keltner (EMA ± mult·ATR) = trend."""
    c = df["close"]
    mid = c.ewm(span=n, adjust=False).mean()
    atr = _atr(df, n)
    pos = pd.Series(np.nan, index=c.index)
    pos[c > mid + mult * atr] = 1.0
    pos[c < mid - mult * atr] = -1.0
    return pos.ffill().fillna(0.0)


def s_atr_channel_break(df, n, mult):
    """Cassure clôture > close[-1] + mult·ATR → trend (momentum de volatilité)."""
    c = df["close"]
    atr = _atr(df, n)
    up = c > (c.shift(1) + mult * atr)
    dn = c < (c.shift(1) - mult * atr)
    pos = pd.Series(np.nan, index=c.index)
    pos[up] = 1.0; pos[dn] = -1.0
    return pos.ffill().fillna(0.0)


def s_vol_regime_switch(df, n, lb):
    """Régime de vol : en BASSE vol → suit la tendance (tsmom) ; en HAUTE vol → fade. La vol
    réalisée vs sa médiane glissante définit le régime (commute la stratégie selon la vol)."""
    c = df["close"]
    rv = c.pct_change().rolling(n).std()
    hi = rv > rv.rolling(4 * n).median()
    trend = np.sign(c / c.shift(lb) - 1.0)
    m, sd = c.rolling(lb).mean(), c.rolling(lb).std()
    fade = -np.tanh(((c - m) / sd.replace(0, np.nan)))
    return trend.where(~hi, fade).fillna(0.0)


# ─── Volume ─────────────────────────────────────────────────────────────────────

def s_obv_trend(df, n):
    """Tendance de l'OBV (On-Balance Volume) : signe de la pente sur n barres."""
    c, v = df["close"], df["volume"]
    obv = (np.sign(c.diff()).fillna(0.0) * v).cumsum()
    return np.sign(obv - obv.shift(n)).fillna(0.0)


def s_vol_weighted_mom(df, n):
    """Momentum pondéré par le volume : somme(ret·volume) sur n → signe."""
    c, v = df["close"], df["volume"]
    sig = (c.pct_change().fillna(0.0) * v).rolling(n).sum()
    return np.sign(sig).fillna(0.0)


def s_volume_spike_reversal(df, n):
    """Pic de volume + grosse bougie → fade (épuisement) : volume > 2×médiane et |ret| grand
    → position contre la bougie."""
    c, v = df["close"], df["volume"]
    spike = v > 2.0 * v.rolling(n).median()
    ret = c.pct_change()
    pos = pd.Series(np.nan, index=c.index)
    pos[spike & (ret > 0)] = -1.0
    pos[spike & (ret < 0)] = 1.0
    return pos.ffill(limit=3).fillna(0.0)   # fade court (3 barres)


# ─── Chandelles explicites ────────────────────────────────────────────────────────

def s_engulfing(df):
    """Engulfing haussier/baissier → direction (3 barres de tenue)."""
    o, c = df["open"], df["close"]
    body = c - o
    prev = body.shift(1)
    bull = (body > 0) & (prev < 0) & (c > o.shift(1)) & (o < c.shift(1))
    bear = (body < 0) & (prev > 0) & (c < o.shift(1)) & (o > c.shift(1))
    pos = pd.Series(np.nan, index=c.index)
    pos[bull] = 1.0; pos[bear] = -1.0
    return pos.ffill(limit=3).fillna(0.0)


def s_pinbar(df):
    """Mèche de rejet : longue mèche basse → long (rejet du bas) ; longue mèche haute → short."""
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    rng = (h - l).replace(0, np.nan)
    upper = (h - np.maximum(o, c)) / rng
    lower = (np.minimum(o, c) - l) / rng
    pos = pd.Series(np.nan, index=c.index)
    pos[lower > 0.6] = 1.0
    pos[upper > 0.6] = -1.0
    return pos.ffill(limit=3).fillna(0.0)


def s_inside_break(df, n):
    """Inside bar (range contenue) puis cassure → trend dans le sens de la cassure."""
    h, l, c = df["high"], df["low"], df["close"]
    inside = (h < h.shift(1)) & (l > l.shift(1))
    ref_hi = h.where(inside).ffill()
    ref_lo = l.where(inside).ffill()
    pos = pd.Series(np.nan, index=c.index)
    pos[c > ref_hi] = 1.0
    pos[c < ref_lo] = -1.0
    return pos.ffill().fillna(0.0)


# ─── Oscillateurs supplémentaires ─────────────────────────────────────────────────

def s_macd(df, f, s, sig):
    c = df["close"]
    macd = c.ewm(span=f, adjust=False).mean() - c.ewm(span=s, adjust=False).mean()
    return np.sign(macd - macd.ewm(span=sig, adjust=False).mean()).fillna(0.0)


def s_williams_r(df, n, lo=-80, hi=-20):
    h, l, c = df["high"].rolling(n).max(), df["low"].rolling(n).min(), df["close"]
    wr = -100 * (h - c) / (h - l).replace(0, np.nan)
    pos = pd.Series(np.nan, index=c.index)
    pos[wr < lo] = 1.0          # survendu
    pos[wr > hi] = -1.0
    pos[(wr > -55) & (wr < -45)] = 0.0
    return pos.ffill().fillna(0.0)


def s_cci(df, n):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    sma = tp.rolling(n).mean()
    md = (tp - sma).abs().rolling(n).mean()
    cci = (tp - sma) / (0.015 * md.replace(0, np.nan))
    return np.sign(cci).clip(-1, 1).fillna(0.0)   # CCI>0 trend haussier


# ─── Exits (transforment une position en position-tenue selon une règle de sortie) ──

def apply_time_stop(pos: pd.Series, max_hold: int) -> pd.Series:
    """Force flat après `max_hold` barres dans la même position non-nulle (puis ré-entrée
    possible au signal suivant). Approxime un exit temporel."""
    out = pos.copy().values.astype(float)
    held = 0
    for i in range(1, len(out)):
        if out[i] != 0 and out[i] == np.sign(out[i - 1]) * abs(out[i]) and np.sign(out[i]) == np.sign(out[i - 1]) and out[i - 1] != 0:
            held += 1
            if held >= max_hold:
                out[i] = 0.0
        else:
            held = 0
    return pd.Series(out, index=pos.index)


def apply_trail(df: pd.DataFrame, pos: pd.Series, atr_n: int, mult: float) -> pd.Series:
    """Trailing stop ATR : coupe la position si le prix recule de mult×ATR depuis l'extrême
    favorable atteint pendant la détention. Causal (utilise close/ATR de la barre)."""
    atr = _atr(df, atr_n).values
    c = df["close"].values
    p = pos.values.astype(float)
    out = p.copy()
    extreme = None
    cur = 0.0
    for i in range(len(out)):
        sig = p[i]
        if cur == 0.0:
            cur = sig
            extreme = c[i]
            out[i] = cur
            continue
        # mise à jour de l'extrême favorable
        if cur > 0:
            extreme = max(extreme, c[i])
            if c[i] <= extreme - mult * atr[i]:
                cur = 0.0
        elif cur < 0:
            extreme = min(extreme, c[i])
            if c[i] >= extreme + mult * atr[i]:
                cur = 0.0
        # ré-aligne sur le signal s'il change de direction
        if sig != 0 and np.sign(sig) != np.sign(cur) and cur != 0.0:
            cur = sig
            extreme = c[i]
        elif cur != 0.0 and sig == 0:
            pass  # garde la position tant que le trail n'a pas coupé
        elif cur == 0.0 and sig != 0:
            cur = sig
            extreme = c[i]
        out[i] = cur
    return pd.Series(out, index=pos.index)


# ─── Retours nets ───────────────────────────────────────────────────────────────

def strat_returns(df: pd.DataFrame, pos: pd.Series, fee: float = FEE) -> np.ndarray:
    """Position causale (décalée d'une barre) → rendement net quotidien (frais sur turnover)."""
    ret = df["close"].astype(float).pct_change().fillna(0.0).values
    p = pos.shift(1).fillna(0.0).values
    turn = np.abs(np.diff(p, prepend=0.0))
    return p * ret - turn * fee


def build_pool(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Construit un dictionnaire {nom: position} couvrant trend / MR / chandelles / sorties."""
    pool: dict[str, pd.Series] = {}
    # Trend
    for f, s in [(5, 20), (10, 50), (20, 100), (50, 200)]:
        pool[f"ma_{f}_{s}"] = s_ma_cross(df, f, s)
    for n in [20, 55, 100]:
        pool[f"brk_{n}"] = s_breakout(df, n)
    for lb in [10, 20, 30, 60, 120]:
        pool[f"tsmom_{lb}"] = s_tsmom(df, lb)
    for n in [5, 15, 40]:
        pool[f"nbar_{n}"] = s_nbar_mom(df, n)
    pool["accel_20"] = s_accel(df, 20)
    # Mean-reversion
    for n, lo, hi in [(14, 30, 70), (7, 25, 75), (21, 35, 65)]:
        pool[f"rsi_{n}_{lo}_{hi}"] = s_rsi_mr(df, n, lo, hi)
    for n, k in [(20, 2.0), (20, 1.5), (50, 2.0)]:
        pool[f"bb_{n}_{k}"] = s_bbands_mr(df, n, k)
    for n in [10, 30]:
        pool[f"zfade_{n}"] = s_zscore_fade(df, n)
    # Chandelles brutes
    for n in [10, 30]:
        pool[f"rng_{n}"] = s_range_expansion(df, n)
    # Variantes de SORTIE sur un trend de référence (tsmom_30)
    base = s_tsmom(df, 30)
    pool["tsmom_30_tstop20"] = apply_time_stop(base, 20)
    pool["tsmom_30_trail3"] = apply_trail(df, base, 14, 3.0)
    return pool


def build_pool_deep(df: pd.DataFrame) -> dict[str, pd.Series]:
    """Pool ÉLARGI (#4 2026-06-21) : base + volatilité + volume + chandelles explicites +
    oscillateurs. Plus de familles décorrélées → diversification plus profonde."""
    pool = build_pool(df)
    # Volatilité
    for n, m in [(20, 1.5), (20, 2.5), (50, 2.0)]:
        pool[f"kelt_{n}_{m}"] = s_keltner(df, n, m)
    pool["atrbrk_14_1.5"] = s_atr_channel_break(df, 14, 1.5)
    pool["volregime_20_30"] = s_vol_regime_switch(df, 20, 30)
    # Volume
    for n in [20, 50]:
        pool[f"obv_{n}"] = s_obv_trend(df, n)
    pool["vwmom_20"] = s_vol_weighted_mom(df, 20)
    pool["volspike_30"] = s_volume_spike_reversal(df, 30)
    # Chandelles explicites
    pool["engulf"] = s_engulfing(df)
    pool["pinbar"] = s_pinbar(df)
    pool["inside_break"] = s_inside_break(df, 20)
    # Oscillateurs
    pool["macd_12_26_9"] = s_macd(df, 12, 26, 9)
    pool["wr_14"] = s_williams_r(df, 14)
    pool["cci_20"] = s_cci(df, 20)
    return pool

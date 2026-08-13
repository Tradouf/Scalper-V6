"""
Indicateurs purs — SPEC §3 (règle anti-repaint) et §4.

Deux invariants tenus par tout ce fichier :

1. **Aucune I/O.** Ces fonctions prennent des listes de bougies et rendent des
   listes de nombres. Elles sont donc rejouables à l'identique, ce qui est la
   condition pour que le backtest §9 mesure la même chose que le live.

2. **Aucun regard vers l'avant.** `f(candles)[i]` ne dépend que de
   `candles[:i+1]`. Un indicateur qui violerait ça rendrait un backtest
   flatteur et un live catastrophique — c'est le mode d'échec classique.
   `tests/test_confluence.py::test_anti_repaint_*` vérifie l'invariant en
   rejouant l'historique bougie par bougie.

Convention de bougie (identique au reste du repo, cf. simplebot/data.py) :
    {"ts": int ms (ouverture), "open", "high", "low", "close", "volume"}

`ts` est l'OUVERTURE de la bougie. Une bougie n'est clôturée qu'à
`ts + interval_ms`. Tout le module raisonne là-dessus.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

Candle = Dict[str, float]

INTERVAL_MS: Dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


# ── Hygiène de séries ────────────────────────────────────────────────────────

def closed(candles: Sequence[Candle], interval: str, now_ms: int) -> List[Candle]:
    """Ne garde que les bougies dont la fenêtre est terminée à `now_ms`.

    C'est LE point de passage obligé de la règle anti-repaint §3 : aucune
    couche ne doit voir la bougie en cours. `now_ms` est toujours injecté
    (jamais `time.time()` ici) pour que le backtest et le live partagent
    exactement le même code.
    """
    step = INTERVAL_MS[interval]
    return [c for c in candles if int(c["ts"]) + step <= now_ms]


def sort_dedup(candles: Sequence[Candle]) -> List[Candle]:
    """Trie par ts et garde la DERNIÈRE occurrence de chaque ts.

    L'API Hyperliquid renvoie parfois la même bougie deux fois sur des fenêtres
    qui se chevauchent (le loader §9 pagine en reculant) ; la dernière reçue
    est la plus à jour.
    """
    by_ts: Dict[int, Candle] = {}
    for c in candles:
        by_ts[int(c["ts"])] = c
    return [by_ts[t] for t in sorted(by_ts)]


def find_gaps(candles: Sequence[Candle], interval: str) -> List[tuple]:
    """Trous de la série : [(ts_avant, ts_après, n_bougies_manquantes), ...].

    Hyperliquid a des interruptions réelles (maintenance, listing tardif). Le
    loader doit les signaler plutôt que de les combler : une bougie inventée
    fausse tous les indicateurs qui la traversent.
    """
    step = INTERVAL_MS[interval]
    out = []
    for prev, cur in zip(candles, candles[1:]):
        delta = int(cur["ts"]) - int(prev["ts"])
        if delta > step:
            out.append((int(prev["ts"]), int(cur["ts"]), delta // step - 1))
    return out


def aggregate(candles: Sequence[Candle], src: str, dst: str) -> List[Candle]:
    """Agrège un TF vers un TF supérieur (§3 : construire les TF supérieurs par
    agrégation des 1m si le feed natif est indisponible).

    Une bougie agrégée n'est émise que si elle est COMPLÈTE (toutes ses
    sous-bougies présentes) : une bougie daily construite sur 18 h de données
    aurait un high/low faux et polluerait le biais.
    """
    src_ms, dst_ms = INTERVAL_MS[src], INTERVAL_MS[dst]
    if dst_ms % src_ms != 0:
        raise ValueError(f"{dst} n'est pas un multiple de {src}")
    expected = dst_ms // src_ms

    buckets: Dict[int, List[Candle]] = {}
    for c in candles:
        buckets.setdefault((int(c["ts"]) // dst_ms) * dst_ms, []).append(c)

    out: List[Candle] = []
    for bucket_ts in sorted(buckets):
        group = sorted(buckets[bucket_ts], key=lambda c: int(c["ts"]))
        if len(group) != expected:
            continue                                  # bougie incomplète : rejetée
        out.append({
            "ts": bucket_ts,
            "open": float(group[0]["open"]),
            "high": max(float(c["high"]) for c in group),
            "low": min(float(c["low"]) for c in group),
            "close": float(group[-1]["close"]),
            "volume": sum(float(c.get("volume", 0.0)) for c in group),
        })
    return out


def anomalies(candles: Sequence[Candle], max_move_pct: float = 0.5) -> List[int]:
    """Index des bougies incohérentes : OHLC impossible, prix ≤ 0, ou mouvement
    close-à-close délirant (défaut 50 %, borne de plausibilité pour du BTC sur
    n'importe quel TF ≤ 1d). Sert au contrôle d'intégrité du loader, PAS à la
    décision."""
    bad = []
    for i, c in enumerate(candles):
        o, h, l, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
        if min(o, h, l, cl) <= 0 or h < l or h < max(o, cl) or l > min(o, cl):
            bad.append(i)
            continue
        if i and float(candles[i - 1]["close"]) > 0:
            if abs(cl / float(candles[i - 1]["close"]) - 1.0) > max_move_pct:
                bad.append(i)
    return bad


# ── Moyennes ─────────────────────────────────────────────────────────────────

def sma(values: Sequence[float], period: int) -> List[Optional[float]]:
    """Moyenne simple. `None` tant que la fenêtre n'est pas pleine — on ne
    renvoie jamais une valeur calculée sur moins de `period` points, ce qui
    ferait passer un filtre sur des données insuffisantes."""
    if period <= 0:
        raise ValueError("period doit être > 0")
    out: List[Optional[float]] = [None] * len(values)
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def sma_of_optional(values: Sequence[Optional[float]], period: int) -> List[Optional[float]]:
    """SMA d'une série qui commence par des `None` (typiquement la SMA d'un
    autre indicateur, comme la SMA_20 du BBW au §4.3).

    Rend `None` dès que la fenêtre contient un trou. Substituer 0.0 aux `None`
    — le réflexe tentant — écraserait la moyenne au démarrage et ferait
    apparaître une fausse expansion de volatilité sur les premières barres.
    """
    out: List[Optional[float]] = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        if any(v is None for v in window):
            continue
        out[i] = sum(window) / period          # type: ignore[arg-type]
    return out


def ema(values: Sequence[float], period: int) -> List[Optional[float]]:
    """EMA amorcée sur la SMA des `period` premiers points (amorçage standard,
    et surtout déterministe : une EMA amorcée sur la 1re valeur dépendrait du
    point de départ de la série, donc du moment où on lance le bot)."""
    if period <= 0:
        raise ValueError("period doit être > 0")
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1.0)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def rma(values: Sequence[float], period: int) -> List[Optional[float]]:
    """Moyenne de Wilder (lissage 1/period), utilisée par ATR et ADX. Amorçage
    sur la moyenne simple des `period` premiers points, comme Wilder."""
    if period <= 0:
        raise ValueError("period doit être > 0")
    out: List[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = (prev * (period - 1) + values[i]) / period
        out[i] = prev
    return out


# ── Volatilité ───────────────────────────────────────────────────────────────

def true_range(candles: Sequence[Candle]) -> List[float]:
    """TR de Wilder. La première barre n'a pas de close précédent : TR = h - l."""
    out = []
    for i, c in enumerate(candles):
        h, l = float(c["high"]), float(c["low"])
        if i == 0:
            out.append(h - l)
        else:
            pc = float(candles[i - 1]["close"])
            out.append(max(h - l, abs(h - pc), abs(l - pc)))
    return out


def atr(candles: Sequence[Candle], period: int = 14) -> List[Optional[float]]:
    return rma(true_range(candles), period)


def percentile_rank(window: Sequence[float], value: float) -> float:
    """Rang percentile de `value` dans `window`, en 0-100.

    Utilisé pour le filtre ATR §4.2. Convention : fraction des points
    STRICTEMENT inférieurs + la moitié des ex æquo — le traitement symétrique
    des égalités évite qu'une série plate (beaucoup d'ATR identiques) ne rende
    0 ou 100 selon l'inégalité choisie.
    """
    if not window:
        return float("nan")
    below = sum(1 for x in window if x < value)
    equal = sum(1 for x in window if x == value)
    return 100.0 * (below + 0.5 * equal) / len(window)


def bollinger_width(values: Sequence[float], period: int = 20,
                    n_std: float = 2.0) -> List[Optional[float]]:
    """Bollinger Band Width = (bande haute - bande basse) / bande médiane.

    Normalisé par la médiane : le BBW brut croît mécaniquement avec le prix, et
    la comparaison à sa propre SMA (§4.3) deviendrait un test de tendance du
    prix plutôt qu'un test d'expansion de volatilité.
    """
    if period <= 1:
        raise ValueError("period doit être > 1")
    mid = sma(values, period)
    out: List[Optional[float]] = [None] * len(values)
    for i in range(period - 1, len(values)):
        m = mid[i]
        if m is None or m == 0:
            continue
        window = values[i - period + 1:i + 1]
        var = sum((v - m) ** 2 for v in window) / period      # population, comme TradingView
        out[i] = (2.0 * n_std * math.sqrt(var)) / abs(m)
    return out


# ── Tendance ─────────────────────────────────────────────────────────────────

def adx(candles: Sequence[Candle], period: int = 14) -> List[Optional[float]]:
    """ADX de Wilder.

    Renvoie None tant que l'ADX n'est pas pleinement amorcé : il faut `period`
    barres pour ATR/DM puis `period` de plus pour lisser le DX. Un ADX rendu
    trop tôt vaut ~50 par construction et ouvrirait grand la porte TREND au
    démarrage du bot.
    """
    n = len(candles)
    out: List[Optional[float]] = [None] * n
    if n < 2:
        return out

    plus_dm, minus_dm = [0.0], [0.0]
    for i in range(1, n):
        up = float(candles[i]["high"]) - float(candles[i - 1]["high"])
        down = float(candles[i - 1]["low"]) - float(candles[i]["low"])
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)

    tr_s = rma(true_range(candles), period)
    plus_s = rma(plus_dm, period)
    minus_s = rma(minus_dm, period)

    dx: List[Optional[float]] = [None] * n
    for i in range(n):
        if tr_s[i] is None or not tr_s[i]:
            continue
        pdi = 100.0 * (plus_s[i] or 0.0) / tr_s[i]
        mdi = 100.0 * (minus_s[i] or 0.0) / tr_s[i]
        denom = pdi + mdi
        dx[i] = 100.0 * abs(pdi - mdi) / denom if denom else 0.0

    first = next((i for i, v in enumerate(dx) if v is not None), None)
    if first is None or n - first < period:
        return out
    smoothed = rma([v for v in dx[first:] if v is not None], period)
    for offset, v in enumerate(smoothed):
        out[first + offset] = v
    return out


def dmi(candles: Sequence[Candle], period: int = 14):
    """(+DI, -DI) lissés — exposés pour le diagnostic ; la décision §4.2 prend
    la direction sur les EMA, pas sur le DMI."""
    n = len(candles)
    plus_dm, minus_dm = [0.0], [0.0]
    for i in range(1, n):
        up = float(candles[i]["high"]) - float(candles[i - 1]["high"])
        down = float(candles[i - 1]["low"]) - float(candles[i]["low"])
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
    tr_s = rma(true_range(candles), period)
    plus_s, minus_s = rma(plus_dm, period), rma(minus_dm, period)
    pdi: List[Optional[float]] = [None] * n
    mdi: List[Optional[float]] = [None] * n
    for i in range(n):
        if tr_s[i]:
            pdi[i] = 100.0 * (plus_s[i] or 0.0) / tr_s[i]
            mdi[i] = 100.0 * (minus_s[i] or 0.0) / tr_s[i]
    return pdi, mdi


# ── VWAP de session ──────────────────────────────────────────────────────────

def session_vwap(candles: Sequence[Candle], session_ms: int = INTERVAL_MS["1d"],
                 ) -> List[Optional[float]]:
    """VWAP remis à zéro à chaque session UTC (§4.3 : « EMA_20(15m) ou VWAP
    session »).

    Le volume sert ici de pondération d'un niveau de prix, pas de filtre de
    décision — §11 interdit le volume EN DÉCISION, ce qui n'interdit pas le
    VWAP comme référence de prix. Si le volume est absent ou nul sur toute la
    session, on renvoie None plutôt qu'un VWAP dégénéré.
    """
    out: List[Optional[float]] = [None] * len(candles)
    cur_session = None
    pv = vol = 0.0
    for i, c in enumerate(candles):
        session = (int(c["ts"]) // session_ms) * session_ms
        if session != cur_session:
            cur_session, pv, vol = session, 0.0, 0.0
        typical = (float(c["high"]) + float(c["low"]) + float(c["close"])) / 3.0
        v = float(c.get("volume", 0.0) or 0.0)
        pv += typical * v
        vol += v
        out[i] = pv / vol if vol > 0 else None
    return out


# ── Mean-reversion (MeanReversionAgent, §2) ──────────────────────────────────

def zscore(values: Sequence[float], period: int) -> List[Optional[float]]:
    """Z-score glissant. None si l'écart-type de la fenêtre est nul (série
    plate : le z-score y est indéfini, pas infini)."""
    means = sma(values, period)
    out: List[Optional[float]] = [None] * len(values)
    for i in range(period - 1, len(values)):
        m = means[i]
        if m is None:
            continue
        window = values[i - period + 1:i + 1]
        var = sum((v - m) ** 2 for v in window) / period
        sd = math.sqrt(var)
        if sd > 0:
            out[i] = (values[i] - m) / sd
    return out


def half_life(values: Sequence[float]) -> Optional[float]:
    """Demi-vie de retour à la moyenne, via la régression d'Ornstein-Uhlenbeck
    Δy_t = a + b·y_{t-1} + ε :  half_life = -ln(2) / ln(1 + b).

    None si b ≥ 0 (pas de retour à la moyenne : la série diverge) — c'est un
    résultat, pas une erreur, et l'appelant doit le traiter comme un veto.
    """
    n = len(values)
    if n < 3:
        return None
    y = list(values[:-1])
    dy = [values[i + 1] - values[i] for i in range(n - 1)]
    my = sum(y) / len(y)
    mdy = sum(dy) / len(dy)
    sxx = sum((v - my) ** 2 for v in y)
    if sxx <= 0:
        return None
    b = sum((y[i] - my) * (dy[i] - mdy) for i in range(len(y))) / sxx
    if b >= 0 or (1.0 + b) <= 0:
        return None
    return -math.log(2.0) / math.log(1.0 + b)


def adf_statistic(values: Sequence[float], lags: int = 1) -> Optional[float]:
    """Statistique t d'un test Dickey-Fuller augmenté (constante, sans tendance).

    Implémentation locale : `statsmodels` n'est pas dans le venv du projet et
    n'y a pas sa place pour une seule régression. On régresse
    Δy_t = a + γ·y_{t-1} + Σ δ_i·Δy_{t-i} + ε par moindres carrés, et on rend
    t(γ). Plus la statistique est NÉGATIVE, plus la stationnarité est
    crédible ; comparer aux valeurs critiques de `ADF_CRITICAL`.

    Les valeurs critiques ne sont pas celles d'un t de Student — sous racine
    unitaire la distribution est non standard, d'où la table figée ci-dessous.
    """
    n = len(values)
    if n < lags + 5:
        return None
    dy = [values[i] - values[i - 1] for i in range(1, n)]

    rows, target = [], []
    for t in range(lags, len(dy)):
        row = [1.0, values[t]]                      # constante, y_{t-1}
        row.extend(dy[t - i] for i in range(1, lags + 1))
        rows.append(row)
        target.append(dy[t])
    if len(rows) <= len(rows[0]):
        return None

    try:
        import numpy as np
    except ImportError:                              # pragma: no cover
        return None
    X = np.asarray(rows, dtype=float)
    y = np.asarray(target, dtype=float)
    xtx = X.T @ X
    try:
        xtx_inv = np.linalg.inv(xtx)
    except np.linalg.LinAlgError:
        return None
    beta = xtx_inv @ X.T @ y
    resid = y - X @ beta
    dof = len(y) - X.shape[1]
    if dof <= 0:
        return None
    sigma2 = float(resid @ resid) / dof
    se_gamma = math.sqrt(max(sigma2 * float(xtx_inv[1, 1]), 0.0))
    if se_gamma <= 0:
        return None
    return float(beta[1]) / se_gamma


# Valeurs critiques ADF (constante, sans tendance), grand échantillon.
# Rejet de la racine unitaire si statistique < valeur critique.
ADF_CRITICAL = {0.01: -3.43, 0.05: -2.86, 0.10: -2.57}


__all__ = [
    "ADF_CRITICAL", "INTERVAL_MS", "adf_statistic", "adx", "aggregate",
    "anomalies", "atr", "bollinger_width", "closed", "dmi", "ema", "find_gaps",
    "half_life", "percentile_rank", "rma", "session_vwap", "sma",
    "sma_of_optional", "sort_dedup", "true_range", "zscore",
]

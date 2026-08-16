"""
Cœur pur du MomentumAgent — univers §1, signal §2, portefeuille §3-4.

Tout ce fichier est **sans I/O et sans horloge** : les séries et la date sont
injectées. C'est ce qui rend la propriété centrale testable — et cette propriété
est plus importante que tout le reste du module :

    l'univers à la date t n'utilise AUCUNE donnée postérieure à t.

Le §1 la nomme « piège n°1 » et il a raison : construire le panier avec les
coins liquides *d'aujourd'hui*, c'est sélectionner rétroactivement les gagnants
de la période testée. Un backtest cross-sectionnel qui ment ment presque
toujours ainsi, et il ment de façon spectaculaire — les alts qui ont survécu
jusqu'en 2026 ne sont pas un échantillon aléatoire de ceux qui existaient en
2021.

Toutes les fonctions de sélection prennent donc un `as_of_ms` explicite et
n'acceptent que des séries qu'elles tronquent elles-mêmes.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("sdm.momentum.core")

DAY_MS = 86_400_000
Candle = Dict[str, float]

# §1 — exclusions figées. Mécaniques, jamais discrétionnaires : une liste que
# l'on ajuste au vu des résultats deviendrait un degré de liberté non déclaré.
STABLE_TOKENS = frozenset({
    "USDT", "USDC", "BUSD", "TUSD", "DAI", "FDUSD", "USDP", "UST", "USTC",
    "EUR", "GBP", "AEUR", "USDE", "PYUSD",
})
REBASING_TOKENS = frozenset({"AMPL", "OHM", "BASE", "XFT", "LUNA", "LUNC"})


@dataclass(frozen=True)
class AssetScore:
    """Score de momentum d'un actif à une date, avec sa traçabilité."""

    symbol: str
    score: float
    rank: int = 0                    # 1 = meilleur
    price_start: float = 0.0
    price_end: float = 0.0

    def as_log(self) -> Dict[str, Any]:
        return {"symbol": self.symbol, "score": round(self.score, 6), "rank": self.rank}


@dataclass(frozen=True)
class Leg:
    symbol: str
    side: int                        # +1 long, -1 short
    weight: float                    # fraction du gross, signée
    notional: float = 0.0
    qty: float = 0.0
    entry_price: float = 0.0

    @property
    def is_long(self) -> bool:
        return self.side > 0


@dataclass
class Portfolio:
    legs: Dict[str, Leg] = field(default_factory=dict)
    as_of_ms: int = 0

    @property
    def symbols(self) -> set:
        return set(self.legs)

    @property
    def longs(self) -> List[str]:
        return sorted(s for s, l in self.legs.items() if l.is_long)

    @property
    def shorts(self) -> List[str]:
        return sorted(s for s, l in self.legs.items() if not l.is_long)

    def gross_notional(self) -> float:
        return sum(abs(l.notional) for l in self.legs.values())

    def net_notional(self) -> float:
        return sum(l.notional for l in self.legs.values())

    def dollar_neutrality(self) -> float:
        """|net| / gross. Vaut 0 pour un portefeuille parfaitement neutre.

        Exposé plutôt que supposé : le §3 exige la neutralité dollar, et une
        neutralité qu'on ne mesure pas est une neutralité qu'on n'a pas.
        """
        gross = self.gross_notional()
        return abs(self.net_notional()) / gross if gross > 0 else 0.0


# ── §1 Univers, sans biais du survivant ─────────────────────────────────────

def is_excluded(symbol: str, exclusions: Sequence[str]) -> Optional[str]:
    """Exclusion mécanique. Rend le motif, ou None."""
    base = symbol.upper().removesuffix("USDT").removesuffix("USD")
    if "stables" in exclusions and base in STABLE_TOKENS:
        return "stablecoin"
    if "rebasing" in exclusions and base in REBASING_TOKENS:
        return "supply rebasing"
    return None


def count_gaps(candles: Sequence[Candle], interval_ms: int) -> int:
    """Barres manquantes dans la série (jamais comblées)."""
    missing = 0
    for prev, cur in zip(candles, candles[1:]):
        delta = int(cur["ts"]) - int(prev["ts"])
        if delta > interval_ms:
            missing += delta // interval_ms - 1
    return missing


def select_universe(candles_by_symbol: Mapping[str, Sequence[Candle]], as_of_ms: int,
                    basket_size: int, liquidity_lookback_d: int,
                    min_history_d: int, max_gap_bars: int,
                    exclusions: Sequence[str] = ("stables", "rebasing"),
                    ) -> Tuple[List[str], Dict[str, str]]:
    """Panier à la date `as_of_ms`. **Aucune donnée postérieure n'est lue.**

    Rend `(symboles retenus, motifs d'exclusion)`. Les motifs sont rendus pour
    être loggés : le §1 exige que l'exclusion soit mécanique ET traçable, faute
    de quoi on ne peut pas distinguer un filtre qui travaille d'un bug.

    La liquidité est mesurée par le **volume médian** sur `liquidity_lookback_d`
    jours précédant t — la médiane et non la moyenne, parce qu'un unique jour de
    volume aberrant (listing, squeeze) suffirait sinon à propulser un actif
    illiquide dans le panier.
    """
    reasons: Dict[str, str] = {}
    scored: List[Tuple[float, str]] = []
    window_start = as_of_ms - liquidity_lookback_d * DAY_MS
    history_start = as_of_ms - min_history_d * DAY_MS

    for symbol, series in candles_by_symbol.items():
        why = is_excluded(symbol, exclusions)
        if why:
            reasons[symbol] = why
            continue

        # TRONCATURE STRICTE : rien au-delà de la date d'évaluation.
        past = [c for c in series if int(c["ts"]) < as_of_ms]
        if not past:
            reasons[symbol] = "aucune donnée avant t"
            continue
        if int(past[0]["ts"]) > history_start:
            reasons[symbol] = f"historique insuffisant (< {min_history_d} j avant t)"
            continue

        window = [c for c in past if int(c["ts"]) >= window_start]
        if len(window) < max(2, liquidity_lookback_d // 2):
            reasons[symbol] = "fenêtre de liquidité trop courte"
            continue

        gaps = count_gaps([c for c in past if int(c["ts"]) >= history_start], DAY_MS)
        if gaps > max_gap_bars:
            reasons[symbol] = f"{gaps} barres manquantes > {max_gap_bars}"
            continue

        volumes = sorted(float(c.get("volume", 0.0)) * float(c["close"]) for c in window)
        median = volumes[len(volumes) // 2] if volumes else 0.0
        if median <= 0:
            reasons[symbol] = "volume nul"
            continue
        scored.append((median, symbol))

    scored.sort(key=lambda kv: (-kv[0], kv[1]))       # tri déterministe
    keep = [s for _, s in scored[:basket_size]]
    for _, s in scored[basket_size:]:
        reasons[s] = "hors des plus liquides"
    return keep, reasons


# ── §2 Signal ───────────────────────────────────────────────────────────────

def momentum_score(candles: Sequence[Candle], as_of_ms: int, lookback_d: int,
                   skip_d: int) -> Optional[Tuple[float, float, float]]:
    """Rendement cumulé sur `lookback_d`, en excluant les `skip_d` derniers jours.

    Rend `(score, prix_début, prix_fin)`, ou None si l'historique manque.

    Le skip évite de capturer le retournement court terme — la mean-reversion à
    quelques jours est un effet documenté et **opposé** au momentum. Sans lui, on
    mesurerait la somme de deux effets contraires et le signal serait dilué par
    construction.
    """
    end_ms = as_of_ms - skip_d * DAY_MS
    start_ms = end_ms - lookback_d * DAY_MS

    past = [c for c in candles if int(c["ts"]) < as_of_ms]      # anti-lookahead
    window = [c for c in past if start_ms <= int(c["ts"]) < end_ms]
    if len(window) < 2:
        return None

    p0, p1 = float(window[0]["close"]), float(window[-1]["close"])
    if p0 <= 0 or p1 <= 0 or not math.isfinite(p0) or not math.isfinite(p1):
        return None
    return (p1 / p0 - 1.0, p0, p1)


def rank_scores(candles_by_symbol: Mapping[str, Sequence[Candle]], symbols: Sequence[str],
                as_of_ms: int, lookback_d: int, skip_d: int) -> List[AssetScore]:
    """Classement cross-sectionnel. Rang 1 = meilleur momentum.

    Un score non calculable (NaN, historique manquant) exclut l'actif du
    classement — il ne reçoit **jamais** un rang par défaut. Attribuer un rang
    médian à un actif dont on ne sait rien reviendrait à inventer de
    l'information au milieu du seul signal de la stratégie.
    """
    scored: List[AssetScore] = []
    for symbol in symbols:
        series = candles_by_symbol.get(symbol) or []
        out = momentum_score(series, as_of_ms, lookback_d, skip_d)
        if out is None:
            continue
        score, p0, p1 = out
        if not math.isfinite(score):
            continue
        scored.append(AssetScore(symbol=symbol, score=score, price_start=p0, price_end=p1))

    scored.sort(key=lambda a: (-a.score, a.symbol))    # déterministe sur ex æquo
    return [AssetScore(symbol=a.symbol, score=a.score, rank=i + 1,
                       price_start=a.price_start, price_end=a.price_end)
            for i, a in enumerate(scored)]


# ── §3-4 Portefeuille et hystérésis ─────────────────────────────────────────

def target_symbols(ranked: Sequence[AssetScore], n_legs: int,
                   held: Optional[Portfolio] = None,
                   hysteresis_rank: int = 0) -> Tuple[List[str], List[str], Dict[str, str]]:
    """Cibles long et short, avec la bande anti-churn du §4.

    Un actif DÉJÀ détenu n'est remplacé que s'il sort du top/bottom
    `n_legs + hysteresis_rank`. Sans cette bande, un actif qui oscille entre les
    rangs 3 et 4 génère un aller-retour à chaque rebalancement, qui ne paie que
    l'exchange — la leçon anti-frais du projet, appliquée au cross-sectionnel.

    Rend aussi les décisions par symbole, pour les compteurs de branche du §9.3.
    """
    decisions: Dict[str, str] = {}
    total = len(ranked)
    if total < 2 * n_legs:
        return [], [], {"__univers__": f"trop étroit ({total} < {2*n_legs})"}

    by_symbol = {a.symbol: a for a in ranked}
    held_long = set(held.longs) if held else set()
    held_short = set(held.shorts) if held else set()

    strict_long = [a.symbol for a in ranked[:n_legs]]
    strict_short = [a.symbol for a in ranked[-n_legs:]]
    tol_long = {a.symbol for a in ranked[:n_legs + hysteresis_rank]}
    tol_short = {a.symbol for a in ranked[-(n_legs + hysteresis_rank):]}

    # Les positions tenues qui restent dans la bande sont CONSERVÉES d'abord ;
    # les places restantes vont aux mieux classés non encore détenus.
    longs = [s for s in strict_long if s in held_long]
    longs += [s for s in held_long if s in tol_long and s not in longs]
    for s in strict_long:
        if len(longs) >= n_legs:
            break
        if s not in longs:
            longs.append(s)
    longs = longs[:n_legs]

    shorts = [s for s in strict_short if s in held_short]
    shorts += [s for s in held_short if s in tol_short and s not in shorts]
    for s in strict_short:
        if len(shorts) >= n_legs:
            break
        if s not in shorts:
            shorts.append(s)
    shorts = shorts[:n_legs]

    # Un actif ne peut pas être des deux côtés (panier étroit).
    overlap = set(longs) & set(shorts)
    for s in overlap:
        if by_symbol[s].rank <= total / 2:
            shorts.remove(s)
        else:
            longs.remove(s)

    for s in set(longs) | set(shorts) | held_long | held_short:
        if s in longs:
            decisions[s] = "conservé long" if s in held_long else "ouvert long"
        elif s in shorts:
            decisions[s] = "conservé short" if s in held_short else "ouvert short"
        else:
            decisions[s] = "fermé (sorti de la bande)"
    return sorted(longs), sorted(shorts), decisions


def build_portfolio(longs: Sequence[str], shorts: Sequence[str], equity: float,
                    gross_exposure_frac: float, max_weight_per_asset: float,
                    prices: Mapping[str, float], as_of_ms: int = 0) -> Portfolio:
    """Portefeuille neutre dollar, poids égaux par jambe (§3).

    Le plafond `max_weight_per_asset` mord quand le panier rétrécit. À 3 jambes
    par côté il est inactif (1/6 du gross < 20 %) ; à 1 jambe par côté il ramène
    chaque position de 50 % à 20 % du gross, et l'exposition brute effective
    tombe donc à 40 %. C'est voulu : **on préfère trader moins que trader
    concentré** — le §3 protège contre un panier temporairement étroit, pas
    contre l'ennui.

    La neutralité dollar est préservée dans tous les cas, le plafond
    s'appliquant symétriquement aux deux côtés.
    """
    pf = Portfolio(as_of_ms=as_of_ms)
    if not longs or not shorts or equity <= 0:
        return pf

    gross = equity * gross_exposure_frac
    per_side = gross / 2.0
    cap = gross * max_weight_per_asset

    for side, symbols in ((1, longs), (-1, shorts)):
        if not symbols:
            continue
        per_leg = min(per_side / len(symbols), cap)
        for symbol in symbols:
            price = float(prices.get(symbol, 0.0))
            if price <= 0:
                continue
            notional = side * per_leg
            pf.legs[symbol] = Leg(symbol=symbol, side=side,
                                  weight=notional / gross if gross else 0.0,
                                  notional=notional, qty=notional / price,
                                  entry_price=price)
    return pf


def leverage(pf: Portfolio, equity: float) -> float:
    return pf.gross_notional() / equity if equity > 0 else 0.0


__all__ = ["AssetScore", "Candle", "DAY_MS", "Leg", "Portfolio", "REBASING_TOKENS",
           "STABLE_TOKENS", "build_portfolio", "count_gaps", "is_excluded",
           "leverage", "momentum_score", "rank_scores", "select_universe",
           "target_symbols"]

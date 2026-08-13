"""
Moteur de backtest event-driven — SPEC §9.1 et §9.3.

Cadence : une évaluation à chaque clôture 15m, exactement comme le live (§8).
Le moteur appelle `ConfluenceAgent.decide()` — le MÊME code que le live, pas
une réimplémentation. Toute divergence entre backtest et production ne peut
donc venir que des données ou du modèle d'exécution, jamais de la logique.

**Ordonnancement causal**, dans cet ordre à chaque bougie `i` :

1. stop / trailing testés sur le low-high de `i`, contre le stop en vigueur —
   celui posé à la clôture de `i-1` ;
2. ordre maker en attente testé sur le low-high de `i` ;
3. trailing avancé avec la CLÔTURE de `i` ;
4. décision de confluence à la clôture de `i`, qui ne peut produire qu'un ordre
   pour `i+1`.

Aucune étape n'utilise une information que le marché n'avait pas encore
publiée. Un moteur qui remplirait un ordre sur la bougie où le signal naît
gagnerait un quart d'heure de futur à chaque trade.

**Modèle de coûts** (§9.1), volontairement pessimiste :

* entrée maker (`fee_maker`), sortie taker (`fee_taker`) — le trailing sort au
  marché ;
* slippage appliqué aux SEULES sorties market, dans le sens défavorable ;
* funding payé/reçu à chaque RÈGLEMENT traversé — horaire sur Hyperliquid,
  toutes les 8 h sur Binance ; facturer un pas fixe multiplierait le poste par
  huit selon la source ;
* fill maker exigeant une traversée STRICTE de la limite, et un ordre qui ne
  vit qu'une bougie 15m — cohérent avec les 90 s de timeout et 3 re-cotations
  du §4.4, qui tiennent largement dans un quart d'heure.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from confluence.agent import ConfluenceAgent, EvalCache
from confluence.config import ConfluenceConfig
from confluence.data import History
from confluence.indicators import INTERVAL_MS
from confluence.risk import RiskManager
from confluence.state import AgentState
from confluence.trailing import TrailingState
from confluence.types import ConfluenceSignal, Side

logger = logging.getLogger("sdm.confluence.backtest")

BAR_MS = INTERVAL_MS["15m"]


@dataclass
class Trade:
    entry_ms: int
    exit_ms: int
    side: str
    entry: float
    exit: float
    size: float
    notional: float
    gross_pnl: float
    fees: float
    funding: float
    reason: str
    bars_held: int
    mae: float = 0.0            # excursion défavorable maximale, en R
    mfe: float = 0.0            # excursion favorable maximale, en R

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees - self.funding


@dataclass
class BacktestResult:
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[tuple] = field(default_factory=list)   # [(ts_ms, equity)]
    veto_counts: Counter = field(default_factory=Counter)
    veto_reasons: Counter = field(default_factory=Counter)
    evaluations: int = 0
    signals: int = 0
    abandoned: int = 0
    start_ms: int = 0
    end_ms: int = 0
    initial_equity: float = 0.0

    # ── Métriques §9.3, toutes NETTES de frais ──────────────────────────────

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1][1] if self.equity_curve else self.initial_equity

    @property
    def net_pnl(self) -> float:
        return self.final_equity - self.initial_equity

    @property
    def gross_pnl_abs(self) -> float:
        return sum(abs(t.gross_pnl) for t in self.trades)

    @property
    def fees_paid(self) -> float:
        return sum(t.fees for t in self.trades)

    @property
    def funding_paid(self) -> float:
        return sum(t.funding for t in self.trades)

    @property
    def fee_ratio(self) -> Optional[float]:
        """`fees / gross_pnl_abs` — le critère §9.4 (< 15 %)."""
        gross = self.gross_pnl_abs
        return (self.fees_paid / gross) if gross > 0 else None

    @property
    def profit_factor(self) -> Optional[float]:
        """Somme des gains nets / somme des pertes nettes. None si aucune perte
        (résultat non interprétable plutôt qu'infini flatteur)."""
        wins = sum(t.net_pnl for t in self.trades if t.net_pnl > 0)
        losses = -sum(t.net_pnl for t in self.trades if t.net_pnl < 0)
        if losses <= 0:
            return None
        return wins / losses

    @property
    def win_rate(self) -> Optional[float]:
        if not self.trades:
            return None
        return sum(1 for t in self.trades if t.net_pnl > 0) / len(self.trades)

    @property
    def days(self) -> float:
        if not self.start_ms or not self.end_ms:
            return 0.0
        return (self.end_ms - self.start_ms) / 86_400_000.0

    @property
    def trades_per_day(self) -> float:
        d = self.days
        return len(self.trades) / d if d > 0 else 0.0

    @property
    def max_drawdown(self) -> float:
        """Drawdown maximal en fraction de l'equity de pic."""
        peak, worst = self.initial_equity, 0.0
        for _, eq in self.equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                worst = max(worst, (peak - eq) / peak)
        return worst

    @property
    def cagr(self) -> Optional[float]:
        d = self.days
        if d <= 0 or self.initial_equity <= 0 or self.final_equity <= 0:
            return None
        return (self.final_equity / self.initial_equity) ** (365.0 / d) - 1.0

    @property
    def mar(self) -> Optional[float]:
        """CAGR / maxDD (§9.3). None si aucun drawdown — non interprétable."""
        dd, cagr = self.max_drawdown, self.cagr
        if cagr is None or dd <= 0:
            return None
        return cagr / dd

    @property
    def sharpe(self) -> Optional[float]:
        """Sharpe annualisé sur les rendements JOURNALIERS de l'equity.

        Sur les rendements par trade, il dépendrait de la fréquence de trading —
        or c'est précisément ce que ce module cherche à réduire ; la métrique
        récompenserait alors le défaut qu'on corrige.
        """
        daily = self._daily_returns()
        if len(daily) < 2:
            return None
        mean = sum(daily) / len(daily)
        var = sum((r - mean) ** 2 for r in daily) / (len(daily) - 1)
        sd = math.sqrt(var)
        if sd <= 0:
            return None
        return mean / sd * math.sqrt(365.0)

    def _daily_returns(self) -> List[float]:
        by_day: Dict[int, float] = {}
        for ts, eq in self.equity_curve:
            by_day[ts // 86_400_000] = eq
        days = sorted(by_day)
        out = []
        for prev, cur in zip(days, days[1:]):
            base = by_day[prev]
            if base > 0:
                out.append(by_day[cur] / base - 1.0)
        return out

    def metrics(self) -> Dict[str, Any]:
        return {
            "trades": len(self.trades),
            "days": round(self.days, 1),
            "trades_per_day": round(self.trades_per_day, 3),
            "net_pnl": round(self.net_pnl, 2),
            "final_equity": round(self.final_equity, 2),
            "profit_factor": _round(self.profit_factor, 3),
            "win_rate": _round(self.win_rate, 3),
            "sharpe": _round(self.sharpe, 3),
            "cagr": _round(self.cagr, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "mar": _round(self.mar, 3),
            "fees_paid": round(self.fees_paid, 2),
            "funding_paid": round(self.funding_paid, 2),
            "gross_pnl_abs": round(self.gross_pnl_abs, 2),
            "fee_ratio": _round(self.fee_ratio, 4),
            "evaluations": self.evaluations,
            "signals": self.signals,
            "abandoned_fills": self.abandoned,
        }

    def veto_distribution(self, top: int = 12) -> List[tuple]:
        """Distribution des motifs de veto (§9.3). C'est le rapport qui dit
        POURQUOI le bot ne trade pas — sans lui, un backtest à 4 trades est
        indiscernable d'un backtest cassé."""
        return self.veto_reasons.most_common(top)


@dataclass
class _Position:
    signal: ConfluenceSignal
    trail: TrailingState
    size: float
    entry: float
    entry_ms: int
    entry_bar: int
    entry_fee: float
    funding_accrued: float = 0.0
    mae: float = 0.0
    mfe: float = 0.0


class Backtester:
    def __init__(self, cfg: ConfluenceConfig, initial_equity: float = 10_000.0,
                 params=None) -> None:
        self.cfg = cfg
        self.initial_equity = initial_equity
        # §12.3 : « le backtest du §9 doit tourner avec le RegimeConditioner
        # actif, pas sur paramètres figés ». Sans ce passe-plat, on validerait
        # une stratégie à k_stop constant pour en exécuter une où k_stop varie
        # avec la volatilité — deux stratégies différentes portant le même nom.
        self.params = params

    def run(self, history: History, start_ms: Optional[int] = None,
            end_ms: Optional[int] = None) -> BacktestResult:
        cfg = self.cfg
        agent = ConfluenceAgent(cfg, store=None, params=self.params)
        cache = EvalCache()

        bars = history.candles.get("15m", [])
        if not bars:
            return BacktestResult(initial_equity=self.initial_equity)

        warm_ms = self._warmup_end_ms(history)
        first_ms = max(warm_ms, start_ms or 0)
        last_ms = end_ms if end_ms is not None else int(bars[-1]["ts"]) + BAR_MS

        result = BacktestResult(initial_equity=self.initial_equity)
        state = AgentState()
        equity = self.initial_equity
        position: Optional[_Position] = None
        pending: Optional[ConfluenceSignal] = None
        pending_price: Optional[float] = None
        pending_risk = None
        last_atr_1h: Optional[float] = None

        # Les séries 1d/1h ne sont tranchées qu'aux frontières utiles : un
        # `bisect` par bougie plutôt qu'un filtrage complet, sinon le coût est
        # quadratique sur trois ans.
        idx = _SeriesIndex(history)

        for i, bar in enumerate(bars):
            bar_ts = int(bar["ts"])
            close_ms = bar_ts + BAR_MS
            if close_ms <= first_ms:
                continue
            if close_ms > last_ms:
                break

            high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])

            # 1. Stop en vigueur (posé à la clôture précédente) contre cette bougie.
            if position is not None:
                hit = agent.trailing.hit(position.trail, low, high)
                if hit is not None:
                    equity = self._close(result, position, hit, close_ms, i,
                                         equity, history, "stop", market=True)
                    closed = result.trades[-1]
                    state = agent.on_exit(state, closed.gross_pnl, closed.fees,
                                          close_ms, "stop", funding=closed.funding)
                    position = None

            # 2. Ordre maker en attente : rempli seulement si le marché vient le
            #    chercher pendant CETTE bougie.
            if position is None and pending is not None and pending_price is not None:
                filled = (low < pending_price if pending.side is Side.LONG
                          else high > pending_price)
                if filled:
                    position, state, equity = self._open(
                        agent, state, pending, pending_price, equity, bar_ts, i,
                        pending_risk)
                else:
                    result.abandoned += 1
                pending, pending_price, pending_risk = None, None, None
            elif pending is not None:
                pending, pending_price, pending_risk = None, None, None

            # 3. Trailing avancé sur la clôture, avec l'ATR_1h le plus récent
            #    connu — celui du dernier verdict de régime. Le recalculer ici
            #    referait tourner l'ADX 4× par heure pour rien.
            if position is not None:
                agent.trailing.update(position.trail, close,
                                      last_atr_1h or position.signal.atr_1h)
                self._track_excursion(position, low, high)

            # 4. Décision à la clôture de cette bougie.
            series = idx.slices(close_ms)
            decision = agent.decide(
                now_ms=close_ms,
                candles=series,
                state=state,
                equity=equity,
                funding_hourly=history.funding_at(close_ms),
                already_closed=True,
                cache=cache,
            )
            state = decision.state or state
            result.evaluations += 1
            v_regime = decision.verdicts.get("1h")
            if v_regime is not None and v_regime.data.get("atr_1h"):
                last_atr_1h = float(v_regime.data["atr_1h"])
            blocking = decision.blocked_by
            if blocking:
                result.veto_counts[blocking] += 1
                result.veto_reasons[_reason_key(blocking, decision.reason)] += 1

            # §6.4 — invalidation de biais sur une position ouverte.
            if position is not None:
                invalidation = agent.bias_invalidated(state, decision)
                if invalidation:
                    equity = self._close(result, position, close, close_ms, i,
                                         equity, history, f"invalidation: {invalidation}",
                                         market=True)
                    closed = result.trades[-1]
                    state = agent.on_exit(state, closed.gross_pnl, closed.fees,
                                          close_ms, "invalidation", funding=closed.funding)
                    position = None

            if decision.signal is not None and position is None:
                result.signals += 1
                pending = decision.signal
                # On bide au bord passif de la zone : c'est le prix que le
                # marché doit venir chercher (§4.4, ordre limite post-only).
                pending_price = (pending.entry_zone[0] if pending.side is Side.LONG
                                 else pending.entry_zone[1])
                pending_risk = decision.risk_config

            result.equity_curve.append((close_ms, equity + self._unrealized(position, close)))
            result.start_ms = result.start_ms or close_ms
            result.end_ms = close_ms

        # Position encore ouverte en fin de période : on la solde au dernier
        # prix connu, sinon les métriques ignoreraient un trade en cours (et
        # une stratégie perdante pourrait « cacher » sa perte dans le flottant).
        if position is not None:
            last_close = float(bars[-1]["close"])
            equity = self._close(result, position, last_close,
                                 int(bars[-1]["ts"]) + BAR_MS, len(bars) - 1,
                                 equity, history, "fin de période", market=True)

        logger.info("backtest: %s", result.metrics())
        return result

    # ── Ouverture / fermeture ───────────────────────────────────────────────

    def _open(self, agent: ConfluenceAgent, state: AgentState, signal: ConfluenceSignal,
              price: float, equity: float, bar_ts: int, bar_index: int,
              risk_cfg=None):
        # On dimensionne avec la config qui a posé le stop, pas avec celle du
        # YAML : sous conditionnement (§12.3), les deux diffèrent.
        risk = RiskManager(risk_cfg) if risk_cfg is not None else agent.risk
        sizing = risk.size(equity, price, signal.stop_price)
        if not sizing.valid:
            return None, state, equity
        entry_fee = sizing.notional * risk.cfg.fee_maker
        now_ms = bar_ts + BAR_MS
        state = agent.on_entry(state, signal, price, sizing.size, now_ms)
        trail = agent.trailing.open(signal.side, price, signal.stop_price)
        position = _Position(
            signal=signal, trail=trail, size=sizing.size, entry=price,
            entry_ms=now_ms, entry_bar=bar_index, entry_fee=entry_fee)
        return position, state, equity

    def _close(self, result: BacktestResult, position: _Position, price: float,
               now_ms: int, bar_index: int, equity: float, history: History,
               reason: str, market: bool) -> float:
        side_sign = position.signal.side.sign
        exit_price = price
        if market:
            # Slippage TOUJOURS dans le sens défavorable (§9.1).
            slip = price * self.cfg.backtest.slippage_bps_market / 10_000.0
            exit_price = price - side_sign * slip

        notional_in = position.size * position.entry
        notional_out = position.size * exit_price
        gross = side_sign * (exit_price - position.entry) * position.size
        exit_fee = notional_out * (self.cfg.risk.fee_taker if market
                                   else self.cfg.risk.fee_maker)
        fees = position.entry_fee + exit_fee
        funding = self._funding_cost(history, position, now_ms, notional_in)

        trade = Trade(
            entry_ms=position.entry_ms, exit_ms=now_ms,
            side=position.signal.side.name, entry=position.entry, exit=exit_price,
            size=position.size, notional=notional_in, gross_pnl=gross, fees=fees,
            funding=funding, reason=reason,
            bars_held=max(0, bar_index - position.entry_bar),
            mae=position.mae, mfe=position.mfe,
        )
        result.trades.append(trade)
        return equity + trade.net_pnl

    def _funding_cost(self, history: History, position: _Position,
                      exit_ms: int, notional: float) -> float:
        """Funding payé (positif) ou reçu (négatif) sur la durée de détention.

        On facture chaque RÈGLEMENT effectivement traversé, pas un pas de temps
        arbitraire. La distinction n'est pas cosmétique : Hyperliquid règle le
        funding toutes les heures, Binance toutes les 8 heures. Une boucle
        horaire appliquée à des taux 8 h les facturerait huit fois — un poste de
        coût multiplié par huit, capable à lui seul de condamner une stratégie
        rentable ou d'en sauver une qui ne l'est pas.

        Sur une position tenue plusieurs jours en funding élevé, ce poste
        dépasse largement les frais de transaction.
        """
        sign = position.signal.side.sign
        return sum(
            sign * rate * notional
            for ts, rate in history.funding
            if position.entry_ms < ts <= exit_ms
        )

    @staticmethod
    def _track_excursion(position: _Position, low: float, high: float) -> None:
        unit = position.trail.risk_unit
        if unit <= 0:
            return
        sign = position.signal.side.sign
        favourable = (high - position.entry) if sign > 0 else (position.entry - low)
        adverse = (position.entry - low) if sign > 0 else (high - position.entry)
        position.mfe = max(position.mfe, favourable / unit)
        position.mae = max(position.mae, adverse / unit)

    @staticmethod
    def _unrealized(position: Optional[_Position], price: float) -> float:
        if position is None:
            return 0.0
        return position.signal.side.sign * (price - position.entry) * position.size

    def _warmup_end_ms(self, history: History) -> int:
        """Instant à partir duquel TOUTES les couches ont leur fenêtre pleine.

        Décider avant cet instant produirait des vetos « warmup insuffisant »
        qui pollueraient la distribution des motifs du §9.3 et masqueraient les
        vrais filtres.
        """
        needs = {"1d": self.cfg.bias_1d.warmup_bars,
                 "1h": self.cfg.regime_1h.warmup_bars,
                 "15m": self.cfg.timing_15m.warmup_bars}
        out = 0
        for tf, need in needs.items():
            series = history.candles.get(tf, [])
            if len(series) < need:
                # Série trop courte : aucune décision ne sera jamais possible.
                return int(series[-1]["ts"]) + INTERVAL_MS[tf] if series else 2 ** 62
            out = max(out, int(series[need - 1]["ts"]) + INTERVAL_MS[tf])
        return out


class _SeriesIndex:
    """Découpe causale des séries par `bisect`, et rien de plus.

    Reconstruire `[c for c in series if c["ts"] < now]` à chaque bougie coûte
    O(n) et rend le backtest quadratique. Ici on avance un curseur par
    timeframe : les 105 000 réveils coûtent au total un parcours de chaque
    série.
    """

    def __init__(self, history: History) -> None:
        self.history = history
        self.ts: Dict[str, List[int]] = {
            tf: [int(c["ts"]) for c in history.candles.get(tf, [])]
            for tf in ("1d", "1h", "15m", "1m")
        }
        self._cursor = {tf: 0 for tf in self.ts}

    def slices(self, now_ms: int) -> Dict[str, List[dict]]:
        out = {}
        for tf, times in self.ts.items():
            step = INTERVAL_MS[tf]
            cur = self._cursor[tf]
            while cur < len(times) and times[cur] + step <= now_ms:
                cur += 1
            self._cursor[tf] = cur
            out[tf] = self.history.candles.get(tf, [])[:cur]
        return out


def _reason_key(layer: str, reason: str) -> str:
    """Motif de veto normalisé pour la distribution du §9.3.

    Les raisons portent leurs valeurs — « ADX=23.7 », « (close=62559,
    open=62762) » — parce que c'est ce qui sert au diagnostic ligne à ligne.
    Pour la distribution, il faut au contraire agréger : on coupe au premier
    deux-points ET à la première parenthèse, sinon un motif fréquent se
    présente en 40 000 variantes uniques et disparaît du classement.
    """
    head = reason.split(":")[0].split("(")[0].strip()
    return f"{layer}/{head}"


def _round(value: Optional[float], digits: int) -> Optional[float]:
    return None if value is None else round(value, digits)


__all__ = ["BacktestResult", "Backtester", "Trade"]

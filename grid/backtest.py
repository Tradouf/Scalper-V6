"""
Moteur de backtest du GridAgent — SPEC §9.

**Granularité 1 m.** Une grille est une stratégie intrabar : une seule bougie
15 m peut traverser plusieurs niveaux, dans un ordre que la bougie ne dit pas.
Simuler une grille sur des bougies 15 m reviendrait à deviner cet ordre — et
donc à choisir son résultat. Le moteur descend à la minute pour les fills, tout
en gardant les décisions à leur cadence propre : régime en 1 h, cassure en 15 m.

**Modèle de fill conservateur (§9.1)** : un ordre maker n'est réputé exécuté que
si le prix **traverse** le niveau, jamais sur un simple contact, et sans aucune
modélisation de file d'attente favorable. Cette hypothèse sous-estime légèrement
les fills : c'est voulu, et c'est documenté ici comme le §9.1 l'exige.

**Ordonnancement intrabar.** Quand une bougie 1 m traverse plusieurs niveaux, ils
sont exécutés dans l'ordre de leur distance à l'OUVERTURE de la bougie : c'est
le chemin que le prix a physiquement dû suivre. Aucune information de fin de
bougie n'est utilisée pour décider d'un fill en cours de bougie.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from confluence import indicators as ind
from confluence.config import ConfluenceConfig
from grid.accounting import GridSession, aggregate
from grid.agent import GridAgent
from grid.build import build_grid, check_activation, detect_range
from grid.config import GridConfig
from grid.router import Engine, RouterState, StrategyRouter
from grid.types import Side

logger = logging.getLogger("sdm.grid.backtest")

MIN_MS = ind.INTERVAL_MS["1m"]
M15_MS = ind.INTERVAL_MS["15m"]
H1_MS = ind.INTERVAL_MS["1h"]


@dataclass
class GridBacktestResult:
    sessions: List[GridSession] = field(default_factory=list)
    equity_curve: List[tuple] = field(default_factory=list)
    activation_vetoes: Dict[str, int] = field(default_factory=dict)
    initial_equity: float = 10_000.0
    start_ms: int = 0
    end_ms: int = 0
    bars_processed: int = 0
    handoff_pnl: float = 0.0

    @property
    def net_mtm_pnl(self) -> float:
        """§7 : la SEULE métrique de décision."""
        return sum(s.pnl.net for s in self.sessions) + self.handoff_pnl

    @property
    def days(self) -> float:
        if not self.start_ms or not self.end_ms:
            return 0.0
        return (self.end_ms - self.start_ms) / 86_400_000.0

    def metrics(self) -> Dict[str, Any]:
        agg = aggregate(self.sessions)
        agg.update({
            "days": round(self.days, 1),
            "net_mtm_pnl": round(self.net_mtm_pnl, 2),   # inclut le PnL des handoffs
            "handoff_pnl": round(self.handoff_pnl, 2),
            "sessions_per_month": (round(len(self.sessions) / (self.days / 30.0), 2)
                                   if self.days > 0 else 0.0),
            "bars_processed": self.bars_processed,
        })
        return agg

    def veto_distribution(self, top: int = 12) -> List[tuple]:
        return sorted(self.activation_vetoes.items(), key=lambda kv: -kv[1])[:top]


class GridBacktester:
    def __init__(self, cfg: GridConfig, conf_cfg: ConfluenceConfig,
                 initial_equity: float = 10_000.0) -> None:
        self.cfg = cfg
        self.conf_cfg = conf_cfg
        self.initial_equity = initial_equity

    # ── Indicateurs de régime, calculés une fois ────────────────────────────

    def _regime_series(self, candles_1h: Sequence[dict]) -> Dict[str, list]:
        """ADX et percentile d'ATR sur 1 h.

        Ce sont les mêmes fonctions que celles du `RegimeLayer` du
        ConfluenceAgent, avec les mêmes paramètres : on réutilise le CALCUL sans
        réutiliser le JUGEMENT. Le verdict du RegimeLayer veto hors de [20, 90]
        de percentile et en biais 1d FLAT ; la grille exige [15, 60] et se moque
        du biais.
        """
        r = self.conf_cfg.regime_1h
        adx = ind.adx(candles_1h, r.adx_period)
        atr = ind.atr(candles_1h, r.atr_period)
        window = r.percentile_window_bars

        percentiles: List[Optional[float]] = []
        for i, value in enumerate(atr):
            if value is None or i < window:
                percentiles.append(None)
                continue
            hist = [v for v in atr[max(0, i - window + 1):i + 1] if v is not None]
            percentiles.append(ind.percentile_rank(hist, value))
        return {"adx": adx, "atr": atr, "percentile": percentiles}

    # ── Boucle principale ───────────────────────────────────────────────────

    def run(self, candles_1m: Sequence[dict], candles_15m: Sequence[dict],
            candles_1h: Sequence[dict], funding: Sequence[tuple] = (),
            bias_by_day: Optional[Dict[int, str]] = None,
            start_ms: Optional[int] = None, end_ms: Optional[int] = None,
            breakout_handoff: Optional[bool] = None) -> GridBacktestResult:
        """`breakout_handoff` force la variante A/B du §9.5 sans toucher au YAML."""
        cfg = self.cfg
        if breakout_handoff is not None and breakout_handoff != cfg.exits.breakout_handoff:
            cfg = cfg.replace_path("exits.breakout_handoff", breakout_handoff)

        result = GridBacktestResult(initial_equity=self.initial_equity)
        if not candles_1m or not candles_15m or not candles_1h:
            return result

        regime = self._regime_series(candles_1h)
        atr_15m = ind.atr(candles_15m, 14)
        ts_1h = [int(c["ts"]) for c in candles_1h]
        ts_15m = [int(c["ts"]) for c in candles_15m]
        funding_times = [t for t, _ in funding]
        funding_rates = [r for _, r in funding]

        router = StrategyRouter(cfg.activation.confirm_bars_1h)
        router_state = RouterState()

        equity = self.initial_equity
        agent: Optional[GridAgent] = None
        cooldown_until_ms = 0
        adx_streak = 0
        i_1h = i_15m = i_fund = 0
        last_1h_seen = last_15m_seen = -1
        handoff_position = None

        warm = self._warmup_ms(candles_1h, candles_15m)
        first_ms = max(warm, start_ms or 0)

        for bar in candles_1m:
            bar_ts = int(bar["ts"])
            close_ms = bar_ts + MIN_MS
            if close_ms <= first_ms:
                continue
            if end_ms is not None and close_ms > end_ms:
                break
            result.bars_processed += 1
            result.start_ms = result.start_ms or close_ms
            result.end_ms = close_ms

            high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])

            # ── Avance des index de TF supérieurs (bougies CLÔTURÉES) ──
            while i_1h + 1 < len(ts_1h) and ts_1h[i_1h + 1] + H1_MS <= close_ms:
                i_1h += 1
            while i_15m + 1 < len(ts_15m) and ts_15m[i_15m + 1] + M15_MS <= close_ms:
                i_15m += 1

            adx = regime["adx"][i_1h]
            atr_1h = regime["atr"][i_1h]
            pct = regime["percentile"][i_1h]
            a15 = atr_15m[i_15m] if i_15m < len(atr_15m) else None

            # Le curseur de funding avance TOUJOURS, session ou pas. Le
            # confiner à la branche « session active » le laissait bloqué à 0
            # tant qu'aucune grille ne tournait : le filtre §2 lisait alors le
            # tout premier taux de la série pour l'éternité et vetait en
            # permanence — une grille qui ne se déploie jamais, pour une raison
            # qui n'a rien à voir avec le marché.
            while i_fund < len(funding_times) and funding_times[i_fund] <= close_ms:
                if agent is not None and agent.stopped is None:
                    agent.accrue_funding(funding_rates[i_fund], close)
                i_fund += 1

            # ── Nouvelle bougie 1h : régime et routage ──
            if i_1h != last_1h_seen:
                last_1h_seen = i_1h
                if adx is not None:
                    adx_streak = adx_streak + 1 if adx < cfg.activation.adx_max else 0
                regime_label = self._regime_label(adx)
                router.route(router_state, regime=regime_label, bar_ts=ts_1h[i_1h],
                             has_handoff_position=handoff_position is not None)

            # ── Position héritée du handoff : gérée par le trailing ──
            if handoff_position is not None:
                closed_pnl = self._manage_handoff(handoff_position, high, low, close)
                if closed_pnl is not None:
                    result.handoff_pnl += closed_pnl
                    handoff_position = None

            # ── Session active ──
            if agent is not None and agent.stopped is None:
                self._fill_levels(agent, bar, close_ms)

                agent.mark(close)

                # Cassure : évaluée à chaque CLÔTURE 15m (§6.1)
                if i_15m != last_15m_seen and a15:
                    last_15m_seen = i_15m
                    decision = agent.check_breakout(float(candles_15m[i_15m]["close"]), a15)
                    if decision.triggered:
                        bias = self._bias_at(bias_by_day, close_ms)
                        _, handoff = agent.on_breakout(
                            decision, close_ms, close, bias, atr_1h or 0.0)
                        if handoff is not None:
                            handoff_position = self._open_handoff(handoff, close_ms)
                        cooldown_until_ms = close_ms + int(
                            cfg.exits.breakout_cooldown_h * 3_600_000)

                if agent.stopped is None and agent.drawdown_breached():
                    agent.on_drawdown(close_ms, close)
                if agent.stopped is None and pct is not None and pct > 90:
                    agent.on_vol_spike(close_ms)
                if agent.stopped is None and router_state.current_engine != Engine.GRID.value:
                    agent.on_regime_shift(close_ms, close)

                if agent.stopped is not None:
                    session = agent.finish(close)
                    result.sessions.append(session)
                    equity += session.pnl.net
                    agent = None

            # ── Pas de session : tenter un déploiement ──
            elif agent is None and i_15m != last_15m_seen:
                last_15m_seen = i_15m
                if router_state.current_engine == Engine.GRID.value:
                    agent = self._try_deploy(
                        cfg, candles_15m[:i_15m + 1], adx, adx_streak, pct,
                        atr_1h, a15, self._funding_annualized(funding_rates, i_fund),
                        cooldown_until_ms, close_ms, equity, result)

            result.equity_curve.append((close_ms, equity + (agent.acct.net if agent else 0.0)))

        if agent is not None and agent.stopped is None:
            agent.close_at_end(result.end_ms, float(candles_1m[-1]["close"]))
            result.sessions.append(agent.finish(float(candles_1m[-1]["close"])))

        logger.info("grid backtest: %s", result.metrics())
        return result

    # ── Fills intrabar ──────────────────────────────────────────────────────

    def _fill_levels(self, agent: GridAgent, bar: dict, close_ms: int) -> None:
        """Exécute les niveaux TRAVERSÉS par cette bougie 1 m (§9.1).

        Traversée STRICTE : `low < prix` pour un BUY, `high > prix` pour un
        SELL. Un simple contact ne remplit pas — sans file d'attente modélisée,
        supposer l'exécution au touch reviendrait à s'attribuer la meilleure
        place du carnet à chaque niveau.
        """
        high, low = float(bar["high"]), float(bar["low"])
        open_ = float(bar["open"])

        candidates = []
        for level in agent.place_orders():
            crossed = (low < level.price) if level.side is Side.BUY else (high > level.price)
            if crossed:
                candidates.append(level)
        # Ordre de parcours = ordre physique du prix depuis l'ouverture.
        candidates.sort(key=lambda lv: abs(lv.price - open_))
        for level in candidates:
            if agent.stopped is not None:
                break
            agent.on_fill(level, close_ms)

    # ── Déploiement ─────────────────────────────────────────────────────────

    def _try_deploy(self, cfg, candles_15m, adx, adx_streak, pct, atr_1h, atr_15m,
                    funding_ann, cooldown_until_ms, now_ms, equity,
                    result) -> Optional[GridAgent]:
        cooldown_h = max(0.0, (cooldown_until_ms - now_ms) / 3_600_000.0)
        spec = (detect_range(candles_15m, cfg.build.range_lookback_bars_15m,
                             cfg.build.tick_size)
                if len(candles_15m) >= cfg.build.range_lookback_bars_15m else None)

        verdict = check_activation(
            cfg, adx=adx, adx_bars_below=adx_streak, atr_percentile=pct,
            range_spec=spec, atr_1h=atr_1h or 0.0, funding_annualized=funding_ann,
            fee_killswitch_active=False, observation_mode=False, macro_extreme=False,
            cooldown_remaining_h=cooldown_h)

        if not verdict.passed:
            key = verdict.reason.split(":")[0].split("(")[0].strip()
            result.activation_vetoes[key] = result.activation_vetoes.get(key, 0) + 1
            return None

        plan = build_grid(cfg, spec, atr_1h, atr_15m or atr_1h, equity)
        if plan is None:
            result.activation_vetoes["grille non déployable"] = (
                result.activation_vetoes.get("grille non déployable", 0) + 1)
            return None
        return GridAgent(cfg, plan, equity=equity, started_ms=now_ms)

    # ── Position héritée du handoff ─────────────────────────────────────────

    def _open_handoff(self, handoff, ts_ms: int) -> Dict[str, Any]:
        """Confie la position au TrailingStopAgent — infrastructure validée du
        candidat n°1, et non son signal rejeté."""
        from confluence.trailing import TrailingStopAgent
        from confluence.types import Side as CSide

        agent = TrailingStopAgent(self.conf_cfg.trailing)
        side = CSide.LONG if handoff.side is Side.BUY else CSide.SHORT
        state = agent.open(side, handoff.entry_price, handoff.stop_price)
        return {"agent": agent, "state": state, "size": handoff.size,
                "atr_1h": handoff.atr_1h, "opened_ms": ts_ms}

    def _manage_handoff(self, pos: Dict[str, Any], high: float, low: float,
                        close: float) -> Optional[float]:
        """Rend le PnL net si la position est sortie, sinon None."""
        agent, state = pos["agent"], pos["state"]
        hit = agent.hit(state, low, high)
        if hit is not None:
            gross = state.side.sign * (hit - state.entry) * pos["size"]
            fees = (state.entry + hit) * pos["size"] * self.cfg.build.taker_fee
            return gross - fees
        agent.update(state, close, pos["atr_1h"])
        return None

    # ── Utilitaires ─────────────────────────────────────────────────────────

    @staticmethod
    def _regime_label(adx: Optional[float]) -> Optional[str]:
        if adx is None:
            return None
        if adx > 25:
            return "trend"
        if adx < 20:
            return "range"
        return "chop"

    @staticmethod
    def _bias_at(bias_by_day: Optional[Dict[int, str]], ts_ms: int) -> Optional[str]:
        if not bias_by_day:
            return None
        return bias_by_day.get(ts_ms // 86_400_000)

    @staticmethod
    def _funding_annualized(rates: Sequence[float], index: int) -> Optional[float]:
        """Taux annualisé du dernier règlement TRAVERSÉ.

        `index` pointe le prochain règlement à venir : le dernier connu est donc
        `index - 1`. Avant le premier règlement, on ne connaît aucun taux et on
        rend None — le §2 traite l'absence comme un veto, ce qui est correct :
        un filtre qu'on ne peut pas évaluer n'est pas un filtre passé.
        """
        if not rates or index <= 0:
            return None
        return rates[min(index - 1, len(rates) - 1)] * 24 * 365

    def _warmup_ms(self, candles_1h, candles_15m) -> int:
        need_1h = self.conf_cfg.regime_1h.warmup_bars
        need_15m = self.cfg.build.range_lookback_bars_15m
        out = 0
        if len(candles_1h) > need_1h:
            out = max(out, int(candles_1h[need_1h]["ts"]) + H1_MS)
        if len(candles_15m) > need_15m:
            out = max(out, int(candles_15m[need_15m]["ts"]) + M15_MS)
        return out


__all__ = ["GridBacktestResult", "GridBacktester"]

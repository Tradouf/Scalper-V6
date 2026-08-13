"""
ConfluenceAgent — orchestrateur. SPEC §8.

Réveil à chaque clôture 15m, réévaluation **complète et descendante**
(1d → 1h → 15m), court-circuit au premier veto, puis contrôle du risque.

Trois propriétés que ce fichier doit garantir :

* **Idempotence** (§8) — rejouer la même bougie 15m ne produit jamais deux
  signaux. La clé est le `ts` de la bougie, pas l'horloge.
* **Traçabilité** — chaque évaluation est loggée en JSON avec les verdicts de
  toutes les couches calculées, MÊME sans trade. C'est explicitement la donnée
  que le §5 veut conserver : « c'est la donnée qui permettra d'auditer pourquoi
  le bot ne trade pas ». Un bot silencieux dont on ne sait pas s'il filtre ou
  s'il est cassé est un bot qu'on finit par débrancher pour de mauvaises
  raisons.
* **Pureté de la décision** — `decide()` ne touche ni le disque ni le réseau.
  L'I/O (lecture macro, persistance) est faite par l'appelant, ou par les
  méthodes `run_*` clairement identifiées comme telles.

Le §2 place aussi le MeanReversionAgent sous ce toit : il n'est consulté que
lorsque la couche 1h retourne RANGE ; en TREND il est suspendu.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from confluence.config import ConfluenceConfig
from confluence.indicators import closed
from confluence.layers import BiasLayer, LayerContext, RegimeLayer, TimingLayer
from confluence.layers.execution import ExecutionLayer
from confluence.macro import MacroRegimeAgent, MacroReading, resolve
from confluence.meanrev import MeanReversionAgent
from confluence.risk import GuardResult, RiskManager, SizeResult
from confluence.state import AgentState, ClosedTrade, StateStore
from confluence.trailing import TrailingStopAgent
from confluence.types import (
    Bias,
    ConfluenceSignal,
    LayerVerdict,
    Regime,
    Side,
    ok,
    utc,
    veto,
)

logger = logging.getLogger("sdm.confluence.agent")

TIMEFRAMES = ("1d", "1h", "15m", "1m")


@dataclass
class Decision:
    """Résultat d'une évaluation, signal ou pas."""

    bar_ts: int
    verdicts: Dict[str, LayerVerdict] = field(default_factory=dict)
    signal: Optional[ConfluenceSignal] = None
    risk_verdict: Optional[LayerVerdict] = None
    guards: Optional[GuardResult] = None
    sizing: Optional[SizeResult] = None
    state: Optional[AgentState] = None
    macro: Optional[MacroReading] = None
    adaptive: Optional[Dict[str, Any]] = None   # §12 : posture, set, conditionnement
    # Config de risque RÉELLEMENT utilisée pour ce cycle (conditionnée §12.3).
    # L'exécutant doit dimensionner avec la MÊME que celle qui a posé le stop,
    # sinon le risque par trade ne vaut plus ce que le sizing croit valoir.
    risk_config: Optional[Any] = None

    @property
    def blocked_by(self) -> Optional[str]:
        """Couche qui a opposé le veto, ou None si le signal est passé."""
        for key, v in self.verdicts.items():
            if not v.passed:
                return key
        if self.risk_verdict is not None and not self.risk_verdict.passed:
            return "risk"
        return None

    @property
    def reason(self) -> str:
        blocking = self.blocked_by
        if blocking is None:
            return "signal émis" if self.signal else "aucun veto mais pas de signal"
        source = self.risk_verdict if blocking == "risk" else self.verdicts[blocking]
        return source.reason

    def as_log(self) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "event": "confluence_eval",
            "bar_ts": self.bar_ts,
            "bar_time": utc(self.bar_ts).isoformat() if self.bar_ts else None,
            "blocked_by": self.blocked_by,
            "reason": self.reason,
            "verdicts": {k: v.as_log() for k, v in self.verdicts.items()},
        }
        if self.risk_verdict is not None:
            record["risk"] = self.risk_verdict.as_log()
        if self.adaptive is not None:
            record["adaptive"] = self.adaptive
        if self.macro is not None:
            record["macro"] = {
                "risk_level": self.macro.risk_level.value,
                "source": self.macro.source,
                "note": self.macro.note,
            }
        if self.sizing is not None:
            record["sizing"] = {
                "size": self.sizing.size,
                "notional": self.sizing.notional,
                "risk_usd": self.sizing.risk_usd,
                "capped_by": self.sizing.capped_by,
            }
        if self.signal is not None:
            record["signal"] = self.signal.as_log()
        return record


class DeploymentBlocked(RuntimeError):
    """Le module a été rejeté par le §9 : aucun passage d'ordre autorisé."""


BLOCK_FILE = Path(__file__).resolve().parent / "DEPLOY_BLOCKED"


def _assert_deployable() -> None:
    """Refuse le mode live tant que le verdict de rejet est en place.

    Un VERDICT.md est un document ; on peut ne pas le lire. Ce garde-fou-ci est
    exécutable : il transforme le verdict en erreur au moment précis où
    quelqu'un tenterait de brancher le module sur un compte réel. Le backtest et
    les tests restent libres — c'est l'ORDRE qui est interdit, pas l'étude.
    """
    if BLOCK_FILE.exists():
        raise DeploymentBlocked(
            f"ConfluenceAgent: déploiement bloqué par {BLOCK_FILE.name} — "
            f"verdict §9 REJETÉ (placebo p=0,42 et p=0,61). "
            f"Voir confluence/VERDICT.md. Le backtest reste autorisé ; "
            f"le passage d'ordre non."
        )


class ConfluenceAgent:
    def __init__(
        self,
        cfg: ConfluenceConfig,
        macro: Optional[MacroRegimeAgent] = None,
        store: Optional[StateStore] = None,
        params=None,
        live: bool = False,
    ) -> None:
        # `live=True` est le seul chemin vers un passage d'ordre : il est barré
        # tant que le module porte un verdict de rejet.
        if live:
            _assert_deployable()
        self.live = live
        # `params` = AdaptiveParameterManager (§12). Absent, l'agent tourne sur
        # la config figée du YAML — c'est le comportement d'avant le §12, gardé
        # pour les tests et pour qu'une panne de l'APM ne rende pas l'agent
        # inconstructible.
        self.params = params
        self.cfg = cfg
        self.bias_layer = BiasLayer(cfg.bias_1d)
        self.regime_layer = RegimeLayer(cfg.regime_1h)
        self.meanrev = MeanReversionAgent(cfg.meanrev)
        self.timing_layer = TimingLayer(cfg.timing_15m, meanrev=self.meanrev)
        self.execution_layer = ExecutionLayer(cfg.execution_1m)
        self.trailing = TrailingStopAgent(cfg.trailing)
        self.risk = RiskManager(cfg.risk)
        self.macro = macro if macro is not None else MacroRegimeAgent(cfg.macro)
        self.store = store

    # ── Décision (pure) ─────────────────────────────────────────────────────

    def decide(
        self,
        now_ms: int,
        candles: Dict[str, List[dict]],
        state: AgentState,
        equity: float,
        funding_hourly: Optional[float] = None,
        macro_reading: Optional[MacroReading] = None,
        already_closed: bool = False,
        cache: Optional["EvalCache"] = None,
    ) -> Decision:
        """Évaluation complète descendante. AUCUNE I/O.

        `candles` : séries brutes par timeframe. Elles sont filtrées ici même
        sur « bougie clôturée » (§3) sauf si `already_closed=True` — le backtest
        les a alors déjà découpées, et refiltrer serait redondant.

        `state` n'est pas muté : la décision porte un `state` NOUVEAU, que
        l'appelant persiste s'il le veut. Muter l'état dans une fonction de
        décision rendrait le backtest non rejouable.

        `cache` mémoïse les verdicts 1d et 1h, qui ne peuvent changer qu'à la
        clôture de LEUR bougie. C'est une optimisation du backtest (105 000
        réveils 15m contre 26 000 bougies 1h), et elle est sûre parce que la
        clé de cache couvre toutes les entrées de la couche — voir `EvalCache`.
        """
        series = {
            tf: _tail(
                list(candles.get(tf, [])) if already_closed
                else closed(candles.get(tf, []), tf, now_ms),
                self._window_bars(tf),
            )
            for tf in TIMEFRAMES
        }
        bars_15m = series["15m"]
        if not bars_15m:
            return Decision(bar_ts=0, verdicts={}, state=state)

        bar_ts = int(bars_15m[-1]["ts"])
        new_state = _copy_state(state)
        reading = macro_reading if macro_reading is not None else resolve(self.macro, now_ms)

        ctx = LayerContext(
            now_ms=now_ms,
            candles=series,
            funding_hourly=funding_hourly,
            macro_risk=reading.risk_level,
            bias_state=new_state.bias,
        )

        decision = Decision(bar_ts=bar_ts, state=new_state, macro=reading)

        # Idempotence §8 : une bougie déjà évaluée ne redonne pas de signal.
        # L'hystérésis du biais, elle, est protégée séparément par son propre
        # `last_bar_ts` — c'est pourquoi on peut sortir ici sans rien fausser.
        if bar_ts <= state.last_eval_bar_ts:
            decision.verdicts["15m"] = veto(
                f"bougie 15m {bar_ts} déjà évaluée (idempotence §8)", utc(bar_ts))
            return decision
        new_state.last_eval_bar_ts = bar_ts

        # ── 1d ──
        v_bias = self._cached(cache, "1d", series["1d"], ctx,
                              lambda: self.bias_layer.evaluate(series["1d"], ctx))
        decision.verdicts["1d"] = v_bias
        # L'hystérésis avance même quand la couche oppose son veto : c'est ce
        # qui fait qu'un biais met bien 2 clôtures à basculer, et non 2 clôtures
        # *autorisées*.
        if "bias_state" in v_bias.data:
            new_state.bias = v_bias.data["bias_state"]
            ctx = ctx.with_(bias_state=new_state.bias)
        if not v_bias.passed:
            self._log(decision)
            return decision
        ctx = ctx.with_(bias=v_bias.data.get("bias", Bias.FLAT))

        # ── 1h ──
        v_regime = self._cached(cache, "1h", series["1h"], ctx,
                                lambda: self.regime_layer.evaluate(series["1h"], ctx))
        decision.verdicts["1h"] = v_regime
        if not v_regime.passed:
            self._log(decision)
            return decision
        ctx = ctx.with_(
            regime=v_regime.data.get("regime"),
            direction=v_regime.data.get("direction"),
            atr_1h=v_regime.data.get("atr_1h"),
        )

        # ── 15m ──
        v_timing = self.timing_layer.evaluate(series["15m"], ctx)
        decision.verdicts["15m"] = v_timing
        if not v_timing.passed:
            self._log(decision)
            return decision

        # ── Risque §6, sous les paramètres adaptatifs §12 ──
        # Le conditionnement (§12.3) se fait ICI et pas plus tôt : il a besoin
        # du percentile de volatilité que la couche 1h vient de calculer, et il
        # ne touche que des paramètres de risque, lus en aval. D'où l'absence
        # de boucle de rétroaction — cf. `RegimeConditioner.assert_no_feedback`.
        risk_cfg, adaptive_note = self._risk_config(v_regime)
        decision.adaptive = adaptive_note
        decision.risk_config = risk_cfg
        if adaptive_note and adaptive_note.get("observation_mode"):
            decision.risk_verdict = veto(
                "mode observation (§12.4/§6.5) : signaux loggés, aucun ordre",
                utc(bar_ts), **adaptive_note)
            self._log(decision)
            return decision

        decision.risk_verdict, decision.guards, decision.sizing = self._check_risk(
            ctx, v_timing, new_state, equity, now_ms, bar_ts, risk_cfg)
        if not decision.risk_verdict.passed:
            self._log(decision)
            return decision

        side: Side = v_timing.data["side"]
        entry_ref = float(v_timing.data["entry_ref"])
        atr_1h = float(ctx.atr_1h or 0.0)
        decision.signal = ConfluenceSignal(
            side=side,
            entry_zone=tuple(v_timing.data["entry_zone"]),          # type: ignore[arg-type]
            stop_price=RiskManager(risk_cfg).stop_price(entry_ref, side, atr_1h),
            atr_1h=atr_1h,
            verdicts=dict(decision.verdicts),
            expires_at=v_timing.data["expires_at"],
            entry_ref=entry_ref,
            bar_ts=bar_ts,
        )
        self._log(decision)
        return decision

    def _window_bars(self, timeframe: str) -> int:
        return {
            "1d": self.cfg.bias_1d.window_bars,
            "1h": self.cfg.regime_1h.window_bars,
            "15m": self.cfg.timing_15m.window_bars,
            "1m": 200,
        }[timeframe]

    @staticmethod
    def _cached(cache: Optional["EvalCache"], layer: str, series: List[dict],
                ctx: LayerContext, compute) -> LayerVerdict:
        if cache is None or not series:
            return compute()
        key = cache.key(layer, series, ctx)
        hit = cache.get(key)
        if hit is not None:
            return hit
        verdict = compute()
        cache.put(key, verdict)
        return verdict

    def _risk_config(self, v_regime: LayerVerdict):
        """Section risque effective de ce cycle, via l'APM si présent (§12).

        Ne lève jamais : une panne de l'APM fait retomber sur la config figée
        plutôt que d'interrompre la décision. Le §12.8 l'exige — « le
        ConfluenceAgent obtient toujours un jeu de paramètres valide ».
        """
        if self.params is None:
            return self.cfg.risk, None
        try:
            effective = self.params.effective(v_regime.data.get("atr_percentile"))
            return effective.config.risk, effective.as_log()
        except Exception as exc:                     # noqa: BLE001 — repli obligatoire
            logger.error("APM indisponible (%r) — repli sur la config figée", exc)
            return self.cfg.risk, {"degraded": True, "error": repr(exc)}

    def _check_risk(self, ctx: LayerContext, v_timing: LayerVerdict,
                    state: AgentState, equity: float, now_ms: int, bar_ts: int,
                    risk_cfg=None):
        at = utc(bar_ts)
        side: Side = v_timing.data["side"]
        entry_ref = float(v_timing.data["entry_ref"])
        atr_1h = float(ctx.atr_1h or 0.0)
        risk = RiskManager(risk_cfg) if risk_cfg is not None else self.risk
        risk_cfg = risk.cfg

        if atr_1h <= 0:
            return veto("ATR_1h nul: stop incalculable", at), None, None

        # §6.5 filtre d'edge minimal — avant tout le reste, c'est le filtre qui
        # répond au diagnostic frais du §1.
        edge_ok, edge_detail = risk.edge_ok(entry_ref, atr_1h)
        if not edge_ok:
            return veto(
                f"edge insuffisant: mouvement espéré {edge_detail['expected_move']:.2f} "
                f"< {risk_cfg.edge_multiple:g}× frais aller-retour "
                f"({edge_detail['required_move']:.2f})", at, **edge_detail), None, None

        guards = risk.check_guards(state.guards, now_ms, bar_ts=bar_ts)
        if not guards.allowed:
            return veto(guards.reason, at, **edge_detail, **guards.detail), guards, None

        if state.open_position is not None:
            return (veto("position déjà ouverte", at, **edge_detail, **guards.detail),
                    guards, None)

        stop = risk.stop_price(entry_ref, side, atr_1h)
        sizing = risk.size(equity, entry_ref, stop)
        if not sizing.valid:
            return (veto(f"taille nulle (equity={equity:.2f})", at,
                         **edge_detail, **guards.detail), guards, sizing)

        return (ok(f"risque OK: {sizing.size:.6f} unités, "
                   f"notionnel {sizing.notional:.0f} $, risque {sizing.risk_usd:.2f} $",
                   at, **edge_detail, **guards.detail,
                   size=sizing.size, notional=sizing.notional, capped_by=sizing.capped_by),
                guards, sizing)

    # ── Mises à jour d'état (mutantes, appelées par l'exécutant) ────────────

    def on_entry(self, state: AgentState, signal: ConfluenceSignal,
                 fill_price: float, size: float, now_ms: int) -> AgentState:
        self.risk.register_entry(state.guards, now_ms, bar_ts=signal.bar_ts)
        trail = self.trailing.open(signal.side, fill_price, signal.stop_price)
        state.open_position = {
            "side": signal.side.name,
            "entry": fill_price,
            "size": size,
            "initial_stop": signal.stop_price,
            "stop": trail.stop,
            "peak": trail.peak,
            "activated": trail.activated,
            "opened_ms": now_ms,
            "bar_ts": signal.bar_ts,
        }
        return state

    def on_exit(self, state: AgentState, gross_pnl: float, fees: float,
                now_ms: int, reason: str, funding: float = 0.0) -> AgentState:
        side = (state.open_position or {}).get("side", "")
        self.risk.register_exit(state.guards, ClosedTrade(
            closed_ms=now_ms, gross_pnl=gross_pnl, fees=fees, funding=funding,
            side=side, reason=reason))
        state.open_position = None
        return state

    def bias_invalidated(self, state: AgentState, decision: Decision) -> Optional[str]:
        """§6.4 — la couche 1d ou 1h a-t-elle basculé CONTRE la position ouverte ?

        On lit le biais CONFIRMÉ (celui qui a déjà passé ses 2 clôtures), pas le
        biais brut : le §6.4 exige les mêmes 2 clôtures de confirmation pour
        fermer que pour ouvrir, sans quoi on sortirait sur le premier
        franchissement d'EMA — c'est-à-dire souvent, et au pire moment.
        """
        pos = state.open_position
        if not pos:
            return None
        side = Side[pos["side"]]

        v_bias = decision.verdicts.get("1d")
        if v_bias is not None:
            confirmed = v_bias.data.get("confirmed_bias")
            if isinstance(confirmed, Bias) and confirmed is not Bias.FLAT:
                if confirmed.value != side.sign:
                    return f"biais 1d passé à {confirmed.name}, contre la position {side.name}"

        v_regime = decision.verdicts.get("1h")
        if v_regime is not None:
            direction = v_regime.data.get("direction")
            regime = v_regime.data.get("regime")
            if regime is Regime.TREND and isinstance(direction, Side) and direction is not side:
                return f"tendance 1h passée à {direction.name}, contre la position {side.name}"
        return None

    # ── I/O explicite ───────────────────────────────────────────────────────

    def load_state(self) -> AgentState:
        return self.store.load() if self.store else AgentState()

    def save_state(self, state: AgentState) -> None:
        if self.store:
            self.store.save(state)

    def _log(self, decision: Decision) -> None:
        """Log structuré JSON de CHAQUE évaluation (§8), trade ou pas.

        Le garde `isEnabledFor` n'est pas une micro-optimisation : un backtest
        de 3 ans fait 105 000 évaluations, et sérialiser autant de dictionnaires
        pour les jeter coûte plus cher que la stratégie elle-même. En live, où
        le niveau INFO est actif, le log est émis intégralement.
        """
        if not logger.isEnabledFor(logging.INFO):
            return
        try:
            logger.info(json.dumps(decision.as_log(), ensure_ascii=False, default=str))
        except (TypeError, ValueError) as exc:      # pragma: no cover
            logger.warning("log structuré impossible: %r", exc)


class EvalCache:
    """Mémoïsation des verdicts 1d et 1h.

    Une couche ne peut changer d'avis qu'à la clôture de SA bougie : le verdict
    1h est identique pour les quatre réveils 15m d'une même heure. Le backtest
    passe ainsi de 105 000 calculs d'ADX à 26 000.

    La sûreté tient entièrement à la clé, qui doit couvrir **toutes** les
    entrées de la couche : le ts de sa dernière bougie, plus les champs du
    contexte qu'elle lit (biais amont, funding, macro, état d'hystérésis). Une
    clé incomplète ferait resservir un verdict périmé — un bug silencieux qui
    ne se verrait qu'en comparant backtest et live.
    """

    def __init__(self) -> None:
        self._store: Dict[tuple, LayerVerdict] = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(layer: str, series: List[dict], ctx: LayerContext) -> tuple:
        last_ts = int(series[-1]["ts"])
        if layer == "1d":
            b = ctx.bias_state
            return (layer, last_ts, len(series), ctx.macro_risk,
                    b.current, b.pending, b.pending_count, b.last_bar_ts)
        if layer == "1h":
            return (layer, last_ts, len(series), ctx.bias, ctx.funding_hourly)
        raise KeyError(f"couche non mémoïsable: {layer}")

    def get(self, key: tuple) -> Optional[LayerVerdict]:
        hit = self._store.get(key)
        if hit is None:
            self.misses += 1
        else:
            self.hits += 1
        return hit

    def put(self, key: tuple, verdict: LayerVerdict) -> None:
        # Une seule entrée par couche suffit : le backtest avance dans le temps
        # et ne revient jamais en arrière. Garder l'historique ferait grossir le
        # cache jusqu'à peser plus cher que les calculs évités.
        layer = key[0]
        self._store = {k: v for k, v in self._store.items() if k[0] != layer}
        self._store[key] = verdict


def _tail(series: List[dict], window: int) -> List[dict]:
    """Fenêtre glissante passée aux couches. Voir `config.WINDOW_SLACK` : c'est
    la MÊME longueur en live et en backtest, sans quoi les EMA amorcées sur
    fenêtre ne rendraient pas les mêmes valeurs des deux côtés."""
    return series[-window:] if len(series) > window else series


def _copy_state(state: AgentState) -> AgentState:
    return AgentState.from_json(state.to_json())


__all__ = ["ConfluenceAgent", "Decision", "DeploymentBlocked", "EvalCache",
           "TIMEFRAMES"]

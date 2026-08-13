"""
AdaptiveParameterManager — assemblage des deux étages. SPEC §12.

Point d'entrée unique du ConfluenceAgent pour obtenir ses paramètres. Sa
propriété la plus importante n'est pas ce qu'il calcule mais ce qu'il garantit :

    `effective()` rend TOUJOURS une configuration valide.

Registre vide, registre corrompu, LLM injoignable, posture aberrante — chaque
panne a un repli, et le repli final est le set neutre embarqué. Le §12.8 l'exige
explicitement, et la raison est structurelle : le ConfluenceAgent est un
empilement de vetos, donc un paramètre manquant ne le rend pas dangereux, il le
rend *muet*. Un bot muet qui croit fonctionner est plus coûteux qu'un bot qui
s'arrête en le disant — d'où le drapeau `degraded` remonté à chaque cycle.

**Ordre d'application**, qui n'est pas commutatif :

1. set actif de la posture courante (étage 2 a choisi la posture, pas les
   nombres) ;
2. conditionnement par le percentile de volatilité (étage 1a) ;
3. plafonds durs du §12.5 — `risk_pct`, `max_trades_per_day` et le kill-switch
   frais ne peuvent JAMAIS être relevés au-dessus du set validé, quelle que
   soit la posture.

L'étape 3 vient en dernier exprès : elle doit s'appliquer au résultat, pas aux
intentions.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from confluence.adaptive.conditioner import RegimeConditioner
from confluence.adaptive.posture import (
    Posture,
    PostureSelector,
    PostureState,
    build_backend,
)
from confluence.adaptive.registry import ParameterSet, ParamRegistry
from confluence.config import ConfigError, ConfluenceConfig, RiskConfig

logger = logging.getLogger("sdm.confluence.adaptive.manager")


@dataclass(frozen=True)
class EffectiveParams:
    """Ce que le ConfluenceAgent reçoit à chaque cycle."""

    config: ConfluenceConfig
    posture: str
    set_version: str
    vol_percentile: Optional[float]
    degraded: bool
    observation_mode: bool
    notes: tuple = ()

    def as_log(self) -> Dict[str, Any]:
        return {
            "posture": self.posture,
            "set_version": self.set_version,
            "vol_percentile": self.vol_percentile,
            "degraded": self.degraded,
            "observation_mode": self.observation_mode,
            "notes": list(self.notes),
        }


class AdaptiveParameterManager:
    def __init__(self, base_cfg: ConfluenceConfig,
                 registry: Optional[ParamRegistry] = None,
                 selector: Optional[PostureSelector] = None,
                 state_path: Optional[Path] = None) -> None:
        self.base_cfg = base_cfg
        adaptive = base_cfg.adaptive
        self.registry = registry or ParamRegistry(
            Path(adaptive.registry_path) if adaptive.registry_path else None).load()
        self.conditioner = RegimeConditioner(
            adaptive.regime_conditioner.vol_percentile_low,
            adaptive.regime_conditioner.vol_percentile_high)

        ps = adaptive.posture_selector
        self.selector = selector if selector is not None else PostureSelector(
            backend=(build_backend(ps.backend, ps.endpoint, ps.model)
                     if ps.enabled else None),
            min_confidence=ps.min_confidence,
            aggressive_confirm_days=ps.aggressive_confirm_days,
            shadow_days=ps.shadow_days,
            enabled=ps.enabled,
        )
        base = Path(os.environ.get("CONFLUENCE_STATE_DIR")
                    or (Path(__file__).resolve().parent.parent / "state"))
        self.state_path = Path(state_path) if state_path else base / "posture_state.json"
        self.posture_state = self._load_posture()
        self._cache: Dict[tuple, ConfluenceConfig] = {}

    # ── Lecture par le ConfluenceAgent ──────────────────────────────────────

    @property
    def posture(self) -> str:
        return self.posture_state.current

    @property
    def observation_mode(self) -> bool:
        return self.registry.observation_mode

    def active_set(self) -> ParameterSet:
        return self.registry.get(self.posture)

    def effective(self, vol_percentile: Optional[float] = None) -> EffectiveParams:
        """Configuration effective de ce cycle. Ne lève jamais."""
        notes = []
        param_set = self.active_set()
        if not param_set.oos_metrics:
            notes.append("set de repli embarqué — JAMAIS validé sur données (§12.8)")

        params = self.conditioner.condition(
            param_set.params, vol_percentile, param_set.conditioning_bounds)
        params = self._apply_hard_caps(params, param_set, notes)

        key = (param_set.version, tuple(sorted(params.items())))
        cfg = self._cache.get(key)
        if cfg is None:
            cfg = self._build_config(params, notes)
            self._cache[key] = cfg
            # Le cache ne sert qu'à éviter de reconstruire la config à chaque
            # bougie 15m ; il est borné parce que le conditionnement produit une
            # valeur continue, donc potentiellement une clé par barre.
            if len(self._cache) > 256:
                self._cache = {key: cfg}

        return EffectiveParams(
            config=cfg,
            posture=self.posture,
            set_version=param_set.version,
            vol_percentile=vol_percentile,
            degraded=self.registry.degraded or not param_set.oos_metrics,
            observation_mode=self.observation_mode,
            notes=tuple(notes),
        )

    def _apply_hard_caps(self, params: Dict[str, float], param_set: ParameterSet,
                         notes: list) -> Dict[str, float]:
        """§12.5 — plafonds que rien ne peut relever.

        Le conditionnement du §12.3 est censé RÉDUIRE le risque en forte
        volatilité. On vérifie qu'il n'a pas fait l'inverse, plutôt que de le
        supposer : une borne saisie à l'envers dans un ParameterSet produirait
        un bot qui augmente sa taille quand le marché s'emballe, et rien
        ailleurs ne l'attraperait.
        """
        out = dict(params)
        nominal_risk = param_set.params.get("risk.risk_pct", self.base_cfg.risk.risk_pct)
        if out.get("risk.risk_pct", nominal_risk) > nominal_risk:
            notes.append(
                f"risk_pct conditionné ({out['risk.risk_pct']:.4f}) au-dessus du nominal "
                f"({nominal_risk:.4f}) — plafonné (§12.5)")
            out["risk.risk_pct"] = nominal_risk

        nominal_trades = param_set.params.get("risk.max_trades_per_day",
                                              self.base_cfg.risk.max_trades_per_day)
        if out.get("risk.max_trades_per_day", nominal_trades) > nominal_trades:
            notes.append("max_trades_per_day conditionné au-dessus du set validé — plafonné")
            out["risk.max_trades_per_day"] = nominal_trades

        # Le kill-switch frais n'est jamais paramétrable par posture : il reste
        # celui du YAML validé.
        out.pop("risk.fee_killswitch_ratio", None)
        return out

    def _build_config(self, params: Mapping[str, float], notes: list) -> ConfluenceConfig:
        cfg = self.base_cfg
        for path, value in params.items():
            try:
                cfg = cfg.replace_path(path, value)
            except ConfigError as exc:
                # Un paramètre inapplicable ne doit pas emporter tout le cycle :
                # on garde la valeur de base et on le dit.
                notes.append(f"paramètre {path} ignoré ({exc})")
                logger.error("APM: %s=%r inapplicable — %s", path, value, exc)
        return cfg

    def risk_for(self, vol_percentile: Optional[float]) -> RiskConfig:
        """Raccourci pour le chemin chaud : seule la section risque dépend du
        conditionnement (§12.3)."""
        return self.effective(vol_percentile).config.risk

    # ── Étage 2 — cycle quotidien ───────────────────────────────────────────

    def daily_posture_cycle(self, digest: Mapping[str, Any],
                            now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """Un passage du PostureSelector. Rend None si ce n'est pas l'heure.

        Le digest est journalisé INTÉGRALEMENT (§12.5) : c'est la seule façon de
        rejouer plus tard pourquoi le modèle a conseillé ce qu'il a conseillé.
        """
        now = now or datetime.now(timezone.utc)
        if not self.selector.due(self.posture_state, now):
            return None

        logger.info("posture: digest %s", json.dumps(dict(digest), ensure_ascii=False,
                                                     default=str))
        advice = self.selector.ask(digest, now)
        before = self.posture_state.current
        outcome = self.selector.apply(self.posture_state, advice, now,
                                      observation_mode=self.observation_mode)
        self._save_posture()
        if outcome.get("applied") or outcome.get("alert"):
            self.registry.record_posture(before, self.posture_state.current,
                                         source="posture_selector",
                                         detail={k: v for k, v in outcome.items()
                                                 if k != "advice"},
                                         now=now)
        logger.info("posture: %s", outcome.get("reason"))
        return outcome

    def force_posture(self, posture: str, reason: str,
                      now: Optional[datetime] = None) -> None:
        """Changement manuel — le seul chemin autorisé vers plus agressif sans
        les trois avis, et il est humain, tracé, et jamais déclenché par le LLM."""
        if posture not in (Posture.DEFENSIVE, Posture.NEUTRAL, Posture.AGGRESSIVE):
            raise ValueError(f"posture inconnue: {posture!r}")
        before = self.posture_state.current
        self.posture_state.current = posture
        self.posture_state.pending, self.posture_state.pending_count = None, 0
        self._save_posture()
        self.registry.record_posture(before, posture, source="human",
                                     detail={"reason": reason}, now=now)

    def shadow_report(self) -> Dict[str, Any]:
        return self.selector.shadow_report(self.posture_state)

    # ── Persistance ─────────────────────────────────────────────────────────

    def _load_posture(self) -> PostureState:
        if not self.state_path.exists():
            return PostureState()
        try:
            return PostureState.from_json(
                json.loads(self.state_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.error("APM: état de posture illisible (%s) — retour à neutral", exc)
            return PostureState()

    def _save_posture(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(self.posture_state.to_json(), ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError as exc:
            logger.error("APM: état de posture non écrit (%s)", exc)


def build_digest(metrics: Mapping[str, Any], veto_distribution: Any,
                 bias: str, regime: str, vol_percentile: Optional[float],
                 funding_annualized: Optional[float],
                 macro: Mapping[str, Any]) -> Dict[str, Any]:
    """Digest structuré du §12.5 — produit par le bot, jamais du texte externe.

    Cette contrainte est une frontière de sécurité, pas une préférence de
    format : tout champ qu'un tiers pourrait remplir deviendrait un canal
    d'instruction vers le LLM.
    """
    return {
        "window_days": 30,
        "performance": {k: metrics.get(k) for k in
                        ("net_pnl", "profit_factor", "fee_ratio", "trades",
                         "win_rate", "max_drawdown")},
        "veto_distribution": list(veto_distribution)[:10],
        "layers": {"bias_1d": bias, "regime_1h": regime,
                   "vol_percentile": vol_percentile},
        "funding_annualized": funding_annualized,
        "macro": dict(macro),
    }


__all__ = ["AdaptiveParameterManager", "EffectiveParams", "build_digest"]

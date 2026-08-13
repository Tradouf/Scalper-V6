"""
PostureSelector — étage 2, LLM strictement borné. SPEC §12.5 et §12.6.

**Rôle : choisir `defensive | neutral | aggressive`. C'est tout.**

Le LLM ne produit jamais de nombre. Il n'a pas non plus le pouvoir de désactiver
un garde-fou, ni de faire sortir le bot du mode observation. Ce qu'il choisit,
ce sont trois jeux de paramètres que l'étage 1 a déjà validés sur données — et
même ce choix passe par un ratchet asymétrique et un shadow mode.

Le dispositif de sécurité tient en quatre couches, appliquées dans l'ordre :

1. **Schéma** — sortie JSON stricte, sinon un retry unique, puis posture
   inchangée et alerte. Un LLM qui bafouille ne doit pas faire bouger le bot.
2. **Confiance** — `confidence < min_confidence` ⇒ inchangé.
3. **Ratchet asymétrique** — vers plus défensif : immédiat. Vers plus agressif :
   trois avis quotidiens consécutifs identiques. L'asymétrie est le point
   central : se protéger vite coûte peu, se découvrir vite coûte cher.
4. **Plafonds durs** — `risk_pct`, kill-switch frais et `max_trades_per_day`
   restent ceux du set validé. Aucune posture ne peut les relever.

Et par-dessus tout ça, le **shadow mode** (§12.6) : pendant 45 jours par défaut,
les avis sont produits, journalisés et évalués, mais la posture appliquée reste
`neutral`. L'activation réelle se mérite sur pièces.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Protocol

logger = logging.getLogger("sdm.confluence.adaptive.posture")

POSTURE_ORDER = ("defensive", "neutral", "aggressive")


class Posture:
    DEFENSIVE = "defensive"
    NEUTRAL = "neutral"
    AGGRESSIVE = "aggressive"

    @staticmethod
    def rank(posture: str) -> int:
        return POSTURE_ORDER.index(posture)

    @staticmethod
    def is_more_aggressive(new: str, current: str) -> bool:
        return Posture.rank(new) > Posture.rank(current)


class LLMBackend(Protocol):
    """Interface commune (§12.5). Le backend est un détail d'hébergement ;
    aucune règle de décision ne doit en dépendre."""

    def complete(self, prompt: str) -> str:
        ...


class LocalAIBackend:
    """LocalAI ou tout endpoint OpenAI-compatible (QUEEN, port 8080)."""

    def __init__(self, endpoint: str, model: str, timeout: float = 60.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout = timeout

    def complete(self, prompt: str) -> str:
        import requests

        resp = requests.post(
            f"{self.endpoint}/chat/completions",
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


class AnthropicBackend:
    """API externe. Même interface, mêmes bornes — le PostureSelector ne sait
    pas quel backend il interroge, et c'est voulu."""

    def __init__(self, model: str, timeout: float = 60.0,
                 max_tokens: int = 512) -> None:
        self.model = model or "claude-sonnet-5"
        self.timeout = timeout
        self.max_tokens = max_tokens

    def complete(self, prompt: str) -> str:
        import anthropic

        client = anthropic.Anthropic(timeout=self.timeout)
        message = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in message.content
                       if getattr(block, "type", "") == "text")


@dataclass(frozen=True)
class PostureAdvice:
    """Un avis, tel qu'il sort du LLM et tel qu'il est journalisé."""

    posture: Optional[str]
    confidence: float
    rationale: str
    raw: str = ""
    valid: bool = True
    error: str = ""
    at: Optional[datetime] = None

    def to_json(self) -> Dict[str, Any]:
        out = asdict(self)
        out["at"] = self.at.isoformat() if self.at else None
        return out


@dataclass
class PostureState:
    """État persistant du sélecteur (rechargé au restart)."""

    current: str = Posture.NEUTRAL
    pending: Optional[str] = None
    pending_count: int = 0
    last_run_day: str = ""
    shadow_started: str = ""
    advices: List[Dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> Dict[str, Any]:
        return {
            "current": self.current,
            "pending": self.pending,
            "pending_count": self.pending_count,
            "last_run_day": self.last_run_day,
            "shadow_started": self.shadow_started,
            "advices": self.advices[-400:],     # borné : le journal fait foi
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "PostureState":
        return cls(
            current=str(raw.get("current", Posture.NEUTRAL)),
            pending=raw.get("pending"),
            pending_count=int(raw.get("pending_count", 0)),
            last_run_day=str(raw.get("last_run_day", "")),
            shadow_started=str(raw.get("shadow_started", "")),
            advices=list(raw.get("advices", [])),
        )


PROMPT_TEMPLATE = """Tu choisis la POSTURE de risque d'un bot de trading pour les 24 prochaines heures.

Tu ne produis AUCUNE valeur numérique de paramètre. Tu choisis exactement une
posture parmi trois jeux de paramètres déjà validés statistiquement :
- "defensive"  : moins de trades, stops plus larges, exigence d'edge renforcée
- "neutral"    : réglage nominal
- "aggressive" : exigence d'edge assouplie

Digest des 30 derniers jours et de l'état courant :
{digest}

Réponds UNIQUEMENT par un objet JSON, sans texte autour :
{{"posture": "defensive"|"neutral"|"aggressive", "confidence": 0.0-1.0, "rationale": "une phrase"}}
"""


class PostureSelector:
    """Étage 2. Une exécution par jour, après clôture daily UTC (§12.5)."""

    def __init__(self, backend: Optional[LLMBackend], min_confidence: float = 0.6,
                 aggressive_confirm_days: int = 3, shadow_days: int = 45,
                 enabled: bool = True) -> None:
        self.backend = backend
        self.min_confidence = min_confidence
        self.aggressive_confirm_days = max(1, aggressive_confirm_days)
        self.shadow_days = shadow_days
        self.enabled = enabled

    # -- cadence -------------------------------------------------------------

    def due(self, state: PostureState, now: datetime) -> bool:
        """Un seul passage par jour UTC, jamais en intra-journée (§12.5)."""
        return state.last_run_day != now.astimezone(timezone.utc).strftime("%Y-%m-%d")

    def in_shadow(self, state: PostureState, now: datetime) -> bool:
        if not state.shadow_started:
            return True
        try:
            started = datetime.fromisoformat(state.shadow_started)
        except ValueError:
            return True
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return (now - started).days < self.shadow_days

    # -- appel LLM -----------------------------------------------------------

    def ask(self, digest: Mapping[str, Any], now: datetime) -> PostureAdvice:
        """Interroge le backend, avec UN retry sur sortie invalide (§12.5).

        Le digest est structuré et produit par le bot : jamais de texte libre
        externe non contrôlé. C'est ce qui empêche qu'une manchette d'actualité
        ou un champ rempli par un tiers devienne une instruction.
        """
        if self.backend is None:
            return PostureAdvice(None, 0.0, "", valid=False,
                                 error="aucun backend LLM configuré", at=now)

        prompt = PROMPT_TEMPLATE.format(digest=json.dumps(digest, ensure_ascii=False, indent=2))
        last_error = ""
        for attempt in (1, 2):
            try:
                raw = self.backend.complete(prompt)
            except Exception as exc:                     # noqa: BLE001 — réseau/backend
                last_error = f"backend indisponible: {exc!r}"
                logger.warning("posture: tentative %d — %s", attempt, last_error)
                continue
            advice = self.parse(raw, now)
            if advice.valid:
                return advice
            last_error = advice.error
            logger.warning("posture: tentative %d — sortie invalide (%s)", attempt, last_error)
        return PostureAdvice(None, 0.0, "", raw="", valid=False,
                             error=last_error or "sortie invalide", at=now)

    def parse(self, raw: str, now: Optional[datetime] = None) -> PostureAdvice:
        """Validation stricte du JSON attendu (§12.5).

        On tolère qu'un modèle encadre son JSON de texte (le premier objet
        accolade-à-accolade est extrait), mais pas qu'il invente une posture ou
        omette la confiance. La tolérance porte sur la mise en forme, jamais sur
        le contenu.
        """
        text = (raw or "").strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return PostureAdvice(None, 0.0, "", raw=text, valid=False,
                                 error="aucun objet JSON dans la réponse", at=now)
        try:
            doc = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            return PostureAdvice(None, 0.0, "", raw=text, valid=False,
                                 error=f"JSON illisible: {exc}", at=now)
        if not isinstance(doc, dict):
            return PostureAdvice(None, 0.0, "", raw=text, valid=False,
                                 error="racine JSON non objet", at=now)

        posture = doc.get("posture")
        if posture not in POSTURE_ORDER:
            return PostureAdvice(None, 0.0, "", raw=text, valid=False,
                                 error=f"posture hors énumération: {posture!r}", at=now)
        try:
            confidence = float(doc.get("confidence"))
        except (TypeError, ValueError):
            return PostureAdvice(None, 0.0, "", raw=text, valid=False,
                                 error="confidence absente ou non numérique", at=now)
        if not 0.0 <= confidence <= 1.0:
            return PostureAdvice(None, 0.0, "", raw=text, valid=False,
                                 error=f"confidence hors [0,1]: {confidence}", at=now)

        return PostureAdvice(posture=posture, confidence=confidence,
                             rationale=str(doc.get("rationale", ""))[:500],
                             raw=text, valid=True, at=now)

    # -- décision ------------------------------------------------------------

    def apply(self, state: PostureState, advice: PostureAdvice, now: datetime,
              observation_mode: bool = False) -> Dict[str, Any]:
        """Applique un avis à l'état, sous toutes les bornes du §12.5.

        Rend un compte rendu de ce qui a été fait ET pourquoi — c'est ce que le
        registre journalise. Un changement de posture sans motif enregistré est
        indébuggable trois mois plus tard.
        """
        before = state.current
        state.last_run_day = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        if not state.shadow_started:
            state.shadow_started = now.astimezone(timezone.utc).isoformat()
        state.advices.append(advice.to_json())

        result: Dict[str, Any] = {
            "from": before, "to": before, "applied": False,
            "shadow": self.in_shadow(state, now),
            "advice": advice.to_json(),
        }

        if not self.enabled:
            result["reason"] = "PostureSelector désactivé (§12.7 enabled=false)"
            return result

        # Borne 4 : le LLM ne peut JAMAIS relever le bot du mode observation.
        if observation_mode:
            result["reason"] = ("mode observation actif — le PostureSelector ne peut pas "
                                "en faire sortir le bot (§12.5)")
            return result

        if not advice.valid:
            result["reason"] = f"avis invalide ({advice.error}) — posture inchangée + alerte"
            result["alert"] = True
            logger.error("posture: %s", result["reason"])
            return result

        if advice.confidence < self.min_confidence:
            result["reason"] = (f"confiance {advice.confidence:.2f} < "
                                f"{self.min_confidence:.2f} — posture inchangée")
            return result

        target = advice.posture
        if target == before:
            state.pending, state.pending_count = None, 0
            result["reason"] = "avis conforme à la posture courante"
            return result

        # Borne 3 : ratchet asymétrique.
        if Posture.is_more_aggressive(target, before):
            if state.pending == target:
                state.pending_count += 1
            else:
                state.pending, state.pending_count = target, 1
            if state.pending_count < self.aggressive_confirm_days:
                result["reason"] = (
                    f"passage vers {target} (plus agressif) : "
                    f"{state.pending_count}/{self.aggressive_confirm_days} avis consécutifs")
                result["pending"] = target
                return result
            result["reason"] = (f"passage vers {target} confirmé sur "
                                f"{self.aggressive_confirm_days} avis consécutifs")
        else:
            result["reason"] = f"passage vers {target} (plus défensif) appliqué immédiatement"

        state.pending, state.pending_count = None, 0

        # §12.6 : en shadow mode, l'avis est retenu et évalué, jamais appliqué.
        if self.in_shadow(state, now):
            result["reason"] += " — NON APPLIQUÉ (shadow mode §12.6)"
            result["would_be"] = target
            return result

        state.current = target
        result["to"] = target
        result["applied"] = True
        return result

    # -- §12.6 rapport de shadow mode ---------------------------------------

    def shadow_report(self, state: PostureState) -> Dict[str, Any]:
        """Bilan de la période d'ombre : l'étage 2 s'active sur pièces.

        Le §12.6 exige que le simulé soit ≥ au réel ET qu'aucun avis n'ait
        violé le schéma. Ce rapport fournit le second point et l'inventaire des
        avis ; la comparaison de PnL, elle, se fait sur les postures rejouées
        par le backtest — un LLM ne peut pas s'auto-évaluer.
        """
        total = len(state.advices)
        invalid = [a for a in state.advices if not a.get("valid", True)]
        distribution: Dict[str, int] = {}
        for advice in state.advices:
            key = advice.get("posture") or "invalide"
            distribution[key] = distribution.get(key, 0) + 1
        return {
            "advices": total,
            "schema_violations": len(invalid),
            "schema_clean": not invalid,
            "distribution": distribution,
            "eligible_for_activation": bool(total) and not invalid,
            "note": ("§12.6 : activation seulement si le PnL simulé des postures LLM est "
                     "≥ au PnL réel sur la période ET si aucun avis n'a violé le schéma"),
        }


def build_backend(kind: str, endpoint: str = "", model: str = "") -> Optional[LLMBackend]:
    if kind == "localai":
        if not endpoint:
            logger.error("posture: backend localai sans endpoint — étage 2 désactivé")
            return None
        return LocalAIBackend(endpoint, model)
    if kind == "anthropic":
        return AnthropicBackend(model)
    logger.error("posture: backend inconnu %r — étage 2 désactivé", kind)
    return None


__all__ = ["AnthropicBackend", "LLMBackend", "LocalAIBackend", "POSTURE_ORDER",
           "Posture", "PostureAdvice", "PostureSelector", "PostureState",
           "build_backend"]

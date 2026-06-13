"""
RiskGovernor — agent LLM qui ADAPTE les paramètres de risque au régime (2026-06-13).

Motivation : V7 était 100% règles figées. Le seuil emergency fixe (-2.2% ROE =
-0.73% prix à 3x) guillotinait chaque position sur du bruit avant que la
stratégie ne joue → bleed -8.7%/semaine. Personne n'ajustait : l'audit Opus
(6h, lecture seule) ne faisait que constater.

Ce gouverneur tourne en boucle (cadence GOVERNOR_INTERVAL_SEC), lit l'état du
marché, demande à un LLM de choisir les bons paramètres de risque, et les
applique LIVE — mais TOUJOURS clampés à des bornes dures en code. Le LLM ne peut
jamais sortir de la zone sûre ; s'il est indisponible ou répond n'importe quoi,
on retombe sur un défaut prudent. Les freins catastrophe (kill_switch -10% DD,
daily_loss_limit -3%) restent EN DUR, hors de portée du LLM.

Knobs gouvernés (v1) :
  - emergency_exit_roe_pct  ∈ [0.030, 0.060]  (plancher 3% > l'ancien 2.2%)
  - size_mult (×notional des stratégies) ∈ [0.5, 1.2]

Sortie persistée : memory/risk_overrides.json (décision + raison + features).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional

import requests

logger = logging.getLogger("v7.governor")

REPO = Path(__file__).resolve().parent.parent
OVERRIDES_PATH = REPO / "memory" / "risk_overrides.json"

# ── Bornes DURES (le LLM ne peut JAMAIS en sortir) ───────────────────────────
EMERGENCY_MIN = 0.030     # plancher 3% : garantit plus large que l'ancien 2.2%
EMERGENCY_MAX = 0.060
SIZE_MULT_MIN = 0.5
SIZE_MULT_MAX = 1.2

# Défaut prudent si le LLM est indisponible / réponse invalide : on N'revient
# PAS à 2.2% (la cause du bleed) — on tient un seuil élargi sûr.
SAFE_DEFAULT = {"emergency_roe_pct": 0.040, "size_mult": 1.0,
                "reason": "défaut prudent (LLM indisponible)"}

SYSTEM_PROMPT = """Tu es le gouverneur de risque d'un bot de trading crypto sur Hyperliquid (levier 3x).
Ton rôle : choisir les paramètres de risque adaptés à l'état du marché, à chaque cycle.

Tu reçois des features de marché. Tu réponds UNIQUEMENT en JSON strict, en
COMMENÇANT par l'accolade, "reason" en 12 mots MAXIMUM :
{"emergency_roe_pct": <float>, "size_mult": <float>, "reason": "<12 mots max>"}

Règles de bon sens :
- emergency_roe_pct = seuil de perte (ROE) où une position est force-fermée.
  À 3x, emergency_roe_pct/3 = le mouvement de prix qui déclenche.
  Un seuil TROP serré coupe les positions sur du bruit avant que la stratégie
  (mean-reversion, supertrend, grille) ne se réalise → saignée par mille coupures.
  * Range calme / vol faible : ÉLARGIR (0.045-0.055) pour laisser respirer.
  * High-vol / tendance violente : RESSERRER (0.030-0.038) pour limiter les pertes.
- size_mult = multiplicateur de taille des positions.
  * Conditions favorables et stables : proche de 1.0-1.2.
  * Vol élevée, pertes récentes, incertitude : réduire (0.5-0.8).
- Bornes dures appliquées ensuite : emergency_roe_pct ∈ [0.030,0.060], size_mult ∈ [0.5,1.2].
Réfléchis au compromis, puis renvoie le JSON. Aucun texte hors du JSON."""


@dataclass
class GovernorDecision:
    emergency_roe_pct: float
    size_mult: float
    reason: str
    ts: float
    source: str           # "llm" | "fallback" | "clamped"
    raw_llm: Optional[str] = None


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class RiskGovernor:
    def __init__(self, endpoint: str, model: str, timeout: float = 30.0,
                 envelope_provider=None) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._model = model
        self._timeout = timeout
        # envelope_provider() -> dict {max_size_mult, emergency_floor, emergency_ceiling}
        # posé par le stratège Opus. Le tactique INTERSECTE ses bornes dures avec
        # cette enveloppe (Opus resserre, jamais ne desserre au-delà du code).
        self._envelope_provider = envelope_provider
        self._last: Optional[GovernorDecision] = self._load_last()

    def _bounds(self) -> tuple[float, float, float, float]:
        """(emergency_min, emergency_max, size_min, size_max) = code ∩ enveloppe stratège."""
        em_lo, em_hi, sz_lo, sz_hi = EMERGENCY_MIN, EMERGENCY_MAX, SIZE_MULT_MIN, SIZE_MULT_MAX
        if self._envelope_provider is not None:
            try:
                env = self._envelope_provider() or {}
                em_lo = max(em_lo, float(env.get("emergency_floor", em_lo)))
                em_hi = min(em_hi, float(env.get("emergency_ceiling", em_hi)))
                sz_hi = min(sz_hi, float(env.get("max_size_mult", sz_hi)))
                if em_lo > em_hi:          # enveloppe dégénérée → on garde le floor
                    em_hi = em_lo
                if sz_lo > sz_hi:
                    sz_lo = sz_hi
            except Exception:
                pass
        return em_lo, em_hi, sz_lo, sz_hi

    @property
    def last(self) -> Optional[GovernorDecision]:
        return self._last

    # ── LLM ──────────────────────────────────────────────────────────────────
    def _call_llm(self, features: dict) -> Optional[str]:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "Features marché:\n" + json.dumps(features, indent=2)},
            ],
            "temperature": 0.2,
            "max_tokens": 500,
        }
        try:
            r = requests.post(f"{self._endpoint}/v1/chat/completions",
                              json=payload, timeout=self._timeout)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            logger.warning("Governor LLM call échec: %r", e)
            return None

    @staticmethod
    def _parse(text: str) -> Optional[dict]:
        """Extrait le 1er objet JSON du texte (robuste au bavardage du LLM)."""
        if not text:
            return None
        import re
        # Greedy : 1er '{' au dernier '}' (qwen peut préfixer du <think>… ; on
        # veut l'objet complet, pas le 1er sous-objet tronqué).
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
            return d if ("emergency_roe_pct" in d and "size_mult" in d) else None
        except Exception:
            return None

    # ── Décision ─────────────────────────────────────────────────────────────
    def decide(self, features: dict) -> GovernorDecision:
        raw = self._call_llm(features)
        parsed = self._parse(raw) if raw else None
        if parsed is None:
            # Fallback : on garde la dernière bonne décision si on en a une,
            # sinon le défaut prudent. Jamais de retour au 2.2% d'origine.
            base = self._last if self._last else None
            dec = GovernorDecision(
                emergency_roe_pct=(base.emergency_roe_pct if base else SAFE_DEFAULT["emergency_roe_pct"]),
                size_mult=(base.size_mult if base else SAFE_DEFAULT["size_mult"]),
                reason="LLM indisponible/invalide → maintien dernier réglage sûr",
                ts=time.time(), source="fallback", raw_llm=raw,
            )
            self._persist(dec, features)
            return dec

        em_lo, em_hi, sz_lo, sz_hi = self._bounds()
        em = _clamp(float(parsed["emergency_roe_pct"]), em_lo, em_hi)
        sm = _clamp(float(parsed["size_mult"]), sz_lo, sz_hi)
        clamped = (em != float(parsed["emergency_roe_pct"]) or sm != float(parsed["size_mult"]))
        dec = GovernorDecision(
            emergency_roe_pct=round(em, 4), size_mult=round(sm, 3),
            reason=str(parsed.get("reason", ""))[:200],
            ts=time.time(), source=("clamped" if clamped else "llm"), raw_llm=raw,
        )
        self._last = dec
        self._persist(dec, features)
        logger.info("Governor décision: emergency=%.3f size_mult=%.2f [%s] — %s",
                    dec.emergency_roe_pct, dec.size_mult, dec.source, dec.reason)
        return dec

    # ── Persistance ──────────────────────────────────────────────────────────
    def _persist(self, dec: GovernorDecision, features: dict) -> None:
        try:
            OVERRIDES_PATH.parent.mkdir(exist_ok=True)
            OVERRIDES_PATH.write_text(json.dumps(
                {"decision": asdict(dec), "features": features}, indent=2, default=str))
        except Exception as e:
            logger.debug("persist risk_overrides: %r", e)

    def _load_last(self) -> Optional[GovernorDecision]:
        if not OVERRIDES_PATH.exists():
            return None
        try:
            d = json.loads(OVERRIDES_PATH.read_text())["decision"]
            return GovernorDecision(**{k: d[k] for k in (
                "emergency_roe_pct", "size_mult", "reason", "ts", "source") if k in d},
                raw_llm=d.get("raw_llm"))
        except Exception:
            return None

"""
Strategist — étage STRATÈGE du gouverneur (Opus, cadence lente) (2026-06-13).

Architecture à deux étages (décision francois) :
  - Tactique : qwen local (risk_governor.py), cadence 15min, choisit emergency
    + taille DANS l'enveloppe posée par le stratège.
  - Stratège : Opus via `claude -p`, cadence lente (1h par défaut), coût
    négligeable (~5s/appel). Lit un contexte plus large (trajectoire equity,
    historique régime, fréquence des emergencies) et pose l'ENVELOPPE de risque :
    posture + bornes que le tactique doit respecter.

Opus peut RESSERRER, jamais desserrer au-delà des bornes dures du code. Le code
reste le mur extérieur ; Opus pose une boîte à l'intérieur ; qwen bouge dans la
boîte. En cas d'échec Opus → enveloppe neutre (pleine latitude code).

Sortie : memory/strategy_posture.json (enveloppe + raisonnement).
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger("v7.strategist")

REPO = Path(__file__).resolve().parent.parent
POSTURE_PATH = REPO / "memory" / "strategy_posture.json"

# Bornes DURES (le stratège ne peut pas en sortir — mêmes murs que le tactique).
EMERGENCY_MIN = 0.030
EMERGENCY_MAX = 0.060
SIZE_MULT_MIN = 0.5
SIZE_MULT_MAX = 1.2

# Enveloppe neutre = pleine latitude code (si Opus indisponible).
NEUTRAL = {
    "risk_posture": "neutral",
    "max_size_mult": SIZE_MULT_MAX,
    "emergency_floor": EMERGENCY_MIN,
    "emergency_ceiling": EMERGENCY_MAX,
    "commentary": "enveloppe neutre (stratège indisponible)",
}

PROMPT_TMPL = """Tu es le STRATÈGE de risque d'un bot de trading crypto Hyperliquid (levier 3x).
Un gouverneur tactique (LLM rapide, toutes les 15min) ajuste finement le seuil de
stop d'urgence et la taille des positions. TON rôle, moins fréquent : poser
l'ENVELOPPE de risque dans laquelle le tactique a le droit d'opérer, selon la
situation d'ensemble (tendance de l'equity, régime, fréquence des coupures).
Tu APPRENDS de tes enveloppes passées (palmarès ci-dessous) : reproduis ce qui a
donné BON, corrige ce qui a donné MAUVAIS.

{feedback_block}Contexte actuel :
{context}

Décide une posture globale et l'enveloppe correspondante. Réponds UNIQUEMENT en
JSON strict commençant par l'accolade, "commentary" en 15 mots max :
{{"risk_posture": "defensive|neutral|aggressive",
  "max_size_mult": <float 0.5-1.2>,
  "emergency_floor": <float 0.03-0.06>,
  "emergency_ceiling": <float 0.03-0.06>,
  "commentary": "<15 mots max>"}}

Guides :
- equity qui saigne / coupures fréquentes → defensive : taille plafonnée bas
  (0.6-0.8), stops élargis (floor 0.045+) pour cesser de couper sur le bruit.
- conditions saines, equity stable/montante → neutral/aggressive : plus de
  latitude (max_size_mult 1.0-1.2, plage emergency large 0.03-0.06).
- emergency_floor <= emergency_ceiling obligatoire."""


@dataclass
class StrategistDecision:
    risk_posture: str
    max_size_mult: float
    emergency_floor: float
    emergency_ceiling: float
    commentary: str
    ts: float
    source: str            # "opus" | "fallback" | "clamped"


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class Strategist:
    def __init__(self, model: str = "opus", budget_usd: float = 0.5, timeout: float = 90.0) -> None:
        self._model = model
        self._budget = budget_usd
        self._timeout = timeout
        self._last: Optional[StrategistDecision] = self._load_last()

    @property
    def last(self) -> Optional[StrategistDecision]:
        return self._last

    def envelope(self) -> dict:
        """Enveloppe courante pour le tactique (neutre si pas encore de décision)."""
        d = self._last
        if d is None:
            return dict(NEUTRAL)
        return {
            "risk_posture": d.risk_posture,
            "max_size_mult": d.max_size_mult,
            "emergency_floor": d.emergency_floor,
            "emergency_ceiling": d.emergency_ceiling,
            "commentary": d.commentary,
        }

    # ── Appel Opus (claude -p, non-agentique : prompt → JSON) ─────────────────
    def _call_opus(self, prompt: str) -> Optional[str]:
        try:
            r = subprocess.run(
                ["claude", "-p", "--model", self._model,
                 "--max-budget-usd", str(self._budget), prompt],
                capture_output=True, text=True, timeout=self._timeout,
            )
            if r.returncode != 0:
                logger.warning("Strategist Opus rc=%d: %s", r.returncode, (r.stderr or "")[:200])
                return None
            return r.stdout
        except Exception as e:
            logger.warning("Strategist Opus call échec: %r", e)
            return None

    @staticmethod
    def _parse(text: str) -> Optional[dict]:
        if not text:
            return None
        import re
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
            req = ("risk_posture", "max_size_mult", "emergency_floor", "emergency_ceiling")
            return d if all(k in d for k in req) else None
        except Exception:
            return None

    # ── Décision ─────────────────────────────────────────────────────────────
    def decide(self, context: dict, feedback: str = "") -> StrategistDecision:
        fb_block = ("TON HISTORIQUE D'ENVELOPPES (apprends-en) :\n" + feedback + "\n\n") if feedback else ""
        prompt = PROMPT_TMPL.format(context=json.dumps(context, indent=2), feedback_block=fb_block)
        raw = self._call_opus(prompt)
        parsed = self._parse(raw) if raw else None
        if parsed is None:
            base = self._last if self._last else None
            env = (self.envelope() if base else NEUTRAL)
            dec = StrategistDecision(
                risk_posture=env["risk_posture"], max_size_mult=env["max_size_mult"],
                emergency_floor=env["emergency_floor"], emergency_ceiling=env["emergency_ceiling"],
                commentary="Opus indisponible → maintien enveloppe précédente",
                ts=time.time(), source="fallback",
            )
            self._persist(dec, context)
            return dec

        floor = _clamp(float(parsed["emergency_floor"]), EMERGENCY_MIN, EMERGENCY_MAX)
        ceil = _clamp(float(parsed["emergency_ceiling"]), EMERGENCY_MIN, EMERGENCY_MAX)
        if floor > ceil:
            floor, ceil = ceil, floor
        sm = _clamp(float(parsed["max_size_mult"]), SIZE_MULT_MIN, SIZE_MULT_MAX)
        posture = str(parsed.get("risk_posture", "neutral"))
        clamped = (floor != float(parsed["emergency_floor"]) or
                   ceil != float(parsed["emergency_ceiling"]) or
                   sm != float(parsed["max_size_mult"]))
        dec = StrategistDecision(
            risk_posture=posture, max_size_mult=round(sm, 3),
            emergency_floor=round(floor, 4), emergency_ceiling=round(ceil, 4),
            commentary=str(parsed.get("commentary", ""))[:200],
            ts=time.time(), source=("clamped" if clamped else "opus"),
        )
        self._last = dec
        self._persist(dec, context)
        logger.info("Strategist [%s] posture=%s size<=%.2f emergency[%.3f,%.3f] — %s",
                    dec.source, dec.risk_posture, dec.max_size_mult,
                    dec.emergency_floor, dec.emergency_ceiling, dec.commentary)
        return dec

    # ── Persistance ──────────────────────────────────────────────────────────
    def _persist(self, dec: StrategistDecision, context: dict) -> None:
        try:
            POSTURE_PATH.parent.mkdir(exist_ok=True)
            POSTURE_PATH.write_text(json.dumps(
                {"decision": asdict(dec), "context": context}, indent=2, default=str))
        except Exception as e:
            logger.debug("persist strategy_posture: %r", e)

    def _load_last(self) -> Optional[StrategistDecision]:
        if not POSTURE_PATH.exists():
            return None
        try:
            d = json.loads(POSTURE_PATH.read_text())["decision"]
            return StrategistDecision(**{k: d[k] for k in (
                "risk_posture", "max_size_mult", "emergency_floor",
                "emergency_ceiling", "commentary", "ts", "source") if k in d})
        except Exception:
            return None

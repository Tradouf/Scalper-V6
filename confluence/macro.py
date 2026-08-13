"""
MacroRegimeAgent — SPEC §2 et §4.1.

Cité par la spec comme existant ; il n'existait pas dans le repo. Son unique
pouvoir dans ce module est un **droit de veto** sur la couche 1d : `EXTREME`
force FLAT. Il ne peut jamais autoriser un trade que le biais 1d refuse.

Le point délicat est la donnée. MVRV et flux d'entrée exchange demandent un
fournisseur on-chain (Glassnode & co.), qu'il n'y a pas dans ce repo. Plutôt
que de simuler ces métriques — un veto macro inventé serait pire que pas de
veto —, l'agent est **enfichable** :

* `provider: none`   → `UNKNOWN` en permanence. Aucun veto, aucun feu vert.
* `provider: file`   → lit un JSON alimenté par un process tiers.

`UNKNOWN` n'est PAS `NORMAL`. Une donnée absente ne doit jamais se lire comme
une confirmation que le risque macro est faible ; elle se lit « ce filtre n'a
rien à dire », et le biais 1d décide seul. Une donnée périmée au-delà de
`max_age_h` retombe sur `UNKNOWN` pour la même raison.

Conséquence à assumer : tant qu'aucun fournisseur n'est branché, le veto macro
du §4.1 est INACTIF. C'est signalé à chaque évaluation dans le log structuré,
pas enterré dans un commentaire.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from confluence.config import MacroConfig
from confluence.types import RiskLevel

logger = logging.getLogger("sdm.confluence.macro")


@dataclass(frozen=True)
class MacroReading:
    risk_level: RiskLevel
    as_of_ms: int
    source: str
    note: str = ""

    @property
    def vetoes(self) -> bool:
        return self.risk_level is RiskLevel.EXTREME


class MacroRegimeAgent:
    def __init__(self, cfg: MacroConfig) -> None:
        self.cfg = cfg

    def read(self, now_ms: int) -> MacroReading:
        """Lecture courante. Seul point d'I/O de la chaîne de décision, et il
        est isolé ici exprès : les couches reçoivent le `RiskLevel` déjà résolu
        via le `LayerContext`, donc leur `evaluate()` reste pur."""
        if not self.cfg.enabled:
            return MacroReading(RiskLevel.UNKNOWN, now_ms, "disabled",
                                "macro.enabled=false — veto macro §4.1 inactif")
        if self.cfg.provider == "none":
            return MacroReading(RiskLevel.UNKNOWN, now_ms, "none",
                                "aucun fournisseur on-chain branché — veto macro §4.1 inactif")
        if self.cfg.provider == "file":
            return self._read_file(now_ms)
        return MacroReading(RiskLevel.UNKNOWN, now_ms, self.cfg.provider,
                            f"fournisseur inconnu {self.cfg.provider!r}")

    def _read_file(self, now_ms: int) -> MacroReading:
        path = Path(self.cfg.file_path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("macro: lecture de %s impossible (%s) — UNKNOWN", path, exc)
            return MacroReading(RiskLevel.UNKNOWN, now_ms, "file", f"illisible: {exc}")

        level = self._parse_level(raw.get("risk_level"))
        as_of = self._parse_as_of(raw.get("as_of"), now_ms)
        age_h = (now_ms - as_of) / 3_600_000.0
        if age_h > self.cfg.max_age_h:
            return MacroReading(RiskLevel.UNKNOWN, as_of, "file",
                                f"donnée périmée ({age_h:.1f}h > {self.cfg.max_age_h:g}h)")
        return MacroReading(level, as_of, "file", raw.get("note", ""))

    @staticmethod
    def _parse_level(value) -> RiskLevel:
        if not isinstance(value, str):
            return RiskLevel.UNKNOWN
        try:
            return RiskLevel(value.strip().lower())
        except ValueError:
            logger.warning("macro: risk_level inconnu %r — UNKNOWN", value)
            return RiskLevel.UNKNOWN

    @staticmethod
    def _parse_as_of(value, now_ms: int) -> int:
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp() * 1000)
            except ValueError:
                pass
        # Pas d'horodatage exploitable ⇒ on considère la donnée comme datant de
        # maintenant serait trop généreux : on la fait échouer au test d'âge.
        return 0


def resolve(agent: Optional[MacroRegimeAgent], now_ms: int) -> MacroReading:
    if agent is None:
        return MacroReading(RiskLevel.UNKNOWN, now_ms, "absent", "aucun MacroRegimeAgent")
    return agent.read(now_ms)


__all__ = ["MacroReading", "MacroRegimeAgent", "resolve"]

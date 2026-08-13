"""
ParamRegistry — source de vérité des paramètres. SPEC §12.2.

Stocke des `ParameterSet` **versionnés et immuables**, trois actifs à tout
instant (`defensive`, `neutral`, `aggressive`), tous trois ayant passé les
critères d'acceptation du §9.4. Un set jamais validé ne peut pas être
enregistré — c'est la règle qui empêche un paramètre « qui a l'air bien » de
se retrouver en production sans être passé par le seul dispositif capable de
le rejeter.

Deux propriétés qui ont l'air décoratives et ne le sont pas :

* **Immuabilité réelle** (pas seulement `frozen=True`) : le dict de paramètres
  est enveloppé dans un `MappingProxyType`. Un `frozen` dataclass empêche de
  réassigner l'attribut, pas de muter le dict qu'il pointe ; sans le proxy, un
  appelant pourrait modifier un set déjà validé et le §9 aurait validé autre
  chose que ce qui tourne.
* **Journal append-only** : chaque enregistrement et chaque changement de
  posture est écrit en JSONL, jamais réécrit. Quand une performance dérive,
  la première question est « qu'est-ce qui a changé, quand, et pourquoi » ;
  un état courant sans historique ne répond pas.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger("sdm.confluence.adaptive.registry")

POSTURES = ("defensive", "neutral", "aggressive")

# Métriques OOS exigées à l'enregistrement (§12.2 : « un set jamais validé ne
# peut pas être enregistré »).
REQUIRED_METRICS = ("profit_factor", "fee_ratio", "trades", "max_drawdown")


class RegistryError(Exception):
    """Enregistrement refusé, ou registre inutilisable."""


@dataclass(frozen=True)
class ParameterSet:
    """Un jeu complet de paramètres du §7, validé et figé.

    `params` est indexé par chemin pointé (« risk.k_stop »), la même notation
    que `ConfluenceConfig.replace_path` : un set s'applique donc à la config de
    base sans traduction, et la validation du YAML (§7) s'applique au résultat.

    `conditioning_bounds` porte les bornes d'interpolation du §12.3 sous la
    forme `{chemin: (valeur_à_vol_basse, valeur_à_vol_haute)}`. Le §12.3 exige
    qu'elles fassent partie du set, pour qu'elles soient optimisées et validées
    comme le reste plutôt que réglées à la main sur le côté.
    """

    version: str
    posture: str
    params: Mapping[str, float]
    validated_at: datetime
    oos_metrics: Mapping[str, float]
    data_window: Tuple[datetime, datetime]
    conditioning_bounds: Mapping[str, Tuple[float, float]] = field(default_factory=dict)
    config_hash: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        if self.posture not in POSTURES:
            raise RegistryError(f"posture inconnue: {self.posture!r} (parmi {POSTURES})")
        # Immuabilité réelle : on gèle les mappings eux-mêmes, pas seulement
        # les attributs qui les référencent.
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))
        object.__setattr__(self, "oos_metrics", MappingProxyType(dict(self.oos_metrics)))
        object.__setattr__(self, "conditioning_bounds", MappingProxyType(
            {k: tuple(v) for k, v in dict(self.conditioning_bounds).items()}))
        if not self.config_hash:
            object.__setattr__(self, "config_hash", self.compute_hash())

    def compute_hash(self) -> str:
        payload = json.dumps({
            "params": dict(sorted(self.params.items())),
            "bounds": {k: list(v) for k, v in sorted(self.conditioning_bounds.items())},
        }, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def drift_vs(self, other: "ParameterSet") -> Dict[str, float]:
        """Dérive relative par paramètre, |neuf − ancien| / |ancien|.

        Sert au garde-fou du §12.4 (> 40 % ⇒ promotion bloquée). Un paramètre
        absent de l'ancien set compte comme une dérive infinie : c'est un
        changement de structure, pas un réglage.
        """
        out: Dict[str, float] = {}
        for key, value in self.params.items():
            previous = other.params.get(key)
            if previous is None:
                out[key] = float("inf")
            elif previous == 0:
                out[key] = 0.0 if value == 0 else float("inf")
            else:
                out[key] = abs(value - previous) / abs(previous)
        return out

    def to_json(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "posture": self.posture,
            "params": dict(self.params),
            "validated_at": self.validated_at.isoformat(),
            "oos_metrics": dict(self.oos_metrics),
            "data_window": [self.data_window[0].isoformat(), self.data_window[1].isoformat()],
            "conditioning_bounds": {k: list(v) for k, v in self.conditioning_bounds.items()},
            "config_hash": self.config_hash,
            "notes": self.notes,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> "ParameterSet":
        window = raw.get("data_window") or [_EPOCH.isoformat(), _EPOCH.isoformat()]
        return cls(
            version=str(raw["version"]),
            posture=str(raw["posture"]),
            params={k: float(v) for k, v in (raw.get("params") or {}).items()},
            validated_at=_parse_dt(raw.get("validated_at")),
            oos_metrics={k: float(v) for k, v in (raw.get("oos_metrics") or {}).items()},
            data_window=(_parse_dt(window[0]), _parse_dt(window[1])),
            conditioning_bounds={k: (float(v[0]), float(v[1]))
                                 for k, v in (raw.get("conditioning_bounds") or {}).items()},
            config_hash=str(raw.get("config_hash", "")),
            notes=str(raw.get("notes", "")),
        )


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return _EPOCH


# ── Set de repli embarqué (§12.8) ────────────────────────────────────────────
#
# Registre illisible ⇒ on repart sur CE set, pas sur rien. Ses paramètres sont
# ceux du §7, c'est-à-dire les défauts de la spec — jamais validés sur données,
# et c'est assumé : le repli sert à garder le bot cohérent et bruyant, pas à
# trader. `oos_metrics` est vide, donc l'APM sait qu'il tourne dégradé et le
# signale à chaque cycle.
FALLBACK_NEUTRAL = ParameterSet(
    version="fallback-neutral-embedded",
    posture="neutral",
    params={
        "regime_1h.adx_trend": 25.0,
        "regime_1h.adx_range": 20.0,
        "risk.k_stop": 1.5,
        "risk.edge_multiple": 5.0,
        "risk.risk_pct": 0.005,
        "risk.max_trades_per_day": 3,
    },
    validated_at=_EPOCH,
    oos_metrics={},
    data_window=(_EPOCH, _EPOCH),
    conditioning_bounds={
        "risk.k_stop": (1.2, 2.0),
        "risk.edge_multiple": (7.0, 4.0),
        "risk.risk_pct": (0.005, 0.0035),
    },
    notes="repli embarqué — JAMAIS validé sur données (§12.8)",
)


class ParamRegistry:
    """Persistance + journal des ParameterSets."""

    def __init__(self, path: Optional[Path] = None) -> None:
        base = Path(os.environ.get("CONFLUENCE_REGISTRY_DIR")
                    or (Path(__file__).resolve().parent.parent / "state" / "param_registry"))
        self.dir = Path(path) if path else base
        self.state_path = self.dir / "active.json"
        self.journal_path = self.dir / "journal.jsonl"
        self.archive_dir = self.dir / "sets"
        self._active: Dict[str, ParameterSet] = {}
        self.degraded = False
        self.observation_mode = False

    # -- enregistrement ------------------------------------------------------

    def register(self, param_set: ParameterSet, acceptance_passed: bool,
                 reason: str = "", now: Optional[datetime] = None) -> None:
        """Enregistre un set et le rend actif pour sa posture.

        `acceptance_passed` vient du §9.4 et n'est pas recalculé ici : le
        registre ne sait pas backtester, et prétendre le contraire ferait
        exister deux définitions de « validé ».
        """
        if not param_set.oos_metrics:
            raise RegistryError(
                f"{param_set.version}: aucune métrique out-of-sample — "
                f"un set non validé ne peut pas être enregistré (§12.2)")
        missing = [m for m in REQUIRED_METRICS if m not in param_set.oos_metrics]
        if missing:
            raise RegistryError(f"{param_set.version}: métriques OOS manquantes {missing}")
        if not acceptance_passed:
            raise RegistryError(
                f"{param_set.version}: critères d'acceptation §9.4 non remplis — "
                f"promotion refusée")

        previous = self._active.get(param_set.posture)
        self._active[param_set.posture] = param_set
        self._archive(param_set)
        self.save()
        self._journal("register", now, {
            "version": param_set.version,
            "posture": param_set.posture,
            "replaces": previous.version if previous else None,
            "config_hash": param_set.config_hash,
            "oos_metrics": dict(param_set.oos_metrics),
            "reason": reason,
        })
        logger.info("registre: %s promu en %s (remplace %s)", param_set.version,
                    param_set.posture, previous.version if previous else "—")

    def get(self, posture: str) -> ParameterSet:
        """Set actif d'une posture, ou le repli embarqué.

        Ne lève jamais : le ConfluenceAgent doit obtenir un jeu de paramètres
        valide en toutes circonstances (§12.8). Un registre vide est un
        incident à signaler, pas une raison d'arrêter de décider — surtout que
        « ne pas décider » est déjà le comportement par défaut du bot.
        """
        found = self._active.get(posture)
        if found is not None:
            return found
        self.degraded = True
        logger.error("registre: aucun set actif pour la posture %s — repli embarqué", posture)
        return FALLBACK_NEUTRAL

    @property
    def active(self) -> Mapping[str, ParameterSet]:
        return MappingProxyType(dict(self._active))

    @property
    def complete(self) -> bool:
        return all(p in self._active for p in POSTURES)

    # -- posture -------------------------------------------------------------

    def record_posture(self, old: str, new: str, source: str, detail: Mapping[str, Any],
                       now: Optional[datetime] = None) -> None:
        self._journal("posture", now, {
            "from": old, "to": new, "source": source, **dict(detail),
        })

    def set_observation(self, on: bool, reason: str, now: Optional[datetime] = None) -> None:
        if self.observation_mode == on:
            return
        self.observation_mode = on
        self.save()
        self._journal("observation", now, {"enabled": on, "reason": reason})
        logger.warning("registre: mode observation %s (%s)", "ACTIVÉ" if on else "levé", reason)

    # -- persistance ---------------------------------------------------------

    def save(self) -> None:
        payload = {
            "active": {p: s.to_json() for p, s in self._active.items()},
            "observation_mode": self.observation_mode,
        }
        _atomic_write(self.state_path, json.dumps(payload, ensure_ascii=False, indent=2))

    def load(self) -> "ParamRegistry":
        """Recharge l'état. Un fichier illisible est ARCHIVÉ, jamais écrasé.

        Perdre le registre en silence ferait repartir le bot sur le repli
        embarqué — jamais validé — sans que personne ne le sache. On préfère
        un incident visible.
        """
        self._active = {}
        if not self.state_path.exists():
            return self
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            stamp = int(datetime.now(timezone.utc).timestamp())
            backup = self.state_path.with_suffix(f".corrupt.{stamp}")
            try:
                self.state_path.rename(backup)
            except OSError:
                pass
            self.degraded = True
            logger.error("registre illisible (%s) — archivé dans %s, repli embarqué actif",
                         exc, backup.name)
            self._journal("corrupt", None, {"error": str(exc), "backup": backup.name})
            return self

        for posture, blob in (raw.get("active") or {}).items():
            try:
                self._active[posture] = ParameterSet.from_json(blob)
            except (KeyError, RegistryError, TypeError, ValueError) as exc:
                self.degraded = True
                logger.error("registre: set %s illisible (%s) — ignoré", posture, exc)
        self.observation_mode = bool(raw.get("observation_mode", False))
        return self

    def history(self) -> List[Dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        out = []
        for line in self.journal_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def _archive(self, param_set: ParameterSet) -> None:
        try:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            path = self.archive_dir / f"{param_set.version}.json"
            path.write_text(json.dumps(param_set.to_json(), ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except OSError as exc:
            logger.warning("registre: archivage de %s impossible (%s)", param_set.version, exc)

    def _journal(self, event: str, now: Optional[datetime], detail: Mapping[str, Any]) -> None:
        """Écriture append-only. Jamais de réécriture, jamais de troncature."""
        record = {
            "ts": (now or datetime.now(timezone.utc)).isoformat(),
            "event": event,
            **dict(detail),
        }
        try:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.journal_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            logger.error("registre: journal non écrit (%s) — %s", exc, record)


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".reg-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


__all__ = ["FALLBACK_NEUTRAL", "POSTURES", "REQUIRED_METRICS", "ParamRegistry",
           "ParameterSet", "RegistryError"]

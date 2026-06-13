"""
DecisionJournal — mémoire expérientielle des agents gouverneurs (2026-06-13).

Principe (exigence francois) : un agent doit APPRENDRE DE SES ERREURS, sinon le
déployer n'a aucun sens. Chaque décision (tactique qwen / stratège Opus) est :
  1. JOURNALISÉE avec son contexte + l'equity au moment de la décision,
  2. NOTÉE après une fenêtre d'observation (equity_delta + coupures survenues),
  3. RÉINJECTÉE dans le prompt suivant → l'agent voit son palmarès et corrige.

C'est de l'apprentissage in-context (expérientiel), immédiat, et PERSISTANT à
travers les redémarrages (fichier jsonl) → résout aussi l'amnésie-au-boot.

Verdict d'une décision sur sa fenêtre :
  - MAUVAIS : equity baisse > 0.3% OU >= 3 coupures
  - BON     : equity monte > 0.1% ET <= 1 coupure
  - NEUTRE  : entre les deux
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("v7.journal")


class DecisionJournal:
    def __init__(self, path: Path, score_window_sec: float, equity_ref: float = 1000.0) -> None:
        self._path = Path(path)
        self._window = float(score_window_sec)
        self._equity_ref = max(float(equity_ref), 1.0)
        self._entries: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if not self._path.exists():
            return []
        out = []
        try:
            for line in self._path.read_text().splitlines():
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        except Exception as e:
            logger.warning("journal load %s: %r", self._path, e)
        return out[-500:]   # borne mémoire

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(exist_ok=True)
            self._path.write_text("\n".join(json.dumps(e, default=str) for e in self._entries[-500:]))
        except Exception as e:
            logger.debug("journal save: %r", e)

    # ── 1. Enregistrer une décision ──────────────────────────────────────────
    def record(self, context: dict, params: dict, equity_now: float) -> None:
        self._entries.append({
            "ts": time.time(),
            "context": {k: context.get(k) for k in (
                "regime", "vol_ratio_vs_median", "emergency_exits_last_1h")},
            "params": params,
            "equity_at": round(float(equity_now), 2),
            "scored": False,
            "outcome": None,
        })
        self._save()

    # ── 2. Noter les décisions dont la fenêtre est écoulée ───────────────────
    def score_pending(self, equity_now: float, emergency_ts: list[float]) -> int:
        """emergency_ts : timestamps des coupures (pour compter dans la fenêtre)."""
        now = time.time()
        n = 0
        for e in self._entries:
            if e.get("scored") or (now - e["ts"]) < self._window:
                continue
            t0, t1 = e["ts"], e["ts"] + self._window
            cuts = sum(1 for ts in emergency_ts if t0 <= ts <= t1)
            eq_delta = float(equity_now) - float(e["equity_at"])
            eq_delta_pct = eq_delta / self._equity_ref * 100.0
            if eq_delta_pct < -0.3 or cuts >= 3:
                verdict = "MAUVAIS"
            elif eq_delta_pct > 0.1 and cuts <= 1:
                verdict = "BON"
            else:
                verdict = "NEUTRE"
            e["outcome"] = {"equity_delta_pct": round(eq_delta_pct, 3),
                            "cuts_in_window": cuts, "verdict": verdict}
            e["scored"] = True
            n += 1
        if n:
            self._save()
        return n

    # ── 3. Restituer le palmarès pour le prompt ──────────────────────────────
    def feedback_text(self, k: int = 6) -> str:
        scored = [e for e in self._entries if e.get("scored") and e.get("outcome")]
        if not scored:
            return "(pas encore d'historique noté — première phase d'apprentissage)"
        lines = []
        now = time.time()
        for e in scored[-k:]:
            o = e["outcome"]; p = e["params"]; c = e["context"]
            age = int((now - e["ts"]) / 60)
            pstr = ", ".join(f"{kk}={vv}" for kk, vv in p.items())
            lines.append(
                f"- il y a {age}min [régime={c.get('regime')} vol={c.get('vol_ratio_vs_median')}] "
                f"→ {pstr} | résultat: equity {o['equity_delta_pct']:+.2f}%, "
                f"{o['cuts_in_window']} coupures → {o['verdict']}")
        # bilan agrégé
        from collections import Counter
        cnt = Counter(e["outcome"]["verdict"] for e in scored)
        bilan = f"Bilan global: {cnt.get('BON',0)} BON / {cnt.get('NEUTRE',0)} NEUTRE / {cnt.get('MAUVAIS',0)} MAUVAIS"
        return bilan + "\n" + "\n".join(lines)

    @property
    def equity_ref(self) -> float:
        return self._equity_ref

    def set_equity_ref(self, v: float) -> None:
        if v and v > 0:
            self._equity_ref = float(v)

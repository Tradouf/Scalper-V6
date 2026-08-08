"""
Persistance des signaux — JSONL append-only sous grokwatch/state/.

Dédoublonnage par content_hash (un même mail re-livré ou ré-ingéré
manuellement n'est compté qu'une fois).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List

_DEFAULT_STATE_DIR = Path(__file__).resolve().parent / "state"


def _state_dir() -> Path:
    raw = os.environ.get("GROKWATCH_STATE_DIR", "").strip()
    d = Path(raw) if raw else _DEFAULT_STATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _signals_path() -> Path:
    return _state_dir() / "signals.jsonl"


def load_signals() -> List[dict]:
    path = _signals_path()
    if not path.exists():
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def record_signal(sig: dict) -> bool:
    """Ajoute le signal ; False si son content_hash est déjà enregistré."""
    existing = {s.get("content_hash") for s in load_signals()}
    if sig.get("content_hash") in existing:
        return False
    with open(_signals_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(sig, ensure_ascii=False) + "\n")
    return True

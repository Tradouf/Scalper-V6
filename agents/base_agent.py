"""
Agent de base — tous les agents héritent de cette classe.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, Optional

import requests

from config.settings import LOCALAI_BASE_URL, MODELS
from memory.shared_memory import SharedMemory

# Sémaphore global : on limite à 1 requête LLM à la fois
_LLM_SEMAPHORE = threading.Semaphore(1)


class BaseAgent:
    def __init__(self, name: str, memory: SharedMemory):
        self.name = name
        self.memory = memory
        self.logger = logging.getLogger(f"sdm.{name}")
        self.model = MODELS.get(name, MODELS["trader"])
        self._url = f"{LOCALAI_BASE_URL}/chat/completions"

    # ─────────────────────────────────────────────────────────────
    # Appel LLM bas niveau
    # ─────────────────────────────────────────────────────────────
    def _llm(
        self,
        system: str,
        user: str,
        temperature: float = 0.2,
        max_tokens: int = 500,
    ) -> Optional[str]:
        """
        Appel LLM centralisé avec :
        - sémaphore global (un seul appel à la fois pour tout le bot),
        - timeout allongé (90s),
        - vérification que la réponse contient bien 'choices',
        - logs explicites en cas de réponse invalide.
        """
        with _LLM_SEMAPHORE:
            for attempt in range(2):
                try:
                    resp = requests.post(
                        self._url,
                        json={
                            "model": self.model,
                            "messages": [
                                {"role": "system", "content": system},
                                {"role": "user", "content": user},
                            ],
                            "temperature": temperature,
                            "max_tokens": max_tokens,
                        },
                        timeout=90,
                    )
                    data = resp.json()
                    if "choices" not in data:
                        # On logge la réponse brute tronquée, en échappant les %
                        raw = str(data)[:200].replace("%", "%%")
                        self.logger.warning(
                            "LLM réponse sans choices (%s): %s", self.model, raw
                        )
                        continue
                    return data["choices"][0]["message"]["content"].strip()
                except Exception as e:
                    self.logger.error("LLM error: %s", e)
                    if attempt == 0:
                        import time

                        time.sleep(3)
            return None

    # ─────────────────────────────────────────────────────────────
    # Parsing JSON bas niveau
    # ─────────────────────────────────────────────────────────────
    def _parse_json(self, text: str) -> Optional[Dict[str, Any]]:
        """Parse JSON robuste — gère markdown, texte avant/après."""
        if not text:
            return None

        import re

        # Bloc ```json ... ```
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            text = m.group(1)
        else:
            s = text.find("{")
            e = text.rfind("}") + 1
            if s == -1 or e == 0:
                return None
            text = text[s:e]

        try:
            return json.loads(text)
        except Exception:
            return None

    # ─────────────────────────────────────────────────────────────
    # Wrappers publics utilisés par les agents
    # ─────────────────────────────────────────────────────────────
    def llm(
        self,
        system: str,
        user: str,
        temperature: float = 0.2,
        max_tokens: int = 500,
    ) -> Optional[str]:
        return self._llm(system, user, temperature=temperature, max_tokens=max_tokens)

    def parse_json(self, text: str) -> Optional[Dict[str, Any]]:
        return self._parse_json(text)

    # ─────────────────────────────────────────────────────────────
    # Messagerie entre agents
    # ─────────────────────────────────────────────────────────────
    def _send_message(self, to: str, content: str) -> None:
        self.memory.send_message(self.name, to, content)

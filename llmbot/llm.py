"""Client LLM centralisé — 1 appel à la fois, JSON structuré."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Dict, Optional

import requests

from llmbot import config

logger = logging.getLogger("sdm.llmbot.llm")

_SEMAPHORE = threading.Semaphore(1)


def _parse_json(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", raw)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            return None
    return None


def chat(
    system: str,
    user: str,
    model: Optional[str] = None,
    temperature: float = 0.15,
    max_tokens: int = 600,
) -> Optional[str]:
    model = model or config.MODEL_TRADER
    url = f"{config.LOCALAI_BASE_URL}/chat/completions"
    with _SEMAPHORE:
        for attempt in range(2):
            try:
                resp = requests.post(
                    url,
                    json={
                        "model": model,
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
                    logger.warning("LLM sans choices (%s): %s", model, str(data)[:200])
                    continue
                return data["choices"][0]["message"]["content"].strip()
            except Exception as e:
                logger.error("LLM error (%s): %r", model, e)
                if attempt == 0:
                    time.sleep(2)
    return None


def chat_json(
    system: str,
    user: str,
    model: Optional[str] = None,
    temperature: float = 0.15,
) -> Optional[Dict[str, Any]]:
    raw = chat(system=system, user=user, model=model, temperature=temperature)
    return _parse_json(raw)